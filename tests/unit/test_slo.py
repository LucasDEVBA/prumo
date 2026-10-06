import pytest

from prumo.stats.ppi import Estimate, EstimationMethod
from prumo.stats.slo import Slo, SloStatus, should_rollback


def _est(point: float, lower: float, upper: float, n: int = 100) -> Estimate:
    return Estimate(
        method=EstimationMethod.PPI_CS,
        point=point,
        lower=lower,
        upper=upper,
        n_labels=n,
        n_decisions=n * 20,
        judge_rate=0.9,
    )


@pytest.mark.parametrize(
    ("estimate", "expected"),
    [
        (None, SloStatus.COLLECTING),
        (_est(0.9, 0.86, 0.94, n=10), SloStatus.COLLECTING),
        (_est(0.9, 0.86, 0.94), SloStatus.HEALTHY),
        (_est(0.86, 0.80, 0.92), SloStatus.AT_RISK),
        (_est(0.78, 0.72, 0.84), SloStatus.BREACHED),
    ],
)
def test_status_should_follow_the_interval_against_the_target(estimate, expected):
    assert Slo(target=0.85).status(estimate) is expected


def test_burn_rate_should_be_one_when_spending_exactly_the_budget():
    assert Slo(target=0.85).burn_rate(_est(0.85, 0.8, 0.9)) == pytest.approx(1.0)
    assert Slo(target=0.85).burn_rate(None) is None


def test_should_rollback_only_after_consecutive_breaches():
    slo = Slo(breaches_to_rollback=2)
    breached, risk = SloStatus.BREACHED, SloStatus.AT_RISK
    assert not should_rollback([breached], slo)
    assert not should_rollback([breached, risk], slo)
    assert should_rollback([risk, breached, breached], slo)


def test_slo_should_reject_invalid_target():
    with pytest.raises(ValueError, match="meta"):
        Slo(target=1.2)
