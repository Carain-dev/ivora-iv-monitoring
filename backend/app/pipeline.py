"""One reading in, everything out: quality check, processing, rules, sensor
health, evidence, events and alerts, all written to the database.

The backend is authoritative: the dashboard only displays what this module
stores. There is one Pipeline per (device_id, source) so simulated and real
data never share a window, an event or a bag session.
"""

from __future__ import annotations

import math
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import evidence as ev
from .alarms import AlarmManager, Context
from .config import Settings
from .db import BagSession, Event, Measurement, RawReading
from .processing import SignalProcessor, remaining_pct
from .rules import RuleEngine, gain_threshold
from .sensor_health import SensorHealthModel, assess

SENSOR_PERSIST_S = 20.0
GOOD_QUALITY = ("ok", "repeated")

SENSOR_LABELS = {
    "POSSIBLE_DRIFT": "Possible sensor drift",
    "POSSIBLE_STUCK_SIGNAL": "Possible stuck sensor signal",
    "ABNORMAL_NOISE": "Abnormal sensor noise",
}
ONE_SHOT = {"BAG_REPLACEMENT"}


def aware(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; everything in this app is UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def to_grams(value_num: float | None, cfg: Settings) -> float | None:
    if value_num is None:
        return None
    return value_num * cfg.weight_scale + cfg.weight_offset_g


def parse_number(value) -> float | None:
    """Accept 81, "81", "81.5", ["81"]; anything else is None (stored as invalid)."""
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def classify(
    *,
    api_error: str | None,
    device_online: bool | None,
    weight_g: float | None,
    value_raw: str | None,
    prev_value_raw: str | None,
    cfg: Settings,
) -> str:
    """First match wins (INGESTION_DESIGN.md §4)."""
    if api_error:
        return "api_error"
    if device_online is False:
        return "device_offline"
    if weight_g is None or not (cfg.min_valid_g <= weight_g <= cfg.load_cell_capacity_g):
        return "invalid"
    if prev_value_raw is not None and value_raw == prev_value_raw:
        return "repeated"
    return "ok"


class Pipeline:
    def __init__(self, cfg: Settings, device_id: str, source: str, health_model: SensorHealthModel | None):
        self.cfg = cfg
        self.device_id = device_id
        self.source = source
        self.health_model = health_model
        self.lock = threading.RLock()
        self.proc = SignalProcessor(
            moving_avg_samples=cfg.moving_avg_samples,
            rate_window_s=cfg.rate_window_s,
            min_window_samples=cfg.min_window_samples,
            flat_tolerance_g=cfg.flat_tolerance_g,
            step_lookback_s=cfg.step_lookback_s,
        )
        self.rules = RuleEngine(cfg)
        self.prev_value_raw: str | None = None
        self.last_good_at: datetime | None = None
        self.last_step_at: datetime | None = None
        self.sensor_since: dict[str, datetime] = {}
        self.active: dict[str, int] = {}  # event kind -> event id
        self.bag: BagSession | None = None
        self.alarms = AlarmManager(cfg, device_id, source)
        self.last_sensor_state: str | None = None

    # ---- startup -----------------------------------------------------------

    def restore(self, db: Session, now: datetime) -> None:
        """Rebuild in-memory state from the database after a restart."""
        with self.lock:
            self.bag = db.scalars(
                select(BagSession)
                .where(BagSession.device_id == self.device_id, BagSession.source == self.source, BagSession.ended_at.is_(None))
                .order_by(BagSession.id.desc())
            ).first()
            since = now - timedelta(seconds=self.cfg.rate_window_s * 2)
            rows = db.scalars(
                select(RawReading)
                .where(
                    RawReading.device_id == self.device_id,
                    RawReading.source == self.source,
                    RawReading.received_at >= since,
                )
                .order_by(RawReading.received_at)
            ).all()
            for r in rows:
                if r.quality in GOOD_QUALITY and r.weight_g is not None:
                    ts = aware(r.source_ts or r.received_at)
                    self.proc.add(ts, r.weight_g)
                    self.last_good_at = aware(r.received_at)
                self.prev_value_raw = r.value_raw
            for e in db.scalars(
                select(Event).where(
                    Event.device_id == self.device_id, Event.source == self.source, Event.ended_at.is_(None)
                )
            ):
                self.active[e.kind] = e.id
            self.alarms.restore(db)

    # ---- bag sessions ------------------------------------------------------

    def start_bag(
        self,
        db: Session,
        *,
        tare_g: float | None,
        initial_g: float | None,
        expected_rate_g_min: float | None,
        use_current_as_initial: bool = False,
        initial_source: str = "manual",
        notes: str | None = None,
        now: datetime | None = None,
    ) -> BagSession:
        with self.lock:
            now = now or datetime.now(timezone.utc)
            if use_current_as_initial:
                st = self.proc.compute(now)
                initial_g = st.weight_filtered_g
                initial_source = "current_weight"
            if self.bag is not None:
                bag = db.get(BagSession, self.bag.id)
                if bag is not None and bag.ended_at is None:
                    bag.ended_at = now
            bag = BagSession(
                device_id=self.device_id,
                source=self.source,
                started_at=now,
                tare_g=tare_g,
                initial_g=initial_g,
                expected_rate_g_min=expected_rate_g_min,
                initial_source=initial_source if initial_g is not None else "pending_auto",
                notes=notes,
            )
            db.add(bag)
            db.flush()
            self.bag = bag
            self.alarms.on_new_bag(db, now)
            return bag

    # ---- main entry point --------------------------------------------------

    def ingest(
        self,
        db: Session,
        *,
        received_at: datetime,
        value_raw: str | None,
        value_num: float | None,
        device_online: bool | None,
        api_error: str | None = None,
        source_ts: datetime | None = None,
        pin: str | None = None,
        sim_scenario: str | None = None,
    ) -> Measurement:
        with self.lock:
            cfg = self.cfg
            now = received_at
            weight_g = to_grams(value_num, cfg)
            quality = classify(
                api_error=api_error,
                device_online=device_online,
                weight_g=weight_g,
                value_raw=value_raw,
                prev_value_raw=self.prev_value_raw,
                cfg=cfg,
            )
            if value_raw is not None:
                self.prev_value_raw = value_raw
            raw = RawReading(
                device_id=self.device_id,
                pin=pin,
                value_raw=value_raw,
                value_num=value_num,
                weight_g=weight_g,
                unit="g" if cfg.units_verified else "g (unverified)",
                source_ts=source_ts,
                received_at=received_at,
                source=self.source,
                device_online=device_online,
                quality=quality,
                error=api_error,
                sim_scenario=sim_scenario,
            )
            db.add(raw)
            db.flush()

            stuck_since = self.sensor_since.get("POSSIBLE_STUCK_SIGNAL")
            if (quality == "ok" and self.last_sensor_state == "POSSIBLE_STUCK_SIGNAL" and stuck_since is not None
                    and (received_at - stuck_since).total_seconds() >= SENSOR_PERSIST_S):
                # Resynchronisation: the reading moves again after a CONFIRMED
                # stuck-signal flag (a brief candidate flag is not enough). The
                # frozen samples were not measurements, so they leave the rate
                # window; otherwise the catch-up step looks like fast loss.
                self.proc.reset(keep_after=received_at)
            if quality in GOOD_QUALITY:
                self.proc.add(source_ts or received_at, weight_g)
                self.last_good_at = received_at
            data_age = (now - self.last_good_at).total_seconds() if self.last_good_at else None

            quality_problem = None
            if quality == "api_error":
                quality_problem = f"API request failed: {api_error}"
            elif quality == "device_offline":
                quality_problem = "device offline; the value shown by Blynk is the last one stored"
            elif quality == "invalid":
                quality_problem = f"invalid reading {value_raw!r} (outside {cfg.min_valid_g:g}..{cfg.load_cell_capacity_g:g} g or not a number)"
            elif data_age is not None and data_age > cfg.stale_s:
                quality_problem = f"no fresh data for {data_age:.0f} s"

            stats = self.proc.compute(now)
            if stats.step_g is not None and abs(stats.step_g) > cfg.bag_jump_g:
                self.last_step_at = now
            step_recent = self.last_step_at is not None and (now - self.last_step_at).total_seconds() < cfg.rate_window_s

            # Auto-capture the initial weight once readings are stable.
            if self.bag is not None and self.bag.initial_g is None and quality_problem is None:
                stable = self.proc.stable_mean(now, cfg.bag_stable_s, cfg.stable_std_g)
                if stable is not None:
                    bag = db.get(BagSession, self.bag.id)
                    bag.initial_g = stable
                    bag.initial_source = "auto_stable"
                    self.bag = bag

            bag = self.bag
            remaining = remaining_pct(stats.weight_filtered_g, bag.tare_g if bag else None, bag.initial_g if bag else None)
            rule = self.rules.evaluate(
                now, stats, self.proc, quality_problem, remaining, bag.expected_rate_g_min if bag else None
            )
            if rule.new_bag_initial_g is not None:
                self._bag_replaced(db, rule.new_bag_initial_g, now)
                bag = self.bag
                stats = self.proc.compute(now)
                remaining = remaining_pct(stats.weight_filtered_g, bag.tare_g, bag.initial_g)

            health = assess(cfg, stats, quality_problem, step_recent or rule.flow_state == "BAG_REPLACEMENT", self.health_model)
            self.last_sensor_state = health.state
            if self.health_model is not None and self.health_model.meta and health.method == "isolation_forest":
                health.reason += f" [model trained on {self.health_model.meta.get('source_group')} data]"

            def evid(kind: str, since: datetime, required: float, sensor_state: str) -> dict:
                return ev.score(
                    kind=kind,
                    held_s=(now - since).total_seconds(),
                    required_s=required,
                    r2=stats.r2,
                    rate_g_min=stats.rate_g_min,
                    interruption_rate_g_min=cfg.interruption_rate_g_min,
                    noise_g=stats.noise_g,
                    noise_limit_g=cfg.noise_std_g,
                    data_age_s=data_age,
                    fresh_s=cfg.fresh_s,
                    stale_s=cfg.stale_s,
                    sensor_state=sensor_state,
                )

            primary_ev = evid(rule.flow_state, rule.since, rule.required_s, health.state)

            ambiguity = None
            if rule.flow_state == "FLOW_INTERRUPTION" and health.state == "POSSIBLE_STUCK_SIGNAL":
                ambiguity = (
                    "Flat signal: this may be a real flow interruption or a stuck sensor. "
                    "The weight data alone cannot tell them apart; check the line and the sensor."
                )

            # ---- conditions that should have an open event ----------------
            wanted: dict[str, dict] = {}
            if rule.alertable or rule.flow_state == "NORMAL_FLOW":
                wanted[rule.flow_state] = {
                    "layer": "rule", "status": rule.status, "reason": rule.reason,
                    "since": rule.since, "evidence": primary_ev,
                }
            if rule.low_volume:
                wanted["LOW_VOLUME"] = {
                    "layer": "rule", "status": rule.low_volume_status,
                    "reason": f"remaining {remaining:.1f}% below {cfg.low_volume_pct:g}%",
                    "since": rule.low_volume_since,
                    "evidence": evid("LOW_VOLUME", rule.low_volume_since, cfg.low_volume_persist_s, health.state),
                }
            for k in list(self.sensor_since):
                if k != health.state:
                    self.sensor_since.pop(k)
            if health.state in SENSOR_LABELS:
                since = self.sensor_since.setdefault(health.state, now)
                held = (now - since).total_seconds()
                wanted[health.state] = {
                    "layer": "sensor_health",
                    "status": "confirmed" if held >= SENSOR_PERSIST_S else "candidate",
                    "reason": health.reason, "since": since,
                    "evidence": evid(health.state, since, SENSOR_PERSIST_S, health.state),
                }
            if ambiguity:
                for k in ("FLOW_INTERRUPTION", "POSSIBLE_STUCK_SIGNAL"):
                    if k in wanted:
                        wanted[k]["reason"] += " | " + ambiguity

            sensor_status = health.state
            meas = Measurement(
                raw_id=raw.id,
                device_id=self.device_id,
                ts=now,
                source=self.source,
                bag_session_id=bag.id if bag else None,
                weight_g=stats.weight_g if quality in GOOD_QUALITY else None,
                weight_filtered_g=stats.weight_filtered_g,
                rate_g_min=stats.rate_g_min if stats.window_ok else None,
                remaining_pct=remaining,
                slope_g_s=stats.slope_g_s,
                rolling_std_g=stats.rolling_std_g,
                rolling_var_g2=stats.rolling_var_g2,
                noise_g=stats.noise_g,
                baseline_dev_g=stats.baseline_dev_g,
                flat_duration_s=stats.flat_duration_s,
                r2=stats.r2,
                window_s=stats.window_s,
                window_n=stats.window_n,
                data_age_s=data_age,
                quality=quality,
                flow_state=rule.flow_state,
                flow_state_status=rule.status,
                low_volume=rule.low_volume and rule.low_volume_status == "confirmed",
                sensor_state=sensor_status,
                anomaly_index=health.anomaly_index,
                evidence_score=primary_ev["score"],
                details={
                    "rule_reason": rule.reason,
                    "sensor_reason": health.reason,
                    "sensor_method": health.method,
                    "evidence": primary_ev,
                    "high_rate_threshold_g_min": rule.high_threshold_g_min,
                    "low_volume_status": rule.low_volume_status,
                    "ambiguity": ambiguity,
                    "step_g": stats.step_g,
                    "rate_se_g_min": stats.rate_se_g_min,
                    "gain_threshold_g_min": gain_threshold(cfg, stats) if stats.window_ok else None,
                },
            )
            db.add(meas)
            self._sync_events(db, wanted, now)
            self.alarms.update(db, Context(
                now=now, wanted=wanted, event_ids=dict(self.active), flow_state=rule.flow_state,
                rate_g_min=meas.rate_g_min, remaining_pct=remaining, weight_g=stats.weight_filtered_g,
                sensor_state=sensor_status, data_ok=quality_problem is None and quality in GOOD_QUALITY,
                high_threshold_g_min=rule.high_threshold_g_min, bag_active=bag is not None,
                bag_replaced=rule.new_bag_initial_g is not None,
            ))
            if rule.flow_state in ONE_SHOT and rule.status == "confirmed":
                self._end_event(db, rule.flow_state, now)
            db.flush()
            return meas

    # ---- internals -----------------------------------------------------------

    def _bag_replaced(self, db: Session, new_initial_g: float, now: datetime) -> None:
        prev = self.bag
        self.start_bag(
            db,
            tare_g=prev.tare_g if prev else None,
            initial_g=new_initial_g,
            expected_rate_g_min=prev.expected_rate_g_min if prev else None,
            initial_source="bag_replacement",
            notes="created automatically after a confirmed bag replacement",
            now=now,
        )
        # Keep only the stable post-replacement readings in the rate window.
        self.proc.reset(keep_after=now - timedelta(seconds=self.cfg.bag_stable_s))

    def _sync_events(self, db: Session, wanted: dict, now: datetime) -> None:
        """Events record every detected condition; alarms.py decides which become alarms."""
        for kind in list(self.active):
            if kind not in wanted:
                self._end_event(db, kind, now)
        for kind, w in wanted.items():
            evt_id = self.active.get(kind)
            evt = db.get(Event, evt_id) if evt_id else None
            if evt is None:
                evt = Event(
                    device_id=self.device_id, source=self.source, kind=kind, layer=w["layer"],
                    status=w["status"], started_at=w["since"],
                )
                db.add(evt)
                db.flush()
                self.active[kind] = evt.id
            if w["status"] == "confirmed" and evt.confirmed_at is None:
                evt.confirmed_at = now
            evt.status = w["status"]
            evt.reason = w["reason"]
            evt.evidence_score = w["evidence"]["score"]
            evt.evidence = w["evidence"]

    def _end_event(self, db: Session, kind: str, now: datetime) -> None:
        evt_id = self.active.pop(kind, None)
        if not evt_id:
            return
        evt = db.get(Event, evt_id)
        if evt is None:
            return
        evt.ended_at = now
        evt.status = "ended" if evt.confirmed_at is not None else "dismissed"
