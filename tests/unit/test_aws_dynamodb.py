import logging
import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from prumo.adapters.aws.dynamodb import (
    GSI_NAME,
    DynamoDecisionRepository,
    DynamoEventLog,
    DynamoSnapshotStore,
    DynamoTable,
    decision_item,
    expires_at,
    serialize,
    sortable_timestamp,
    to_decimal,
    transaction_token,
)
from prumo.adapters.memory import InMemoryDecisionRepository, ManualClock, NoopReviewWorkflow
from prumo.config import Settings, Storage
from prumo.domain.errors import ConflictError, NotFoundError, ProviderError
from prumo.domain.models import Decision, Event, EventKind, LeadClass, QualitySnapshot
from prumo.services.decisions import ReviewService
from prumo.stats.ppi import SufficientStats

REGION = "us-east-1"
TABLE = "prumo-decisions-test"
START = datetime(2026, 10, 1, 12, tzinfo=UTC)
TTL_DAYS = 180


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def create_table(client) -> None:
    client.create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": name, "AttributeType": "S"}
            for name in ("pk", "sk", "gsi1pk", "gsi1sk")
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": GSI_NAME,
                "KeySchema": [
                    {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                    {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )


@pytest.fixture
def table():
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        create_table(client)
        yield DynamoTable(client, TABLE, sleep=lambda _seconds: None)


@pytest.fixture
def repo(table):
    return DynamoDecisionRepository(table, stats_shards=4, ttl_days=TTL_DAYS)


def make_decision(
    index: int,
    *,
    version: str = "v1",
    judge: bool = True,
    sampled: bool = True,
) -> Decision:
    return Decision(
        id=f"0199a000-0000-7000-8000-{index:012d}",
        lead_text=f"Sou professora, lead {index}.",
        prompt_version=version,
        predicted=LeadClass.MORNO,
        rationale="30 + 15 = 45",
        judge_approved=judge,
        judge_reason="confere",
        sampled_for_review=sampled,
        created_at=START + timedelta(minutes=index),
    )


def labeled(decision: Decision, correct: bool) -> Decision:
    return decision.model_copy(
        update={
            "human_correct": correct,
            "reviewer": "ana",
            "labeled_at": START + timedelta(days=1),
        }
    )


def test_should_read_back_the_same_decision_when_added(repo):
    decision = make_decision(1)
    repo.add(decision)
    assert repo.get(decision.id) == decision


def test_should_return_none_when_decision_does_not_exist(repo):
    assert repo.get("nao-existe") is None


def test_should_reject_duplicate_decision_without_counting_it_twice(repo):
    decision = make_decision(1)
    repo.add(decision)
    with pytest.raises(ConflictError):
        repo.add(decision)
    assert repo.stats_for("v1").all_n == 1


def test_should_aggregate_judge_counters_per_version_across_shards(repo):
    judges = [True, False, True, True, False, True]
    for index, judge in enumerate(judges):
        repo.add(make_decision(index, judge=judge))
    repo.add(make_decision(99, version="v2", judge=False))

    expected = SufficientStats.from_samples([float(j) for j in judges], [])
    assert repo.stats_for("v1") == expected
    assert repo.stats_for("v2").all_n == 1
    assert repo.stats_for("v9") == SufficientStats()


def test_should_save_label_and_add_label_counters_in_one_step(repo):
    decision = make_decision(1, judge=True)
    repo.add(decision)
    repo.save_label(labeled(decision, correct=False))

    stats = repo.stats_for("v1")
    assert stats == SufficientStats.from_judged(1.0) + SufficientStats.from_label(0.0, 1.0)
    stored = repo.get(decision.id)
    assert stored is not None
    assert stored.human_correct is False
    assert stored.reviewer == "ana"


def test_should_reject_second_label_and_keep_counters_untouched(repo):
    decision = make_decision(1)
    repo.add(decision)
    repo.save_label(labeled(decision, correct=True))
    with pytest.raises(ConflictError):
        repo.save_label(labeled(decision, correct=False))
    assert repo.stats_for("v1").lab_n == 1
    stored = repo.get(decision.id)
    assert stored is not None
    assert stored.human_correct is True


def test_should_raise_not_found_when_labeling_unknown_decision(repo):
    with pytest.raises(NotFoundError):
        repo.save_label(labeled(make_decision(1), correct=True))
    assert repo.stats_for("v1") == SufficientStats()


def test_should_reject_label_without_human_answer(repo):
    decision = make_decision(1)
    repo.add(decision)
    with pytest.raises(ConflictError):
        repo.save_label(decision)


def test_should_list_pending_reviews_newest_first_and_drop_labeled_ones(repo):
    for index in range(5):
        repo.add(make_decision(index))
    repo.add(make_decision(10, sampled=False))
    repo.save_label(labeled(make_decision(4), correct=True))

    pending = repo.pending_review(limit=3)

    assert [d.id for d in pending] == [make_decision(i).id for i in (3, 2, 1)]
    assert repo.pending_review(limit=0) == []


def test_should_store_and_read_task_token(repo):
    decision = make_decision(1)
    repo.add(decision)
    assert repo.task_token_for(decision.id) is None
    repo.save_task_token(decision.id, "token-abc")
    assert repo.task_token_for(decision.id) == "token-abc"
    assert repo.get(decision.id) == decision


def test_should_raise_not_found_when_saving_token_of_unknown_decision(repo):
    with pytest.raises(NotFoundError):
        repo.save_task_token("nao-existe", "token-abc")
    assert repo.task_token_for("nao-existe") is None


def test_should_match_memory_repository_for_the_same_operations(repo):
    memory = InMemoryDecisionRepository()
    for index in range(12):
        decision = make_decision(index, judge=index % 3 != 0, sampled=index % 2 == 0)
        repo.add(decision)
        memory.add(decision)
        if decision.sampled_for_review and index % 4 == 0:
            label = labeled(decision, correct=index % 8 == 0)
            repo.save_label(label)
            memory.save_label(label)

    assert repo.stats_for("v1") == memory.stats_for("v1")
    assert repo.pending_review(10) == memory.pending_review(10)


def test_should_append_events_and_return_newest_first_with_details(table):
    log = DynamoEventLog(table)
    first = Event(id="e1", at=START, kind=EventKind.DEPLOY, message="v2 no ar", details={})
    second = Event(
        id="e2",
        at=START + timedelta(hours=1),
        kind=EventKind.ROLLBACK,
        message="v2 → v1",
        details={"from": "v2", "to": "v1", "upper": 0.8123, "missing": None},
    )
    log.append(first)
    log.append(second)

    assert log.recent(10) == [second, first]
    assert log.recent(1) == [second]
    assert log.recent(0) == []


def test_should_append_snapshots_and_return_them_in_chronological_order(table):
    store = DynamoSnapshotStore(table)
    snapshots = [
        QualitySnapshot(
            at=START + timedelta(hours=hour),
            prompt_version="v1",
            status="saudavel",
            method="ppi_cs",
            point=0.9 - hour / 100,
            lower=0.1,
            upper=0.95,
            n_labels=30 + hour,
            n_decisions=400 * hour,
            slo_target=0.85,
        )
        for hour in range(4)
    ]
    for snapshot in snapshots:
        store.append(snapshot)

    assert store.recent(2) == snapshots[-2:]
    assert store.recent(10) == snapshots


def test_should_convert_float_to_its_shortest_decimal():
    assert to_decimal(0.1) == Decimal("0.1")
    assert to_decimal(1.0) == Decimal("1.0")
    with pytest.raises(ValueError, match="nan"):
        to_decimal(float("nan"))


def test_should_order_timestamps_as_strings_even_across_timezones():
    brasilia = timezone(timedelta(hours=-3))
    later_written_in_brasilia = datetime(2026, 10, 1, 10, tzinfo=brasilia)  # 13h UTC
    assert sortable_timestamp(later_written_in_brasilia) > sortable_timestamp(START)
    assert sortable_timestamp(datetime(2026, 10, 1, 12)) == sortable_timestamp(START)


def test_should_convert_missing_table_into_provider_error():
    with mock_aws():
        repo = DynamoDecisionRepository(
            DynamoTable(boto3.client("dynamodb", REGION), "nao-existe"), ttl_days=TTL_DAYS
        )
        with pytest.raises(ProviderError) as caught:
            repo.get("x")
    assert caught.value.details["error_code"] == "ResourceNotFoundException"


def stubbed_table(sleeps: list[float]) -> tuple[DynamoTable, Stubber]:
    client = boto3.client("dynamodb", region_name=REGION)
    return DynamoTable(client, TABLE, sleep=sleeps.append), Stubber(client)


def add_cancellation(stubber: Stubber, *codes: str) -> None:
    stubber.add_client_error(
        "transact_write_items",
        service_error_code="TransactionCanceledException",
        service_message="Transaction cancelled",
        modeled_fields={"CancellationReasons": [{"Code": code} for code in codes]},
    )


def test_should_retry_transaction_conflict_and_then_succeed():
    sleeps: list[float] = []
    table, stubber = stubbed_table(sleeps)
    add_cancellation(stubber, "None", "TransactionConflict")
    stubber.add_response("transact_write_items", {})
    with stubber:
        DynamoDecisionRepository(table, ttl_days=TTL_DAYS).add(make_decision(1))
        stubber.assert_no_pending_responses()
    assert len(sleeps) == 1


def test_should_give_up_after_repeated_transaction_conflicts():
    sleeps: list[float] = []
    table, stubber = stubbed_table(sleeps)
    for _ in range(3):
        add_cancellation(stubber, "TransactionConflict", "None")
    with stubber:
        with pytest.raises(ProviderError) as caught:
            DynamoDecisionRepository(table, ttl_days=TTL_DAYS).add(make_decision(1))
        stubber.assert_no_pending_responses()
    assert caught.value.details["reasons"] == ["TransactionConflict", "None"]
    assert len(sleeps) == 2


def test_should_retry_when_the_same_transaction_is_still_in_progress():
    sleeps: list[float] = []
    table, stubber = stubbed_table(sleeps)
    stubber.add_client_error(
        "transact_write_items", service_error_code="TransactionInProgressException"
    )
    stubber.add_response("transact_write_items", {})
    with stubber:
        DynamoDecisionRepository(table, ttl_days=TTL_DAYS).add(make_decision(1))
        stubber.assert_no_pending_responses()
    assert len(sleeps) == 1


@pytest.mark.parametrize(
    ("act", "already_stored", "expected"),
    [
        (lambda repo: repo.add(make_decision(1)), None, "decisão já existe"),
        (
            lambda repo: repo.save_label(labeled(make_decision(1), correct=False)),
            labeled(make_decision(1), correct=True),
            "decisão já rotulada",
        ),
    ],
    ids=["add", "label"],
)
def test_should_turn_idempotent_parameter_mismatch_into_conflict(act, already_stored, expected):
    """Mesmo token com outros valores: outro pedido já gravou essa decisão (ou esse rótulo)."""
    table, stubber = stubbed_table([])
    stubber.add_client_error(
        "transact_write_items", service_error_code="IdempotentParameterMismatchException"
    )
    if already_stored is not None:
        item = serialize(decision_item(already_stored, ttl_days=TTL_DAYS))
        stubber.add_response("get_item", {"Item": item})
    repo = DynamoDecisionRepository(table, ttl_days=TTL_DAYS)
    with stubber:
        with pytest.raises(ConflictError, match=expected):
            act(repo)
        stubber.assert_no_pending_responses()


def capture_transactions(action, responses: int = 1) -> list[dict]:
    """Roda `action(repo)` com o DynamoDB stubado e devolve os parâmetros de cada transação."""
    client = boto3.client("dynamodb", region_name=REGION)
    captured: list[dict] = []
    client.meta.events.register(
        "provide-client-params.dynamodb.TransactWriteItems",
        lambda params, **_: captured.append(params),
    )
    stubber = Stubber(client)
    for _ in range(responses):
        stubber.add_response("transact_write_items", {})
    repo = DynamoDecisionRepository(
        DynamoTable(client, TABLE, sleep=lambda _s: None), stats_shards=1, ttl_days=TTL_DAYS
    )
    with stubber:
        action(repo)
        stubber.assert_no_pending_responses()
    return captured


def capture_transaction(decision: Decision) -> list[dict]:
    return capture_transactions(lambda repo: repo.add(decision))[0]["TransactItems"]


def test_should_send_conditional_put_and_atomic_add_in_one_transaction():
    put, update = capture_transaction(make_decision(1, judge=False))

    assert put["Put"]["ConditionExpression"] == "attribute_not_exists(pk)"
    assert put["Put"]["Item"]["gsi1pk"] == {"S": "REVIEW#PENDING"}
    assert update["Update"]["Key"] == {"pk": {"S": "STATS#v1"}, "sk": {"S": "SHARD#000"}}
    assert update["Update"]["UpdateExpression"] == "SET prompt_version = :version ADD all_n :all_n"
    assert update["Update"]["ExpressionAttributeValues"][":all_n"] == {"N": "1"}


def test_should_send_client_request_token():
    decision = make_decision(1)
    label = labeled(decision, correct=True)

    sent = capture_transactions(
        lambda repo: (repo.add(decision), repo.add(decision), repo.save_label(label)), responses=3
    )

    tokens = [params["ClientRequestToken"] for params in sent]
    assert tokens[0] == tokens[1] == transaction_token("add", decision.id)
    assert tokens[2] == transaction_token("label", decision.id)
    assert tokens[0] != tokens[2]
    assert all(uuid.UUID(token).version == 5 for token in tokens)
    assert transaction_token("add", make_decision(2).id) != tokens[0]


def test_should_reuse_the_same_token_when_retrying_a_conflicted_transaction():
    client = boto3.client("dynamodb", region_name=REGION)
    tokens: list[str] = []
    client.meta.events.register(
        "provide-client-params.dynamodb.TransactWriteItems",
        lambda params, **_: tokens.append(params["ClientRequestToken"]),
    )
    stubber = Stubber(client)
    add_cancellation(stubber, "None", "TransactionConflict")
    stubber.add_response("transact_write_items", {})
    with stubber:
        DynamoDecisionRepository(
            DynamoTable(client, TABLE, sleep=lambda _s: None), ttl_days=TTL_DAYS
        ).add(make_decision(1))
        stubber.assert_no_pending_responses()
    assert tokens == [transaction_token("add", make_decision(1).id)] * 2


def test_should_set_expires_at_on_decisions(table, repo):
    decision = make_decision(1)
    repo.add(decision)
    DynamoEventLog(table).append(
        Event(id="e1", at=START, kind=EventKind.DEPLOY, message="v1 no ar")
    )

    stored = table.get(f"DECISION#{decision.id}", "DECISION")
    expected = int((decision.created_at + timedelta(days=TTL_DAYS)).timestamp())
    assert stored["expires_at"] == expected == expires_at(decision.created_at, TTL_DAYS)
    stats = table.query("pk", "STATS#v1")
    events = table.query("pk", "EVENT")
    assert stats
    assert events
    assert all("expires_at" not in item for item in [*stats, *events])
    assert repo.get(decision.id) == decision  # o atributo de TTL não vaza para o domínio


def test_should_read_naive_creation_time_as_utc_for_the_ttl():
    naive = datetime(2026, 10, 1, 12)
    assert expires_at(naive, 1) == int(datetime(2026, 10, 2, 12, tzinfo=UTC).timestamp())


def test_should_return_label_and_reviewer_so_a_repeated_review_is_idempotent(repo):
    decision = make_decision(1)
    repo.add(decision)
    reviews = ReviewService(
        repository=repo, review_workflow=NoopReviewWorkflow(), clock=ManualClock(START)
    )

    first = reviews.submit(decision.id, correct=False, reviewer="ana")
    stored = repo.get(decision.id)
    repeated = reviews.submit(decision.id, correct=False, reviewer="ana")

    assert stored is not None
    assert (stored.human_correct, stored.reviewer, stored.labeled_at) == (False, "ana", START)
    assert repeated == first == stored
    assert repo.stats_for("v1").lab_n == 1
    with pytest.raises(ConflictError):
        reviews.submit(decision.id, correct=True, reviewer="ana")


def test_should_build_dynamo_adapters_from_settings():
    settings = Settings(
        storage=Storage.DYNAMODB,
        decisions_table=TABLE,
        aws_region="sa-east-1",
        decision_ttl_days=30,
    )
    repository = DynamoDecisionRepository.from_settings(settings)
    assert repository._ttl_days == 30
    events = DynamoEventLog.from_settings(settings)
    snapshots = DynamoSnapshotStore.from_settings(settings)
    for adapter in (repository, events, snapshots):
        table = adapter._table
        assert table.name == TABLE
        config = table._client.meta.config
        assert table._client.meta.region_name == "sa-east-1"
        assert config.retries["mode"] == "adaptive"
        assert config.read_timeout == 5


def test_should_raise_provider_error_when_stored_item_is_corrupted(table, repo):
    table.put({"pk": "DECISION#ruim", "sk": "DECISION", "id": "ruim", "prompt_version": "x"})
    with pytest.raises(ProviderError, match="item inválido"):
        repo.get("ruim")


def corrupted_event(details: str) -> dict:
    return {
        "pk": "EVENT",
        "sk": "2026-10-01T12:00:00.000000Z#e1",
        "id": "e1",
        "at": START.isoformat(),
        "kind": "deploy",
        "message": "v2 no ar",
        "details": details,
    }


@pytest.mark.parametrize(
    ("item", "read"),
    [
        (
            {"pk": "STATS#v1", "sk": "SHARD#000", "all_n": "nao-e-numero"},
            lambda table: DynamoDecisionRepository(table, ttl_days=TTL_DAYS).stats_for("v1"),
        ),
        (
            {"pk": "STATS#v1", "sk": "SHARD#000", "lab_human_sum": "NaN"},
            lambda table: DynamoDecisionRepository(table, ttl_days=TTL_DAYS).stats_for("v1"),
        ),
        (corrupted_event("{nao-e-json"), lambda table: DynamoEventLog(table).recent(5)),
        (corrupted_event("[1, 2]"), lambda table: DynamoEventLog(table).recent(5)),
    ],
    ids=["decimal-invalido", "decimal-nao-finito", "json-invalido", "json-nao-objeto"],
)
def test_should_raise_provider_error_on_corrupted_item(table, caplog, item, read):
    table.put(item)

    with caplog.at_level(logging.ERROR), pytest.raises(ProviderError, match="item inválido"):
        read(table)

    logged = [r for r in caplog.records if r.getMessage() == "dynamodb_item_invalid"]
    assert [(r.pk, r.sk) for r in logged] == [(item["pk"], item["sk"])]
