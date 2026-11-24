"""End-to-end check of the pipeline on the SIMULATED scenario (no device needed).

These tests verify that the code behaves as designed on synthetic data. They
say nothing about accuracy on the real load cell.
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest


from app import analytics, simulator as sim  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import Alert, Event, GroundTruth, SessionLocal, init_db  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def run_scenario(device_id: str, cycles_seconds: float | None = None):
    """Feed one simulated scenario cycle through a fresh pipeline with synthetic timestamps."""
    init_db()
    cfg = Settings()
    p = Pipeline(cfg, device_id, "simulated", None)
    states: list[tuple[str, str, str, bool, str]] = []
    total = sum(ph.seconds for ph in sim.SCENARIO) if cycles_seconds is None else cycles_seconds
    gt_id = None
    with SessionLocal() as db:
        p.start_bag(db, tare_g=sim.TARE_G, initial_g=sim.INITIAL_G, expected_rate_g_min=sim.NORMAL_RATE, now=T0)
        for s in sim.generate(1.0, 7):
            if s.t >= total:
                break
            now = T0 + timedelta(seconds=s.t)
            if s.phase_start:
                if gt_id:
                    db.get(GroundTruth, gt_id).ended_at = now
                g = GroundTruth(device_id=device_id, source="simulated", kind=s.label, started_at=now)
                db.add(g)
                db.flush()
                gt_id = g.id
            m = p.ingest(db, received_at=now, value_raw=None if s.value is None else str(s.value),
                         value_num=s.value, device_online=True,
                         api_error="simulated API failure" if s.value is None else None)
            states.append((s.label, m.flow_state, m.flow_state_status, m.low_volume, m.sensor_state))
        if gt_id:
            db.get(GroundTruth, gt_id).ended_at = T0 + timedelta(seconds=total)
        db.commit()
    return p, states


@pytest.fixture(scope="module")
def scenario():
    return run_scenario("bed-test")


def confirmed_in_phase(states, label, state):
    return any(t == label and f == state and st == "confirmed" for t, f, st, _, _ in states)


def test_normal_flow_detected(scenario):
    _, states = scenario
    normal = [f for t, f, st, _, _ in states if t == "NORMAL_FLOW" and st == "confirmed"]
    assert Counter(normal).most_common(1)[0][0] == "NORMAL_FLOW"


def test_flow_interruption_confirmed(scenario):
    _, states = scenario
    assert confirmed_in_phase(states, "FLOW_INTERRUPTION", "FLOW_INTERRUPTION")


def test_high_rate_confirmed(scenario):
    _, states = scenario
    assert confirmed_in_phase(states, "HIGH_MASS_LOSS_RATE", "HIGH_MASS_LOSS_RATE")


def test_api_failure_is_uncertain_never_normal(scenario):
    _, states = scenario
    during = [f for t, f, *_ in states if t == "SENSOR_UNCERTAIN"]
    assert during and all(f == "SENSOR_UNCERTAIN" for f in during)


def test_stuck_signal_flagged(scenario):
    _, states = scenario
    assert any(t == "POSSIBLE_STUCK_SIGNAL" and s == "POSSIBLE_STUCK_SIGNAL" for t, *_, s in states)


def test_noise_flagged(scenario):
    _, states = scenario
    assert any(t == "ABNORMAL_NOISE" and s == "ABNORMAL_NOISE" for t, *_, s in states)


def test_drift_flagged(scenario):
    _, states = scenario
    assert any(t == "POSSIBLE_DRIFT" and s == "POSSIBLE_DRIFT" for t, *_, s in states)


def test_low_volume_flagged(scenario):
    _, states = scenario
    assert any(lv for *_, lv, _ in states)


def test_bag_replacement_resets_baseline(scenario):
    p, states = scenario
    assert confirmed_in_phase(states, "BAG_REPLACEMENT", "BAG_REPLACEMENT")
    assert p.bag.initial_source == "bag_replacement"
    assert abs(p.bag.initial_g - sim.INITIAL_G) < 3
    assert p.bag.tare_g == sim.TARE_G


def test_no_contradictory_confirmed_flow_states(scenario):
    with SessionLocal() as db:
        evs = db.query(Event).filter(Event.device_id == "bed-test", Event.layer == "rule",
                                     Event.kind != "LOW_VOLUME").all()
    spans = [(e.started_at, e.ended_at or datetime.max, e.kind) for e in evs if e.confirmed_at]
    spans.sort()
    for (s1, e1, k1), (s2, e2, k2) in zip(spans, spans[1:]):
        assert s2 >= e1 or k1 == k2, f"{k1} and {k2} overlap"


def test_alerts_have_required_fields(scenario):
    with SessionLocal() as db:
        alerts = db.query(Alert).filter(Alert.device_id == "bed-test").all()
    assert alerts
    for a in alerts:
        assert a.event_type and a.created_at and a.severity and a.state in (
            "candidate", "confirmed", "acknowledged", "resolved")
        assert a.evidence_score is not None and a.sensor_status


def test_evaluation_runs_on_simulated_labels(scenario):
    with SessionLocal() as db:
        res = analytics.evaluate(db, "bed-test", "simulated")
        phys = analytics.evaluate(db, "bed-test", "physical")
    assert res["n_labels"] > 0 and res["event_level"]
    assert phys["n_labels"] == 0 and "note" in phys  # never invents physical metrics
