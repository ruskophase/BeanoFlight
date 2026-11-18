from copy import deepcopy
from decimal import Decimal

import pytest

from beanosort.capture import (CAPTURE_DEFAULT, SimulatedMonitor, expected_profile, fixture_particle,
                              simulated_destination, validate_particle)
from beanosort.quantities import DomainError


@pytest.mark.parametrize("p,expected", [("0", "pass_through"), ("1", "selected")])
def test_capture_extremes(p, expected):
    model = {**CAPTURE_DEFAULT, "probability": p}
    assert {simulated_destination("run", str(i), "seed", True, model) for i in range(100)} == {expected}
    assert simulated_destination("run", "x", "seed", False, model) == "pass_through"


def test_default_expected_example_and_all_histograms_conserve():
    rows = [{"payload": fixture_particle(i)} for i in range(10000)]
    profile = expected_profile(rows, CAPTURE_DEFAULT)
    assert profile["expected"] == {"selected": "1940.00", "pass_through": "8060.00", "unresolved": "0"}
    for stream, counts in profile["histograms"].items():
        for bins in counts.values():
            assert sum(Decimal(v) for v in bins.values()) == Decimal(profile["expected"][stream])
    assert profile["counts"]["physical_confirmed"] == 0
    assert profile["coverage"] == "unknown"
    assert profile["moisture"]["value"] is None
    assert "cannot establish" in profile["compliance"]


def test_stable_one_trial_and_joint_destination():
    rows = [fixture_particle(i) for i in range(400)]
    outcomes = {r["key"]: simulated_destination("run", r["key"], "seed", r["decision"] == "selected", CAPTURE_DEFAULT) for r in rows}
    reversed_outcomes = {r["key"]: simulated_destination("run", r["key"], "seed", r["decision"] == "selected", CAPTURE_DEFAULT) for r in reversed(rows)}
    assert outcomes == reversed_outcomes
    profile = expected_profile([{"payload": r, "simulated_destination": outcomes[r["key"]]} for r in rows], CAPTURE_DEFAULT, synthetic=True)
    for stream in ("selected", "pass_through"):
        count = profile["synthetic_monitor"]["counts"][stream]
        for histogram in profile["synthetic_monitor"]["histograms"][stream].values():
            assert sum(histogram.values()) == count
    assert sum(profile["synthetic_monitor"]["counts"][x] for x in ("selected", "pass_through")) == len(rows)
    changed_frames = deepcopy(rows)
    for r in changed_frames:
        r["frames_observed"], r["valve_count"] = 1000, 21
    assert expected_profile([{"payload": r} for r in rows], CAPTURE_DEFAULT) == expected_profile([{"payload": r} for r in changed_frames], CAPTURE_DEFAULT)


def test_decision_without_action_is_not_capture_trial():
    row = fixture_particle(0)
    row["action"] = "not_issued"
    profile = expected_profile([{"payload": row}], CAPTURE_DEFAULT)
    assert profile["expected"] == {"selected": "0", "pass_through": "0", "unresolved": "1"}
    assert profile["counts"]["decision_action_discrepancies"] == 1
    assert profile["counts"]["action_attempted"] == 0


def test_timeout_does_not_become_physical_miss_confirmation():
    events = SimulatedMonitor().events("pass_through", CAPTURE_DEFAULT)
    assert [e.kind for e in events] == ["expected_arrival", "timeout_unconfirmed"]
    assert events[-1].data["physical_confirmation"] is False
    success = SimulatedMonitor().events("selected", CAPTURE_DEFAULT)
    assert success[-1].kind == "association"
    assert success[-1].evidence == "simulation"
    assert not success[-1].data["physical_confirmation"]


def test_overlapping_defects_and_unknowns():
    row = fixture_particle(0)
    row["features"] = {"defects": ["broken", "mould"]}
    profile = expected_profile([{"payload": row}], CAPTURE_DEFAULT)
    selected = profile["histograms"]["selected"]
    assert selected["shape"] == {"unknown": "0.97"}
    assert selected["defects"] == {"broken": "0.97", "mould": "0.97"}


def test_fixture_cannot_be_validated_as_real():
    with pytest.raises(DomainError, match="cannot cross"):
        validate_particle(fixture_particle(0), "real")


@pytest.mark.parametrize("p", ["-1", "1.01", "NaN", "Infinity"])
def test_invalid_capture_parameter(p):
    with pytest.raises(DomainError):
        simulated_destination("run", "1", "seed", True, {**CAPTURE_DEFAULT, "probability": p})


def test_malformed_evidence_and_unspecified_clock_are_rejected():
    row = fixture_particle(0)
    row["evidence"] = {"features": "simulation"}
    with pytest.raises(DomainError, match="objects"):
        validate_particle(row, "simulation")
    row = fixture_particle(0)
    row["source_at"] = "2026-11-10T12:00:00"
    with pytest.raises(DomainError, match="offset-aware"):
        validate_particle(row, "simulation")


def test_real_trial_requires_controller_evidence():
    row = fixture_particle(0)
    row["evidence"] = {k: {"kind": "model_derived", "source": "test", "version": "v1"}
                       for k in ("features", "decision", "action")}
    with pytest.raises(DomainError, match="controller action"):
        validate_particle(row, "real")
