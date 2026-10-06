"""Leads sintéticos e a rubrica que serve de gabarito.

Nenhum dado real é usado. Cada lead nasce de campos estruturados (profissão, movimentação,
empréstimo, contato anterior), vira texto livre e pode ser lido de volta pelo parser. A rubrica
aplicada aos campos é o gabarito: assim o simulador sabe a resposta certa de cada decisão.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from prumo.domain.models import LeadClass

TIER_POINTS: dict[str, int] = {
    "médica": 50,
    "engenheiro de software": 50,
    "empresária": 50,
    "advogado": 50,
    "gerente de banco": 50,
    "professora": 30,
    "analista administrativo": 30,
    "técnico de enfermagem": 30,
    "corretor de imóveis": 30,
    "estudante": 10,
    "auxiliar de serviços": 10,
    "motorista de aplicativo": 10,
}
INCOME_OPTIONS = (3_500, 6_000, 9_500, 14_000, 18_000, 26_000, 41_000)
HIGH_INCOME = 20_000
MID_INCOME = 8_000
HOT_THRESHOLD = 70
WARM_THRESHOLD = 40

RUBRIC_TEXT = """\
Rubrica de qualificação (pontos):
- Profissão tier A (médica, engenheiro de software, empresária, advogado, gerente de banco): 50.
  Tier B (professora, analista administrativo, técnico de enfermagem, corretor de imóveis): 30.
  Tier C (estudante, auxiliar de serviços, motorista de aplicativo): 10.
- Movimentação média acima de R$ 20.000 por mês: +30. De R$ 8.000 a R$ 20.000: +15.
- Sem empréstimo ativo: +10. Já respondeu bem a um contato anterior: +10.
- 70 pontos ou mais: QUENTE. De 40 a 69: MORNO. Abaixo de 40: FRIO."""

_PATTERN = re.compile(
    r"^Sou (?P<prof>[^,]+), tenho (?P<age>\d{2}) anos\. "
    r"Movimento em média R\$ (?P<income>[\d.]+) por mês"
    r"(?P<loan>, sem empréstimos| e tenho um financiamento ativo)\."
    r"(?P<prev> Já falei com vocês antes e gostei do atendimento\.)?$"
)


@dataclass(frozen=True, slots=True)
class LeadProfile:
    profession: str
    age: int
    monthly_income: int
    has_loan: bool
    previous_contact: bool

    def score(self) -> int:
        points = TIER_POINTS[self.profession]
        if self.monthly_income > HIGH_INCOME:
            points += 30
        elif self.monthly_income >= MID_INCOME:
            points += 15
        if not self.has_loan:
            points += 10
        if self.previous_contact:
            points += 10
        return points

    def expected_class(self) -> LeadClass:
        score = self.score()
        if score >= HOT_THRESHOLD:
            return LeadClass.QUENTE
        if score >= WARM_THRESHOLD:
            return LeadClass.MORNO
        return LeadClass.FRIO

    def to_text(self) -> str:
        income = f"{self.monthly_income:,}".replace(",", ".")
        loan = " e tenho um financiamento ativo" if self.has_loan else ", sem empréstimos"
        prev = " Já falei com vocês antes e gostei do atendimento." if self.previous_contact else ""
        return (
            f"Sou {self.profession}, tenho {self.age} anos. "
            f"Movimento em média R$ {income} por mês{loan}.{prev}"
        )


def parse_lead(text: str) -> LeadProfile | None:
    """Lê de volta um lead sintético. Devolve None para textos fora do formato."""
    match = _PATTERN.match(text.strip())
    if match is None or match["prof"] not in TIER_POINTS:
        return None
    return LeadProfile(
        profession=match["prof"],
        age=int(match["age"]),
        monthly_income=int(match["income"].replace(".", "")),
        has_loan=match["loan"].startswith(" e tenho"),
        previous_contact=match["prev"] is not None,
    )


def expected_class_for(text: str) -> LeadClass | None:
    """Gabarito de um texto sintético, ou None se o texto não veio do gerador."""
    profile = parse_lead(text)
    return profile.expected_class() if profile else None


class LeadGenerator:
    """Gera perfis aleatórios e reprodutíveis (mesma semente, mesmos leads)."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng

    def profile(self) -> LeadProfile:
        rng = self._rng
        return LeadProfile(
            profession=rng.choice(list(TIER_POINTS)),
            age=rng.randint(24, 59),
            monthly_income=rng.choice(INCOME_OPTIONS),
            has_loan=rng.random() < 0.4,
            previous_contact=rng.random() < 0.3,
        )

    def text(self) -> str:
        return self.profile().to_text()
