import math
import random

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from prumo.stats.ppi import EstimationMethod, SufficientStats, estimate
from prumo.stats.sequences import asymptotic_cs_radius

ALL_METHODS = list(EstimationMethod)


def _sample(rng: np.random.Generator, n_all: int, n_lab: int, acc: float, hit: float, miss: float):
    """Gera (juiz em todas, pares humano/juiz na amostra) com um juiz de viés conhecido."""
    truth = rng.random(n_all) < acc
    judge = np.where(truth, rng.random(n_all) < hit, rng.random(n_all) < miss)
    idx = rng.choice(n_all, size=n_lab, replace=False)
    labeled = [(float(truth[i]), float(judge[i])) for i in idx]
    return [float(j) for j in judge], labeled


def test_should_return_none_when_fewer_than_two_labels():
    stats = SufficientStats.from_samples([1.0, 0.0, 1.0], [(1.0, 1.0)])
    assert estimate(stats) is None


def test_should_reject_alpha_outside_unit_interval():
    stats = SufficientStats.from_samples([1.0] * 5, [(1.0, 1.0), (0.0, 1.0)])
    with pytest.raises(ValueError, match="alpha"):
        estimate(stats, alpha=1.5)


def test_should_reject_values_outside_zero_one():
    with pytest.raises(ValueError, match="judge"):
        SufficientStats.from_judged(1.2)


def test_human_only_should_match_plain_mean():
    labeled = [(1.0, 1.0), (0.0, 1.0), (1.0, 0.0), (1.0, 1.0)]
    stats = SufficientStats.from_samples([1.0] * 10, labeled)
    est = estimate(stats, method=EstimationMethod.HUMAN_ONLY)
    assert est is not None
    assert est.point == pytest.approx(0.75)
    assert est.lam == 0.0


def test_ppi_should_equal_judge_mean_when_judge_is_perfect():
    judge_all = [1.0] * 900 + [0.0] * 100
    labeled = [(1.0, 1.0)] * 45 + [(0.0, 0.0)] * 5
    est = estimate(SufficientStats.from_samples(judge_all, labeled), method=EstimationMethod.PPI_CI)
    assert est is not None
    assert est.point == pytest.approx(0.9, abs=1e-9)


def test_ppi_should_correct_an_optimistic_judge():
    """Juiz aprova 93%, mas na amostra ele aprova erros: a estimativa precisa cair."""
    rng = np.random.default_rng(7)
    judge_all, labeled = _sample(rng, 20_000, 600, acc=0.80, hit=0.99, miss=0.67)
    est = estimate(SufficientStats.from_samples(judge_all, labeled), method=EstimationMethod.PPI_CI)
    assert est is not None
    assert est.judge_rate > 0.9
    assert est.lower < 0.80 < est.upper


def test_sufficient_stats_addition_should_be_associative():
    a = SufficientStats.from_label(1.0, 0.0)
    b = SufficientStats.from_judged(1.0)
    c = SufficientStats.from_label(0.0, 1.0)
    assert (a + b) + c == a + (b + c)


@settings(max_examples=200, deadline=None)
@given(
    judge_all=st.lists(st.sampled_from([0.0, 1.0]), min_size=5, max_size=80),
    labeled=st.lists(
        st.tuples(st.sampled_from([0.0, 1.0]), st.sampled_from([0.0, 1.0])),
        min_size=2,
        max_size=40,
    ),
    method=st.sampled_from(ALL_METHODS),
)
def test_estimate_should_stay_in_unit_interval_and_be_ordered(judge_all, labeled, method):
    judge_all = judge_all + [j for _, j in labeled]
    est = estimate(SufficientStats.from_samples(judge_all, labeled), method=method)
    assert est is not None
    assert 0.0 <= est.lower <= est.point <= est.upper <= 1.0
    assert 0.0 <= est.lam <= 1.0


@settings(max_examples=100, deadline=None)
@given(
    n=st.integers(min_value=1, max_value=5000),
    variance=st.floats(min_value=0.0, max_value=0.25),
    alpha=st.floats(min_value=0.001, max_value=0.2),
)
def test_cs_radius_should_be_positive_and_shrink_with_more_data(n, variance, alpha):
    r_now = asymptotic_cs_radius(n, variance, alpha)
    r_later = asymptotic_cs_radius(n * 4, variance, alpha)
    assert r_now > 0
    assert r_later < r_now


def test_cs_radius_should_be_infinite_without_data():
    assert math.isinf(asymptotic_cs_radius(0, 0.1, 0.05))


@pytest.mark.slow
def test_ppi_ci_should_cover_the_truth_about_95_percent_of_the_time():
    rng = np.random.default_rng(2026)
    acc, hits, reps = 0.82, 0, 400
    target = acc  # rótulo humano sem ruído
    for _ in range(reps):
        judge_all, labeled = _sample(rng, 4000, 200, acc=acc, hit=0.97, miss=0.5)
        est = estimate(
            SufficientStats.from_samples(judge_all, labeled), method=EstimationMethod.PPI_CI
        )
        assert est is not None
        hits += est.lower <= target <= est.upper
    assert 0.92 <= hits / reps <= 0.98


@pytest.mark.slow
def test_ppi_cs_should_hold_at_every_hourly_look():
    """Consultada a cada 20 rótulos, a faixa erra no máximo alpha (com folga de Monte Carlo)."""
    rng = random.Random(11)
    alpha, reps, misses = 0.05, 300, 0
    acc, hit, miss = 0.78, 0.97, 0.7
    for _ in range(reps):
        stats = SufficientStats()
        failed = False
        for hour in range(1, 31):
            for i in range(400):
                truth = rng.random() < acc
                judge = float(rng.random() < (hit if truth else miss))
                stats += SufficientStats.from_judged(judge)
                if i < 20:
                    stats += SufficientStats.from_label(float(truth), judge)
            est = estimate(stats, alpha=alpha, method=EstimationMethod.PPI_CS)
            if hour >= 2 and est is not None and not est.lower <= acc <= est.upper:
                failed = True
                break
        misses += failed
    assert misses / reps <= alpha + 0.02


@pytest.mark.slow
def test_ppi_should_be_tighter_than_humans_alone_with_a_good_judge():
    rng = np.random.default_rng(5)
    judge_all, labeled = _sample(rng, 20_000, 300, acc=0.85, hit=0.97, miss=0.1)
    stats = SufficientStats.from_samples(judge_all, labeled)
    ppi = estimate(stats, method=EstimationMethod.PPI_CI)
    human = estimate(stats, method=EstimationMethod.HUMAN_ONLY)
    assert ppi is not None
    assert human is not None
    assert ppi.half_width < human.half_width * 0.8
