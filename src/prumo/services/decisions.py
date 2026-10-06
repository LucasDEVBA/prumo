"""Caminho de cada lead: a IA decide, o juiz confere e, se sorteado, vai para revisão humana."""

from __future__ import annotations

import logging
import random
from collections.abc import Callable

from prumo.domain.errors import ConflictError, InvalidInputError, NotFoundError, ProviderError
from prumo.domain.models import Decision
from prumo.ports import Classifier, Clock, DecisionRepository, Judge, PromptStore, ReviewWorkflow

logger = logging.getLogger(__name__)

MAX_LEAD_CHARS = 2000


class BernoulliSampler:
    """Sorteia cada decisão com a mesma chance.

    A amostra PRECISA ser aleatória: se só os casos duvidosos fossem revisados, a correção do
    viés do juiz ficaria enviesada. É por isso que a fila de aprovação de um CRM (que só recebe
    QUENTE/MORNO) não serve, sozinha, como amostra.
    """

    def __init__(self, rng: random.Random, rate: float) -> None:
        self._rng = rng
        self.rate = rate

    @property
    def rate(self) -> float:
        return self._rate

    @rate.setter
    def rate(self, value: float) -> None:
        if not 0.0 <= value <= 1.0:
            raise InvalidInputError("a taxa de amostragem precisa estar entre 0 e 1")
        self._rate = value

    def __call__(self) -> bool:
        return self._rng.random() < self._rate


class DecisionService:
    def __init__(
        self,
        *,
        classifier: Classifier,
        judge: Judge,
        repository: DecisionRepository,
        prompts: PromptStore,
        review_workflow: ReviewWorkflow,
        sampler: Callable[[], bool],
        clock: Clock,
        new_id: Callable[[], str],
    ) -> None:
        self._classifier = classifier
        self._judge = judge
        self._repository = repository
        self._prompts = prompts
        self._review_workflow = review_workflow
        self._sampler = sampler
        self._clock = clock
        self._new_id = new_id

    def process(self, lead_text: str) -> Decision:
        text = lead_text.strip()
        if not text or len(text) > MAX_LEAD_CHARS:
            raise InvalidInputError(f"o lead precisa ter entre 1 e {MAX_LEAD_CHARS} caracteres")

        prompt = self._prompts.active()
        classification = self._classifier.classify(text, prompt)
        verdict = self._judge.judge(text, classification)
        decision = Decision(
            id=self._new_id(),
            lead_text=text,
            prompt_version=prompt.id,
            predicted=classification.predicted,
            rationale=classification.rationale,
            judge_approved=verdict.approved,
            judge_reason=verdict.reason,
            sampled_for_review=self._sampler(),
            created_at=self._clock.now(),
        )
        self._repository.add(decision)
        if decision.sampled_for_review:
            _notify_workflow(self._review_workflow.request_review, decision, "request_review")
        return decision


class ReviewService:
    def __init__(
        self,
        *,
        repository: DecisionRepository,
        review_workflow: ReviewWorkflow,
        clock: Clock,
    ) -> None:
        self._repository = repository
        self._review_workflow = review_workflow
        self._clock = clock

    def pending(self, limit: int = 10) -> list[Decision]:
        return self._repository.pending_review(max(0, min(limit, 50)))

    def submit(
        self, decision_id: str, *, correct: bool, reviewer: str, audit: bool = True
    ) -> Decision:
        """Grava o rótulo humano. `audit=False` só para rótulos automáticos do simulador."""
        current = self._repository.get(decision_id)
        if current is None:
            raise NotFoundError("decisão não encontrada", details={"id": decision_id})
        if current.human_correct is not None:
            return self._repeat(current, correct=correct, reviewer=reviewer)
        if not current.sampled_for_review:
            raise InvalidInputError(
                "só decisões sorteadas entram na conta; rotular outras enviesaria a estimativa",
                details={"id": decision_id},
            )
        labeled = current.model_copy(
            update={
                "human_correct": correct,
                "reviewer": reviewer[:80],
                "labeled_at": self._clock.now(),
            }
        )
        try:
            self._repository.save_label(labeled)
        except ConflictError:
            # Dois envios simultâneos do mesmo rótulo: o segundo perde a corrida no banco,
            # mas é a mesma resposta, então vira repetição idempotente.
            saved = self._repository.get(decision_id)
            if saved is None or saved.human_correct is None:
                raise
            return self._repeat(saved, correct=correct, reviewer=reviewer)
        _notify_workflow(self._review_workflow.complete_review, labeled, "complete_review")
        # Rótulo de pessoa é ação sensível (muda a estimativa): fica na trilha em INFO.
        logger.log(
            logging.INFO if audit else logging.DEBUG,
            "review_submitted",
            extra={
                "decision_id": decision_id,
                "prompt_version": labeled.prompt_version,
                "reviewer": labeled.reviewer,
            },
        )
        return labeled

    def _repeat(self, current: Decision, *, correct: bool, reviewer: str) -> Decision:
        """Reenvio do mesmo rótulo (ex.: retry depois de timeout) é aceito sem contar de novo."""
        if current.human_correct != correct or current.reviewer != reviewer[:80]:
            raise ConflictError("decisão já rotulada", details={"id": current.id})
        _notify_workflow(self._review_workflow.complete_review, current, "complete_review")
        return current


def _notify_workflow(call: Callable[[Decision], None], decision: Decision, operation: str) -> None:
    """O fluxo de revisão é efeito secundário: a decisão e o rótulo já estão gravados.

    Propagar a falha faria a API responder erro para algo que foi salvo, e o retry do cliente
    criaria uma decisão duplicada (contada duas vezes na estimativa). A falha é registrada em
    nível ERROR com um nome fixo, que vira métrica e alarme na infraestrutura.
    """
    try:
        call(decision)
    except ProviderError as exc:
        logger.error(
            "review_workflow_failed",
            extra={"decision_id": decision.id, "operation": operation, "error_code": exc.code},
        )
