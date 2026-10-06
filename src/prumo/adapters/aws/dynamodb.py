"""Decisões, contadores da PPI, trilha de eventos e leituras de qualidade no DynamoDB.

Uma tabela só (single-table design), com chaves genéricas `pk`/`sk`:

| Entidade   | pk             | sk                  | Observação                           |
|------------|----------------|---------------------|--------------------------------------|
| Decisão    | DECISION#<id>  | DECISION            | `gsi1pk`/`gsi1sk` só se pendente;    |
|            |                |                     | `expires_at` (TTL, texto do lead)    |
| Contadores | STATS#<versão> | SHARD#<nnn>         | somas da PPI, sempre com `ADD`       |
| Evento     | EVENT          | <instante>#<id>     | deploys, alarmes e rollbacks         |
| Leitura    | SNAPSHOT       | <instante>#<versão> | uma por avaliação horária            |

O índice `gsi1` (chaves `gsi1pk`/`gsi1sk`, projeção ALL) é esparso: só as decisões pendentes
têm `gsi1pk`, então ele É a fila de revisão, e gravar o rótulo (REMOVE) tira o item da fila.

Só a decisão expira (atributo TTL `expires_at`): ela guarda o texto do lead, dado pessoal. Os
contadores, os eventos e as leituras não têm dado pessoal e ficam.

As transações levam `ClientRequestToken` derivado da operação e do id da decisão: se o botocore
reenviar uma transação que já tinha sido aplicada (timeout na resposta), o DynamoDB a reconhece
e não soma os contadores de novo.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import random
import time
import uuid
import zlib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ValidationError

from prumo.adapters.aws._common import client_config, error_code, provider_error
from prumo.domain.errors import ConflictError, NotFoundError, ProviderError
from prumo.domain.models import Decision, Event, QualitySnapshot
from prumo.stats.ppi import SufficientStats

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import (
        GetItemInputTypeDef,
        QueryInputTypeDef,
        TransactWriteItemTypeDef,
        UniversalAttributeValueTypeDef,
        UpdateTypeDef,
    )

    from prumo.config import Settings

logger = logging.getLogger(__name__)

SERVICE = "dynamodb"
PK = "pk"
SK = "sk"
GSI_NAME = "gsi1"
GSI_PK = "gsi1pk"
GSI_SK = "gsi1sk"
TASK_TOKEN = "task_token"  # noqa: S105 (nome do atributo, não um segredo)
DECISION_SK = "DECISION"
PENDING_PARTITION = "REVIEW#PENDING"
EVENT_PARTITION = "EVENT"
SNAPSHOT_PARTITION = "SNAPSHOT"
EXPIRES_AT = "expires_at"
"""Atributo de TTL da tabela (epoch em segundos)."""

DEFAULT_STATS_SHARDS = 8
"""Transações que tocam o mesmo item concorrentemente se cancelam (TransactionConflict).
Espalhar os contadores de uma versão em N itens reduz esse choque; a leitura soma os N."""

TRANSACTION_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.05

_UNLABELED_DECISION = f"attribute_exists({PK}) AND attribute_not_exists(human_correct)"
_CONDITION_FAILED = "ConditionalCheckFailed"
_TRANSACTION_CONFLICT = "TransactionConflict"
_TRANSACTION_IN_PROGRESS = "TransactionInProgress"
_RETRYABLE_REASONS = frozenset({_TRANSACTION_CONFLICT, _TRANSACTION_IN_PROGRESS})
"""Transitórios: outra transação no mesmo item, ou a mesma (mesmo token) ainda em andamento."""
_TOKEN_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:prumo:dynamodb:transaction")

type Item = dict[str, Any]
"""Item já desserializado. `Any` porque o DynamoDB devolve tipos variados por atributo."""


class ConditionFailedError(Exception):
    """Uma condição da escrita não valeu (item já existe, já rotulado, inexistente...)."""


class _TransactionCanceledError(Exception):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__(", ".join(reasons))
        self.reasons = reasons


_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def to_decimal(value: float) -> Decimal:
    """Converte float em Decimal pela representação decimal mais curta.

    `Decimal(0.1)` carregaria o erro binário (0.1000000000000000055...); `str(0.1)` é a menor
    string que volta ao mesmo float, então o número gravado é o que o código quis dizer.
    """
    if not math.isfinite(value):
        raise ValueError(f"o DynamoDB não aceita {value}")
    return Decimal(str(value))


def to_dynamo(value: object) -> object:
    """Prepara um valor Python para o serializador (que recusa float)."""
    if isinstance(value, float):
        return to_decimal(value)
    if isinstance(value, Mapping):
        return {key: to_dynamo(inner) for key, inner in value.items()}
    if isinstance(value, list | tuple):
        return [to_dynamo(inner) for inner in value]
    return value


def serialize(values: Mapping[str, object]) -> dict[str, UniversalAttributeValueTypeDef]:
    """Serializa atributos, omitindo os `None` (atributo ausente, em vez de NULL)."""
    return {
        key: _serializer.serialize(to_dynamo(value))
        for key, value in values.items()
        if value is not None
    }


def deserialize(raw: Mapping[str, Any]) -> Item:
    return {key: _deserializer.deserialize(value) for key, value in raw.items()}


def _as_utc(moment: datetime) -> datetime:
    aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC)


def sortable_timestamp(moment: datetime) -> str:
    """Instante em UTC com largura fixa: a ordem das strings é a ordem do tempo."""
    return _as_utc(moment).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def expires_at(created_at: datetime, ttl_days: int) -> int:
    """Epoch (segundos) em que o TTL do DynamoDB pode apagar o item."""
    return int((_as_utc(created_at) + timedelta(days=ttl_days)).timestamp())


def transaction_token(operation: str, decision_id: str) -> str:
    """Idempotência da transação: a mesma operação na mesma decisão gera o mesmo token.

    O DynamoDB lembra o token por 10 minutos: um reenvio idêntico volta como sucesso sem aplicar
    de novo, e um reenvio com outros valores volta como IdempotentParameterMismatch (conflito).
    """
    return str(uuid.uuid5(_TOKEN_NAMESPACE, f"{operation}:{decision_id}"))


def _backoff_seconds(attempt: int) -> float:
    # Jitter só espalha as novas tentativas no tempo; não tem papel de segurança.
    return random.uniform(0, BACKOFF_BASE_SECONDS * 2**attempt)  # noqa: S311


class DynamoTable:
    """Acesso de baixo nível à tabela única: serialização, paginação e tradução de erros."""

    def __init__(
        self,
        client: DynamoDBClient,
        name: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self.name = name
        self._sleep = sleep

    @classmethod
    def from_settings(cls, settings: Settings) -> DynamoTable:
        client = boto3.client("dynamodb", region_name=settings.aws_region, config=client_config())
        return cls(client, settings.decisions_table)

    def get(self, pk: str, sk: str, *, projection: str | None = None) -> Item | None:
        request: GetItemInputTypeDef = {
            "TableName": self.name,
            "Key": serialize({PK: pk, SK: sk}),
            "ConsistentRead": True,
        }
        if projection:
            request["ProjectionExpression"] = projection
        response = self._call("GetItem", lambda: self._client.get_item(**request))
        raw = response.get("Item")
        return deserialize(raw) if raw else None

    def put(self, item: Mapping[str, object]) -> None:
        self._call(
            "PutItem", lambda: self._client.put_item(TableName=self.name, Item=serialize(item))
        )

    def update(
        self, *, pk: str, sk: str, expression: str, values: Mapping[str, object], condition: str
    ) -> None:
        self._call(
            "UpdateItem",
            lambda: self._client.update_item(
                TableName=self.name,
                Key=serialize({PK: pk, SK: sk}),
                UpdateExpression=expression,
                ConditionExpression=condition,
                ExpressionAttributeValues=serialize(values),
            ),
        )

    def query(
        self,
        key_attribute: str,
        key_value: str,
        *,
        index: str | None = None,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> list[Item]:
        """Lê uma partição inteira (ou até `limit` itens), seguindo a paginação."""
        request = self._query_request(key_attribute, key_value, index, newest_first)
        items: list[Item] = []
        while True:
            if limit is not None:
                request["Limit"] = limit - len(items)
            page = self._call("Query", lambda: self._client.query(**request))
            items.extend(deserialize(raw) for raw in page.get("Items", []))
            last_key = page.get("LastEvaluatedKey")
            if not last_key or (limit is not None and len(items) >= limit):
                return items
            request["ExclusiveStartKey"] = last_key

    def _query_request(
        self, key_attribute: str, key_value: str, index: str | None, newest_first: bool
    ) -> QueryInputTypeDef:
        request: QueryInputTypeDef = {
            "TableName": self.name,
            "KeyConditionExpression": "#key = :key",
            "ExpressionAttributeNames": {"#key": key_attribute},
            "ExpressionAttributeValues": serialize({":key": key_value}),
            "ScanIndexForward": not newest_first,
        }
        if index:
            request["IndexName"] = index
        else:
            # Índices globais não aceitam leitura consistente; a tabela base aceita.
            request["ConsistentRead"] = True
        return request

    def transact(self, items: Sequence[TransactWriteItemTypeDef], *, request_token: str) -> None:
        """Grava tudo ou nada. Conflito com outra transação é transitório e é re-tentado.

        Todas as tentativas usam o mesmo `request_token`: se uma delas foi aplicada e só a
        resposta se perdeu, a seguinte não aplica de novo.
        """
        for attempt in range(1, TRANSACTION_ATTEMPTS + 1):
            try:
                self._call(
                    "TransactWriteItems",
                    lambda: self._client.transact_write_items(
                        TransactItems=items, ClientRequestToken=request_token
                    ),
                )
                return
            except _TransactionCanceledError as exc:
                if _CONDITION_FAILED in exc.reasons:
                    raise ConditionFailedError(str(exc)) from exc
                retryable = not _RETRYABLE_REASONS.isdisjoint(exc.reasons)
                if not retryable or attempt == TRANSACTION_ATTEMPTS:
                    logger.error(
                        "dynamodb_transaction_canceled",
                        extra={"reasons": exc.reasons, "attempts": attempt},
                    )
                    raise ProviderError(
                        "transação cancelada pelo DynamoDB", details={"reasons": exc.reasons}
                    ) from exc
                self._sleep(_backoff_seconds(attempt))

    def _call[T](self, operation: str, call: Callable[[], T]) -> T:
        try:
            return call()
        except ClientError as exc:
            code = error_code(exc)
            if code in ("ConditionalCheckFailedException", "IdempotentParameterMismatchException"):
                # Mismatch = o mesmo token já foi usado com outros valores: a decisão já existe
                # ou já foi rotulada por outro pedido.
                raise ConditionFailedError(operation) from exc
            if code == "TransactionInProgressException":
                raise _TransactionCanceledError([_TRANSACTION_IN_PROGRESS]) from exc
            if code == "TransactionCanceledException":
                reasons = [
                    r.get("Code", "None") for r in exc.response.get("CancellationReasons", [])
                ]
                raise _TransactionCanceledError(reasons) from exc
            raise provider_error(exc, service=SERVICE, operation=operation) from exc
        except BotoCoreError as exc:
            raise provider_error(exc, service=SERVICE, operation=operation) from exc


def invalid_item(item: Item, model: str, *, errors: int | None = None) -> ProviderError:
    """Item que não volta para o domínio: loga onde está (pk/sk) e vira `ProviderError`."""
    logger.error(
        "dynamodb_item_invalid",
        extra={"model": model, "pk": item.get(PK), "sk": item.get(SK), "errors": errors},
    )
    return ProviderError("item inválido no DynamoDB", details={"model": model})


def _model_from_item[M: BaseModel](model: type[M], item: Item) -> M:
    """Reconstrói um modelo do domínio ignorando os atributos de chave/índice."""
    try:
        return model.model_validate({k: v for k, v in item.items() if k in model.model_fields})
    except ValidationError as exc:
        raise invalid_item(item, model.__name__, errors=exc.error_count()) from exc


def decision_pk(decision_id: str) -> str:
    return f"DECISION#{decision_id}"


def stats_pk(prompt_version: str) -> str:
    return f"STATS#{prompt_version}"


def decision_item(decision: Decision, *, ttl_days: int) -> dict[str, object]:
    item: dict[str, object] = {
        PK: decision_pk(decision.id),
        SK: DECISION_SK,
        "entity": "decision",
        EXPIRES_AT: expires_at(decision.created_at, ttl_days),
        **decision.model_dump(mode="json", exclude_none=True),
    }
    if decision.is_pending_review:
        item[GSI_PK] = PENDING_PARTITION
        item[GSI_SK] = f"{sortable_timestamp(decision.created_at)}#{decision.id}"
    return item


def stats_from_item(item: Item) -> SufficientStats:
    try:
        return _stats_from_numbers(item)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise invalid_item(item, SufficientStats.__name__) from exc


def _stats_from_numbers(item: Item) -> SufficientStats:
    def number(name: str) -> Decimal:
        value = Decimal(item.get(name, 0))
        if not value.is_finite():
            raise ValueError(f"{name} não é finito")
        return value

    return SufficientStats(
        all_n=int(number("all_n")),
        all_judge_sum=float(number("all_judge_sum")),
        all_judge_sq_sum=float(number("all_judge_sq_sum")),
        lab_n=int(number("lab_n")),
        lab_human_sum=float(number("lab_human_sum")),
        lab_human_sq_sum=float(number("lab_human_sq_sum")),
        lab_judge_sum=float(number("lab_judge_sum")),
        lab_judge_sq_sum=float(number("lab_judge_sq_sum")),
        lab_cross_sum=float(number("lab_cross_sum")),
    )


class DynamoDecisionRepository:
    """Porta `DecisionRepository` no DynamoDB, com contadores agregados por versão."""

    def __init__(
        self,
        table: DynamoTable,
        *,
        ttl_days: int,
        stats_shards: int = DEFAULT_STATS_SHARDS,
    ) -> None:
        if stats_shards < 1:
            raise ValueError("stats_shards precisa ser >= 1")
        if ttl_days < 1:
            raise ValueError("ttl_days precisa ser >= 1")
        self._table = table
        self._shards = stats_shards
        self._ttl_days = ttl_days

    @classmethod
    def from_settings(cls, settings: Settings) -> DynamoDecisionRepository:
        return cls(DynamoTable.from_settings(settings), ttl_days=settings.decision_ttl_days)

    def add(self, decision: Decision) -> None:
        put: TransactWriteItemTypeDef = {
            "Put": {
                "TableName": self._table.name,
                "Item": serialize(decision_item(decision, ttl_days=self._ttl_days)),
                "ConditionExpression": f"attribute_not_exists({PK})",
            }
        }
        delta = SufficientStats.from_judged(float(decision.judge_approved))
        try:
            self._table.transact(
                [put, {"Update": self._stats_update(decision, delta)}],
                request_token=transaction_token("add", decision.id),
            )
        except ConditionFailedError as exc:
            raise ConflictError("decisão já existe", details={"id": decision.id}) from exc

    def get(self, decision_id: str) -> Decision | None:
        item = self._table.get(decision_pk(decision_id), DECISION_SK)
        return _model_from_item(Decision, item) if item else None

    def save_label(self, decision: Decision) -> None:
        """Grava o rótulo e soma os contadores na mesma transação: ou os dois, ou nenhum."""
        if decision.human_correct is None:
            raise ConflictError("rótulo ausente", details={"id": decision.id})
        delta = SufficientStats.from_label(
            float(decision.human_correct), float(decision.judge_approved)
        )
        try:
            self._table.transact(
                [
                    {"Update": self._label_update(decision)},
                    {"Update": self._stats_update(decision, delta)},
                ],
                request_token=transaction_token("label", decision.id),
            )
        except ConditionFailedError as exc:
            if self.get(decision.id) is None:
                raise NotFoundError("decisão não encontrada", details={"id": decision.id}) from exc
            raise ConflictError("decisão já rotulada", details={"id": decision.id}) from exc

    def pending_review(self, limit: int) -> list[Decision]:
        """Fila de revisão, mais recentes primeiro (o índice é eventualmente consistente)."""
        if limit <= 0:
            return []
        items = self._table.query(
            GSI_PK, PENDING_PARTITION, index=GSI_NAME, limit=limit, newest_first=True
        )
        return [_model_from_item(Decision, item) for item in items]

    def stats_for(self, prompt_version: str) -> SufficientStats:
        total = SufficientStats()
        for item in self._table.query(PK, stats_pk(prompt_version)):
            total += stats_from_item(item)
        return total

    def save_task_token(self, decision_id: str, task_token: str) -> None:
        """Guarda o token do Step Functions que espera a revisão humana desta decisão."""
        try:
            self._table.update(
                pk=decision_pk(decision_id),
                sk=DECISION_SK,
                expression=f"SET {TASK_TOKEN} = :token",
                values={":token": task_token},
                condition=f"attribute_exists({PK})",
            )
        except ConditionFailedError as exc:
            raise NotFoundError("decisão não encontrada", details={"id": decision_id}) from exc

    def task_token_for(self, decision_id: str) -> str | None:
        item = self._table.get(decision_pk(decision_id), DECISION_SK, projection=TASK_TOKEN)
        token = item.get(TASK_TOKEN) if item else None
        return token if isinstance(token, str) else None

    def _shard_key(self, decision_id: str) -> str:
        # crc32 é estável entre processos (o hash() do Python não é); só distribui carga.
        return f"SHARD#{zlib.crc32(decision_id.encode()) % self._shards:03d}"

    def _stats_update(self, decision: Decision, delta: SufficientStats) -> UpdateTypeDef:
        sums = {
            f.name: getattr(delta, f.name)
            for f in dataclasses.fields(SufficientStats)
            if getattr(delta, f.name)
        }
        additions = ", ".join(f"{name} :{name}" for name in sums)
        return {
            "TableName": self._table.name,
            "Key": serialize(
                {PK: stats_pk(decision.prompt_version), SK: self._shard_key(decision.id)}
            ),
            "UpdateExpression": f"SET prompt_version = :version ADD {additions}",
            "ExpressionAttributeValues": serialize(
                {":version": decision.prompt_version, **{f":{k}": v for k, v in sums.items()}}
            ),
        }

    def _label_update(self, decision: Decision) -> UpdateTypeDef:
        data = decision.model_dump(
            mode="json", include={"human_correct", "reviewer", "labeled_at"}, exclude_none=True
        )
        assignments = ", ".join(f"{name} = :{name}" for name in data)
        return {
            "TableName": self._table.name,
            "Key": serialize({PK: decision_pk(decision.id), SK: DECISION_SK}),
            "UpdateExpression": f"SET {assignments} REMOVE {GSI_PK}, {GSI_SK}",
            "ConditionExpression": _UNLABELED_DECISION,
            "ExpressionAttributeValues": serialize({f":{k}": v for k, v in data.items()}),
        }


class DynamoEventLog:
    """Porta `EventLog`: trilha de auditoria, mais recentes primeiro."""

    def __init__(self, table: DynamoTable) -> None:
        self._table = table

    @classmethod
    def from_settings(cls, settings: Settings) -> DynamoEventLog:
        return cls(DynamoTable.from_settings(settings))

    def append(self, event: Event) -> None:
        data = event.model_dump(mode="json")
        # Os detalhes viram texto JSON: voltam exatamente como foram (sem float virar Decimal).
        data["details"] = json.dumps(data["details"], ensure_ascii=False)
        self._table.put(
            {
                PK: EVENT_PARTITION,
                SK: f"{sortable_timestamp(event.at)}#{event.id}",
                "entity": "event",
                **data,
            }
        )

    def recent(self, limit: int) -> list[Event]:
        if limit <= 0:
            return []
        items = self._table.query(PK, EVENT_PARTITION, limit=limit, newest_first=True)
        return [_model_from_item(Event, {**item, "details": _details(item)}) for item in items]


def _details(item: Item) -> dict[str, Any]:
    """Os detalhes do evento, gravados como texto JSON. Ausente vale `{}`; ilegível é erro."""
    raw = item.get("details")
    if raw is None or raw == "":
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else None
    except ValueError as exc:
        raise invalid_item(item, Event.__name__) from exc
    if not isinstance(parsed, dict):
        raise invalid_item(item, Event.__name__)
    return parsed


class DynamoSnapshotStore:
    """Porta `SnapshotStore`: leituras horárias, devolvidas em ordem cronológica."""

    def __init__(self, table: DynamoTable) -> None:
        self._table = table

    @classmethod
    def from_settings(cls, settings: Settings) -> DynamoSnapshotStore:
        return cls(DynamoTable.from_settings(settings))

    def append(self, snapshot: QualitySnapshot) -> None:
        self._table.put(
            {
                PK: SNAPSHOT_PARTITION,
                SK: f"{sortable_timestamp(snapshot.at)}#{snapshot.prompt_version}",
                "entity": "snapshot",
                **snapshot.model_dump(mode="json", exclude_none=True),
            }
        )

    def recent(self, limit: int) -> list[QualitySnapshot]:
        if limit <= 0:
            return []
        items = self._table.query(PK, SNAPSHOT_PARTITION, limit=limit, newest_first=True)
        return [_model_from_item(QualitySnapshot, item) for item in reversed(items)]
