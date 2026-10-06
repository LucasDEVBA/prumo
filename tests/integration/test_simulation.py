"""A história completa do Prumo, de ponta a ponta, no simulador."""

import pytest

from prumo.config import Settings
from prumo.container import build_container
from prumo.simulation.engine import Simulator


@pytest.fixture
def sim():
    simulator = Simulator(build_container(Settings(seed=3)), labels_per_hour=40)
    simulator.advance(24)
    return simulator


def test_v1_should_be_healthy_after_warmup(sim):
    snapshot = sim.container.quality.read()
    assert snapshot.prompt_version == "v1"
    assert snapshot.status == "saudavel"


def test_bad_prompt_should_be_rolled_back_while_the_judge_stays_happy(sim):
    sim.container.quality.deploy("v2")
    reports = sim.advance(36)
    rolled = [r for r in reports if r.rolled_back_to]
    assert rolled, "o v2 deveria ter sido revertido"
    assert rolled[0].rolled_back_to == "v1"
    v2_reports = reports[: reports.index(rolled[0]) + 1]
    # O juiz nunca percebe: a nota crua dele fica acima da meta o tempo todo.
    assert all((r.snapshot.judge_rate or 0) > 0.85 for r in v2_reports)
    assert sim.container.prompts.active().id == "v1"


def test_good_prompt_should_not_be_rolled_back(sim):
    sim.container.quality.deploy("v3")
    reports = sim.advance(48)
    assert not any(r.rolled_back_to for r in reports)
    assert sim.container.prompts.active().id == "v3"


def test_simulator_should_refuse_real_providers():
    from prumo.config import Provider
    from prumo.domain.errors import ConflictError

    container = build_container(Settings())
    container.settings = Settings(provider=Provider.BEDROCK)
    with pytest.raises(ConflictError):
        Simulator(container)
