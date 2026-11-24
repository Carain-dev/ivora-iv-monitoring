"""Alarm manager: turns detected events into a small number of prioritised alarms.

The rule engine and the sensor-health layer DETECT conditions (stored as
events). This module decides when a condition is worth an ALARM, how loud it
is, and when it is over. It exists to keep false and repeated alarms down:

  * Priority per event type (PRIORITY below). HIGH and MEDIUM sound the
    buzzer; LOW and INFO are shown on the dashboard only.
  * Trigger: infusion alarms need the rule state CONFIRMED (the rule's own
    persistence time has passed) and an active bag session. Sensor and
    data-loss alarms need the condition to have held ALARM_SENSOR_PERSIST_S.
  * One alarm per type per device at a time; while it is active the same
    detection only updates it.
  * Recovery needs POSITIVE evidence held for ALARM_CLEAR_S (hysteresis),
    e.g. the rate back above INTERRUPTION_CLEAR_G_MIN. Missing or uncertain
    data never clears an alarm; it is latched until good data shows recovery.
  * A same-type alarm that recurs within ALARM_REARM_S of clearing reopens the
    old alarm (its `recurrences` count goes up) instead of adding a new one.
  * Acknowledging silences the buzzer while the alarm stays visible. A HIGH
    alarm still active ALARM_RESOUND_S after acknowledgement sounds again.
  * LOW sensor alarms (noise, drift) that persist ALARM_ESCALATE_S become
    MEDIUM ("persistent sensor fault").
  * Starting a new bag (manually or by a confirmed bag replacement) resolves
    the infusion alarms of the old bag.

Every number here is a configurable PROTOTYPE default. None of them is a
clinical threshold and none has been validated experimentally.

Buzzer patterns follow the pulse-count convention of IEC 60601-1-8 (high =
10-pulse burst, medium = 3-pulse burst); the timings are prototype choices and
the system has NOT been tested against that standard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .db import Alert

PRIORITY = {
    "FLOW_INTERRUPTION": "high",
    "HIGH_MASS_LOSS_RATE": "high",
    "LOW_VOLUME": "medium",
    "POSSIBLE_STUCK_SIGNAL": "medium",
    "SENSOR_UNCERTAIN": "medium",
    "ABNORMAL_NOISE": "low",
    "POSSIBLE_DRIFT": "low",
    "BAG_REPLACEMENT": "info",
}
RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}
INFUSION = ("FLOW_INTERRUPTION", "HIGH_MASS_LOSS_RATE", "LOW_VOLUME")
SENSOR = ("SENSOR_UNCERTAIN", "POSSIBLE_STUCK_SIGNAL", "ABNORMAL_NOISE", "POSSIBLE_DRIFT")
ESCALATES = ("ABNORMAL_NOISE", "POSSIBLE_DRIFT")
ACTIVE_STATES = ("confirmed", "acknowledged")

# [on_ms, off_ms] steps played once per repeat period. `code` is what an
# optional firmware handler would receive (0 silent, 1 medium, 2 high).
BUZZER = {
    "high": {
        "code": 2,
        "repeat_s": 5.0,
        "steps_ms": [[150, 100], [150, 100], [150, 400], [150, 100], [150, 1000],
                     [150, 100], [150, 100], [150, 400], [150, 100], [150, 100]],
        "description": "10-pulse burst (3+2, 3+2) every 5 s",
    },
    "medium": {
        "code": 1,
        "repeat_s": 15.0,
        "steps_ms": [[250, 150], [250, 150], [250, 150]],
        "description": "3-pulse burst every 15 s",
    },
}
SILENT = {"code": 0, "repeat_s": None, "steps_ms": [], "description": "silent"}


@dataclass
class Context:
    """What the alarm manager needs from one pipeline step."""

    now: datetime
    wanted: dict  # event kind -> {"status", "since", "reason", "evidence"}
    event_ids: dict  # event kind -> open event id
    flow_state: str
    rate_g_min: float | None
    remaining_pct: float | None
    weight_g: float | None
    sensor_state: str
    data_ok: bool  # latest reading was good and fresh
    high_threshold_g_min: float | None
    bag_active: bool
    bag_replaced: bool


@dataclass
class AlarmManager:
    cfg: Settings
    device_id: str
    source: str
    active: dict = field(default_factory=dict)  # kind -> alert id
    clear_since: dict = field(default_factory=dict)  # kind -> first time recovery held
    hold_until_clear: set = field(default_factory=set)  # resolved by hand while still present

    # ---- startup -----------------------------------------------------------

    def restore(self, db: Session) -> None:
        for a in db.scalars(select(Alert).where(
            Alert.device_id == self.device_id, Alert.source == self.source,
            Alert.state.in_(("candidate",) + ACTIVE_STATES),
        )):
            if a.state == "candidate":  # left over from the pre-alarm-manager version
                a.state = "resolved"
                a.message = (a.message or "") + " | closed: candidate alerts are no longer raised"
                continue
            self.active[a.event_type] = a.id

    # ---- trigger / recovery rules ------------------------------------------

    def _triggered(self, kind: str, c: Context) -> bool:
        w = c.wanted.get(kind)
        if w is None:
            return False
        if kind in INFUSION:
            return w["status"] == "confirmed" and (c.bag_active or not self.cfg.alarms_require_bag)
        return (c.now - w["since"]).total_seconds() >= self.cfg.alarm_sensor_persist_s

    def _recovered(self, kind: str, c: Context) -> bool:
        """Positive evidence that the condition is over. Missing data is never recovery."""
        cfg = self.cfg
        if not c.data_ok:
            return False
        if kind == "FLOW_INTERRUPTION":
            return (c.flow_state in ("NORMAL_FLOW", "HIGH_MASS_LOSS_RATE")
                    and c.rate_g_min is not None and c.rate_g_min >= cfg.interruption_clear_g_min)
        if kind == "HIGH_MASS_LOSS_RATE":
            return (c.flow_state in ("NORMAL_FLOW", "FLOW_INTERRUPTION") and c.rate_g_min is not None
                    and c.high_threshold_g_min is not None
                    and c.rate_g_min <= c.high_threshold_g_min * cfg.high_rate_clear_fraction)
        if kind == "LOW_VOLUME":
            return (c.remaining_pct is not None and c.flow_state != "SENSOR_UNCERTAIN"
                    and c.remaining_pct >= cfg.low_volume_pct + cfg.low_volume_clear_margin_pct)
        if kind == "SENSOR_UNCERTAIN":
            return c.flow_state != "SENSOR_UNCERTAIN"
        return c.sensor_state == "SENSOR_NORMAL"

    # ---- main entry point --------------------------------------------------

    def update(self, db: Session, c: Context) -> None:
        now = c.now
        if c.bag_replaced and "BAG_REPLACEMENT" in c.event_ids:
            self._notice(db, c)
        for kind in INFUSION + SENSOR:
            triggered = self._triggered(kind, c)
            if not triggered:
                self.hold_until_clear.discard(kind)
            alert = self._load(db, kind)
            if alert is None:
                if triggered and kind not in self.hold_until_clear and kind in c.event_ids:
                    self._raise(db, kind, c)
                continue
            self._refresh(alert, kind, c)
            if triggered:
                self.clear_since.pop(kind, None)
            elif self._recovered(kind, c):
                since = self.clear_since.setdefault(kind, now)
                held = (now - since).total_seconds()
                if held >= self.cfg.alarm_clear_s:
                    self._resolve(alert, kind, now, f"recovered for {held:.0f} s")
            else:
                self.clear_since.pop(kind, None)  # not triggered, not recovered: stay latched
            if kind in self.active:
                self._escalate_and_resound(alert, kind, now)

    def on_new_bag(self, db: Session, now: datetime) -> None:
        for kind in INFUSION:
            alert = self._load(db, kind)
            if alert is not None:
                self._resolve(alert, kind, now, "new bag started")
            self.hold_until_clear.discard(kind)

    # ---- internals -----------------------------------------------------------

    def _load(self, db: Session, kind: str) -> Alert | None:
        aid = self.active.get(kind)
        if aid is None:
            return None
        alert = db.get(Alert, aid)
        if alert is None or alert.state not in ACTIVE_STATES:
            # resolved by hand on the dashboard: do not re-raise until the condition clears
            self.active.pop(kind, None)
            self.clear_since.pop(kind, None)
            self.hold_until_clear.add(kind)
            return None
        return alert

    def _refresh(self, alert: Alert, kind: str, c: Context) -> None:
        w = c.wanted.get(kind)
        alert.updated_at = c.now
        alert.weight_g = c.weight_g
        alert.rate_g_min = c.rate_g_min
        alert.remaining_pct = c.remaining_pct
        alert.sensor_status = c.sensor_state
        if w is not None:
            alert.evidence_score = w["evidence"]["score"]
            alert.message = w["reason"]
            if kind in c.event_ids:
                alert.event_id = c.event_ids[kind]

    def _raise(self, db: Session, kind: str, c: Context) -> None:
        now = c.now
        recent = db.scalars(
            select(Alert).where(
                Alert.device_id == self.device_id, Alert.source == self.source, Alert.event_type == kind,
                Alert.state == "resolved", Alert.resolved_at >= now - timedelta(seconds=self.cfg.alarm_rearm_s),
            ).order_by(Alert.resolved_at.desc())
        ).first()
        if recent is not None:
            alert = recent
            alert.recurrences = (alert.recurrences or 0) + 1
            alert.resolved_at = None
            alert.acknowledged_at = None
            alert.acknowledged_by = None
        else:
            from .rules import LABELS
            from .pipeline import SENSOR_LABELS

            alert = Alert(
                event_id=c.event_ids[kind], device_id=self.device_id, source=self.source, event_type=kind,
                label=LABELS.get(kind) or SENSOR_LABELS.get(kind, kind), created_at=now, recurrences=0,
            )
            db.add(alert)
        alert.state = "confirmed"
        alert.severity = PRIORITY[kind]
        alert.confirmed_at = now
        self._refresh(alert, kind, c)
        db.flush()
        self.active[kind] = alert.id
        self.clear_since.pop(kind, None)

    def _resolve(self, alert: Alert, kind: str, now: datetime, why: str) -> None:
        alert.state = "resolved"
        alert.resolved_at = now
        alert.updated_at = now
        alert.message = (alert.message or "") + f" | auto-resolved: {why}"
        self.active.pop(kind, None)
        self.clear_since.pop(kind, None)

    def _escalate_and_resound(self, alert: Alert, kind: str, now: datetime) -> None:
        cfg = self.cfg
        raised = alert.confirmed_at or alert.created_at
        if (kind in ESCALATES and alert.severity == "low" and raised is not None
                and (now - _aware(raised)).total_seconds() >= cfg.alarm_escalate_s):
            alert.severity = "medium"
            alert.state = "confirmed"  # a new, louder alarm: sound even if the low one was acknowledged
            alert.message = (alert.message or "") + f" | escalated to medium: persisted {cfg.alarm_escalate_s:g} s"
        if (alert.state == "acknowledged" and alert.severity == "high" and cfg.alarm_resound_s > 0
                and alert.acknowledged_at is not None
                and (now - _aware(alert.acknowledged_at)).total_seconds() >= cfg.alarm_resound_s):
            alert.state = "confirmed"
            alert.message = (alert.message or "") + f" | sounding again: still active {cfg.alarm_resound_s:g} s after acknowledgement"

    def _notice(self, db: Session, c: Context) -> None:
        """Bag replacement is information, not an alarm: stored already resolved, no buzzer."""
        db.add(Alert(
            event_id=c.event_ids["BAG_REPLACEMENT"], device_id=self.device_id, source=self.source,
            event_type="BAG_REPLACEMENT", label="Bag replacement", state="resolved", severity="info",
            created_at=c.now, updated_at=c.now, confirmed_at=c.now, resolved_at=c.now, recurrences=0,
            weight_g=c.weight_g, remaining_pct=c.remaining_pct, sensor_status=c.sensor_state,
            evidence_score=c.wanted.get("BAG_REPLACEMENT", {}).get("evidence", {}).get("score"),
            message=(c.wanted.get("BAG_REPLACEMENT", {}).get("reason") or "bag replaced") + " | notice only, no alarm",
        ))


def _aware(dt: datetime) -> datetime:
    from .pipeline import aware

    return aware(dt)


def buzzer_for(alerts: list[Alert]) -> dict:
    """Pattern the buzzer should play now: the highest unacknowledged HIGH/MEDIUM alarm."""
    sounding = [a for a in alerts if a.state == "confirmed" and a.severity in BUZZER]
    if not sounding:
        return {**SILENT, "priority": None, "alert_id": None, "event_type": None}
    top = max(sounding, key=lambda a: (RANK[a.severity], a.id))
    return {**BUZZER[top.severity], "priority": top.severity, "alert_id": top.id, "event_type": top.event_type}
