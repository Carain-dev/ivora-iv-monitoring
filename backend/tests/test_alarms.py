"""Alarm manager behaviour: priorities, persistence, recovery, repeat suppression.

All inputs here are SIMULATED (seeded random numbers rounded to whole grams to
mimic Blynk V1), except `LIVE_DIGITISED`, which is the 2026-10-11 live
static-load run digitised from a dashboard screenshot. Passing these tests
shows the logic works as designed; it is not evidence of real-world alarm
performance.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, inspect, select, text

from app.alarms import buzzer_for
from app.config import Settings
from app.db import Alert, SessionLocal, init_db
from app.pipeline import Pipeline

T0 = datetime(2026, 2, 1, tzinfo=timezone.utc)
SERIES = Path(__file__).parent / "data" / "live_static_2026-10-11.json"


def device_values(seconds: int, weight_at, seed: int, noise_g: float = 0.5, update_s: int = 4, poll_s: int = 2):
    """Whole-gram readings as Blynk returns them: the device updates every
    `update_s` s and we poll every `poll_s` s, so values repeat."""
    rng = random.Random(seed)
    cur = None
    for t in range(0, seconds, poll_s):
        if t % update_s == 0:
            cur = round(weight_at(t) + rng.gauss(0, noise_g))
        yield t, cur


class Rig:
    def __init__(self, name: str, bag: dict | None = None, **cfg_overrides):
        init_db()
        self.cfg = Settings()
        for k, v in cfg_overrides.items():
            setattr(self.cfg, k, v)
        self.name = name
        self.p = Pipeline(self.cfg, name, "simulated", None)
        self.t = 0
        if bag:
            with SessionLocal() as db:
                self.p.start_bag(db, now=T0, **bag)
                db.commit()

    def feed(self, readings, online: bool = True):
        """readings: iterable of (seconds offset from now, value or None)."""
        start = self.t
        with SessionLocal() as db:
            for dt, v in readings:
                self.t = start + dt
                self.p.ingest(db, received_at=T0 + timedelta(seconds=self.t),
                              value_raw=None if v is None else str(v), value_num=v,
                              device_online=online if v is not None else False)
            db.commit()
        self.t += 2

    def alerts(self, **where):
        with SessionLocal() as db:
            q = select(Alert).where(Alert.device_id == self.name)
            for k, v in where.items():
                q = q.where(getattr(Alert, k) == v)
            return db.scalars(q.order_by(Alert.id)).all()

    def active(self):
        return [a for a in self.alerts() if a.state in ("confirmed", "acknowledged")]


STATIC_BAG = {"tare_g": 20.0, "initial_g": 90.0, "expected_rate_g_min": 4.0}  # 79 g reads as ~84 % left


def test_static_whole_gram_load_gives_one_interruption_alarm_and_no_drift():
    """A fixed weight is a genuine 'no flow' condition. Before this change the
    same input produced 88 interruption alerts and 47 drift alerts in an hour."""
    rig = Rig("alarm-static", bag=STATIC_BAG)
    rig.feed(device_values(3600, lambda t: 79.0, seed=1))
    alerts = rig.alerts()
    assert [(a.event_type, a.severity) for a in alerts] == [("FLOW_INTERRUPTION", "high")]
    assert alerts[0].state == "confirmed" and (alerts[0].recurrences or 0) == 0


def test_static_load_without_bag_session_is_not_alarmed():
    rig = Rig("alarm-nobag")
    rig.feed(device_values(1800, lambda t: 79.0, seed=3))
    assert rig.alerts() == []


def test_normal_whole_gram_drain_gives_no_alarm():
    rig = Rig("alarm-drain", bag={"tare_g": 40.0, "initial_g": 560.0, "expected_rate_g_min": 4.0})
    rig.feed(device_values(3600, lambda t: 540.0 - 4.0 * t / 60, seed=2))
    assert rig.alerts() == []


def test_live_digitised_static_run_raises_no_sensor_alarm():
    vals = json.loads(SERIES.read_text())["values_g"]
    rig = Rig("alarm-live", bag=STATIC_BAG)
    rig.feed([(2 * i, v) for i, v in enumerate(vals)])
    kinds = {a.event_type for a in rig.alerts()}
    assert not kinds & {"POSSIBLE_DRIFT", "SENSOR_UNCERTAIN", "ABNORMAL_NOISE", "POSSIBLE_STUCK_SIGNAL"}


def test_missing_data_never_clears_an_alarm_and_recovery_needs_flow():
    rig = Rig("alarm-latch", bag={"tare_g": 40.0, "initial_g": 560.0, "expected_rate_g_min": 4.0})
    rig.feed(device_values(300, lambda t: 400.0, seed=4))  # clamp closed
    assert [a.event_type for a in rig.active()] == ["FLOW_INTERRUPTION"]
    rig.feed([(t, None) for t in range(0, 120, 2)])  # device offline for 2 minutes
    kinds = {a.event_type: a for a in rig.active()}
    assert set(kinds) == {"FLOW_INTERRUPTION", "SENSOR_UNCERTAIN"}  # latched, plus data-loss alarm
    assert kinds["SENSOR_UNCERTAIN"].severity == "medium"
    rig.feed(device_values(240, lambda t: 400.0 - 4.0 * t / 60, seed=5))  # flow restored
    assert rig.active() == []
    resolved = rig.alerts(event_type="FLOW_INTERRUPTION")[0]
    assert "recovered" in resolved.message


def test_recurrence_reopens_the_same_alarm():
    rig = Rig("alarm-repeat", bag={"tare_g": 40.0, "initial_g": 560.0, "expected_rate_g_min": 4.0})
    w = {"v": 450.0}

    def stop_then_flow(seconds, flowing):
        base = w["v"]
        rig.feed(device_values(seconds, (lambda t: base - 4.0 * t / 60) if flowing else (lambda t: base), seed=seconds))
        if flowing:
            w["v"] = base - 4.0 * seconds / 60

    stop_then_flow(240, False)
    stop_then_flow(150, True)
    assert rig.active() == []
    stop_then_flow(240, False)
    alerts = rig.alerts(event_type="FLOW_INTERRUPTION")
    assert len(alerts) == 1 and alerts[0].recurrences == 1 and alerts[0].state == "confirmed"


def test_acknowledge_silences_and_high_alarm_resounds():
    rig = Rig("alarm-ack", bag={"tare_g": 40.0, "initial_g": 560.0, "expected_rate_g_min": 4.0}, alarm_resound_s=120.0)
    rig.feed(device_values(240, lambda t: 400.0, seed=6))
    a = rig.active()[0]
    assert buzzer_for([a])["priority"] == "high"
    with SessionLocal() as db:
        x = db.get(Alert, a.id)
        x.state, x.acknowledged_at = "acknowledged", T0 + timedelta(seconds=rig.t)
        db.commit()
    rig.feed(device_values(60, lambda t: 400.0, seed=7))
    assert rig.active()[0].state == "acknowledged" and buzzer_for(rig.active())["code"] == 0
    rig.feed(device_values(90, lambda t: 400.0, seed=8))
    assert rig.active()[0].state == "confirmed"


def test_manual_resolve_is_not_reraised_while_condition_persists():
    rig = Rig("alarm-manual", bag={"tare_g": 40.0, "initial_g": 560.0, "expected_rate_g_min": 4.0})
    rig.feed(device_values(240, lambda t: 400.0, seed=9))
    with SessionLocal() as db:
        db.get(Alert, rig.active()[0].id).state = "resolved"
        db.commit()
    rig.feed(device_values(300, lambda t: 400.0, seed=10))
    assert rig.active() == []


def test_new_bag_resolves_infusion_alarms():
    rig = Rig("alarm-newbag", bag={"tare_g": 40.0, "initial_g": 560.0, "expected_rate_g_min": 4.0})
    rig.feed(device_values(240, lambda t: 400.0, seed=11))
    assert rig.active()
    with SessionLocal() as db:
        rig.p.start_bag(db, tare_g=40.0, initial_g=560.0, expected_rate_g_min=4.0, now=T0 + timedelta(seconds=rig.t))
        db.commit()
    assert rig.active() == [] and "new bag" in rig.alerts()[0].message


def test_buzzer_picks_highest_unacknowledged_priority():
    def a(i, sev, state="confirmed"):
        return Alert(id=i, severity=sev, state=state, event_type="X")

    assert buzzer_for([])["code"] == 0
    assert buzzer_for([a(1, "low"), a(2, "info")])["code"] == 0
    assert buzzer_for([a(1, "medium"), a(2, "high")])["priority"] == "high"
    assert buzzer_for([a(1, "medium"), a(2, "high", "acknowledged")])["priority"] == "medium"


def test_old_database_gets_new_alert_column(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE alert (id INTEGER PRIMARY KEY, event_id INTEGER, device_id VARCHAR(64), "
                       "source VARCHAR(16), event_type VARCHAR(32), label TEXT, state VARCHAR(16), "
                       "severity VARCHAR(16), created_at DATETIME, updated_at DATETIME)"))
        c.execute(text("INSERT INTO alert (id, device_id, state) VALUES (1, 'bed1', 'confirmed')"))
    init_db(eng)
    cols = {c["name"] for c in inspect(eng).get_columns("alert")}
    assert "recurrences" in cols
    with eng.begin() as c:
        assert c.execute(text("SELECT count(*) FROM alert")).scalar() == 1
