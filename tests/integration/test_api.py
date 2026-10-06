import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from prumo.api.app import create_app
from prumo.config import Settings
from prumo.container import build_container


def _client(**overrides) -> TestClient:
    settings = Settings(seed=4, simulation_warmup_hours=24, **overrides)
    return TestClient(create_app(build_container(settings)))


@pytest.fixture
def client():
    with _client() as c:
        yield c


def test_health_should_report_provider(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["provider"] == "simulated"
    assert response.headers["X-Correlation-ID"]
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_dashboard_should_render(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Taxa real de acerto" in response.text


def test_quality_should_be_healthy_after_warmup(client):
    data = client.get("/api/v1/quality").json()
    assert data["current"]["prompt_version"] == "v1"
    assert data["current"]["status"] == "saudavel"
    assert len(data["history"]) == 24


def test_deploying_v2_should_end_in_automatic_rollback(client):
    assert client.post("/api/v1/prompts/v2/deploy").status_code == 200
    for _ in range(6):
        client.post("/api/v1/simulation/advance", json={"hours": 6})
        if client.get("/api/v1/prompts").json()["active"]["id"] == "v1":
            break
    kinds = [e["kind"] for e in client.get("/api/v1/events").json()["items"]]
    assert "rollback" in kinds
    assert client.get("/api/v1/prompts").json()["active"]["id"] == "v1"


def test_review_should_reveal_answer_key_in_simulation(client):
    queue = client.get("/api/v1/review/queue?limit=1").json()
    assert queue, "o simulador deixa itens para revisão humana"
    response = client.post(f"/api/v1/review/{queue[0]['id']}", json={"correct": True})
    assert response.status_code == 200
    assert response.json()["expected"] in {"QUENTE", "MORNO", "FRIO"}
    same = client.post(f"/api/v1/review/{queue[0]['id']}", json={"correct": True})
    assert same.status_code == 200, "reenviar o mesmo rótulo é idempotente"
    other = client.post(f"/api/v1/review/{queue[0]['id']}", json={"correct": False})
    assert other.status_code == 409
    assert other.json()["code"] == "conflict"


def test_create_decision_should_accept_synthetic_lead(client):
    text = "Sou médica, tenho 45 anos. Movimento em média R$ 26.000 por mês, sem empréstimos."
    response = client.post("/api/v1/decisions", json={"lead_text": text})
    assert response.status_code == 201
    assert response.json()["prompt_version"] == "v1"


def test_create_decision_should_explain_free_text_in_simulated_mode(client):
    response = client.post("/api/v1/decisions", json={"lead_text": "quero investir"})
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_input"


def test_unknown_fields_should_be_rejected(client):
    response = client.post("/api/v1/decisions", json={"lead_text": "x", "prompt_version": "v9"})
    assert response.status_code == 422
    assert response.json()["details"]["errors"]


def test_unknown_prompt_should_return_404(client):
    response = client.post("/api/v1/prompts/v9/deploy")
    assert response.status_code == 404
    assert response.json()["correlation_id"]


def test_mutations_should_require_token_when_configured():
    with _client(admin_token=SecretStr("segredo-de-teste")) as c:
        assert c.post("/api/v1/prompts/v2/deploy").status_code == 401
        ok = c.post("/api/v1/prompts/v2/deploy", headers={"X-Prumo-Token": "segredo-de-teste"})
        assert ok.status_code == 200


def test_labels_per_hour_should_be_validated(client):
    response = client.put("/api/v1/simulation/settings", json={"labels_per_hour": 9999})
    assert response.status_code == 422


def test_reset_should_restore_v1(client):
    client.post("/api/v1/prompts/v3/deploy")
    assert client.post("/api/v1/simulation/reset").status_code == 200
    assert client.get("/api/v1/prompts").json()["active"]["id"] == "v1"


def test_review_routes_should_fail_closed_outside_the_simulator(monkeypatch):
    from prumo.api.app import require_admin
    from prumo.domain.errors import AuthorizationError

    class _State:
        class container:  # noqa: N801
            admin_token = None
            is_simulated = False

    with pytest.raises(AuthorizationError):
        require_admin(_State(), None)  # type: ignore[arg-type]


def test_manual_rollback_route_should_restore_previous_version(client):
    client.post("/api/v1/prompts/v3/deploy")
    response = client.post("/api/v1/prompts/rollback")
    assert response.status_code == 200
    assert response.json()["id"] == "v1"
    assert client.post("/api/v1/prompts/rollback").status_code == 409


def test_docs_should_load_swagger_assets_under_a_scoped_csp(client):
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "https://cdn.jsdelivr.net" in docs.headers["Content-Security-Policy"]
    # O painel continua com a política estrita: nada de script de terceiros nem inline.
    panel = client.get("/")
    assert "cdn.jsdelivr.net" not in panel.headers["Content-Security-Policy"]
    assert "'unsafe-inline'" not in panel.headers["Content-Security-Policy"].split("style-src")[0]
