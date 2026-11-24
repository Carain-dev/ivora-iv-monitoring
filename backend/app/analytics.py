"""Research metrics computed ONLY from stored observations (requirements §10).

Simulated and physical data are always evaluated separately. If there are no
labelled observations for a group, the metrics are reported as unavailable
rather than estimated.

Event-level matching: a labelled interval counts as detected (TP) if a
CONFIRMED event of the same kind starts or is active between the label's
start and its end + MATCH_GRACE_S. Confirmed events that match no label are
false positives. Detection latency = confirmed_at - label start.

Sample-level matching (flow states only): each measurement whose timestamp
falls inside a labelled interval of a flow-state kind is compared with the
confirmed primary flow state, giving per-class TP/FP/FN/TN, sensitivity
and specificity.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import CalibrationCheck, Event, GroundTruth, Measurement
from .pipeline import aware

MATCH_GRACE_S = 180.0
EVENT_KINDS = [
    "FLOW_INTERRUPTION", "HIGH_MASS_LOSS_RATE", "BAG_REPLACEMENT", "LOW_VOLUME", "SENSOR_UNCERTAIN",
    "POSSIBLE_DRIFT", "POSSIBLE_STUCK_SIGNAL", "ABNORMAL_NOISE",
]
FLOW_CLASSES = ["NORMAL_FLOW", "FLOW_INTERRUPTION", "HIGH_MASS_LOSS_RATE"]


def group_of(source: str) -> str:
    return "simulated" if source == "simulated" else "physical"


def _sources(group: str) -> list[str]:
    return ["simulated"] if group == "simulated" else ["blynk_poll", "blynk_history"]


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 3) if b else None


def evaluate(db: Session, device_id: str, group: str) -> dict:
    labels = db.scalars(
        select(GroundTruth).where(GroundTruth.device_id == device_id, GroundTruth.source == group)
        .order_by(GroundTruth.started_at)
    ).all()
    labels = [g for g in labels if g.ended_at is not None]
    out: dict = {"group": group, "n_labels": len(labels), "event_level": {}, "sample_level": {}}
    if not labels:
        out["note"] = f"No completed {group} ground-truth labels yet, so no detection metrics are reported."
        return out
    t_min = min(aware(g.started_at) for g in labels)
    t_max = max(aware(g.ended_at) for g in labels) + timedelta(seconds=MATCH_GRACE_S)
    events = db.scalars(
        select(Event).where(
            Event.device_id == device_id, Event.source.in_(_sources(group)),
            Event.confirmed_at.is_not(None), Event.started_at <= t_max,
        )
    ).all()
    events = [e for e in events if (aware(e.ended_at) or t_max) >= t_min]

    for kind in EVENT_KINDS:
        kl = [g for g in labels if g.kind == kind]
        ke = [e for e in events if e.kind == kind]
        if not kl and not ke:
            continue
        matched_events: set[int] = set()
        latencies: list[float] = []
        tp = 0
        for g in kl:
            gs, ge = aware(g.started_at), aware(g.ended_at) + timedelta(seconds=MATCH_GRACE_S)
            hits = [e for e in ke if aware(e.confirmed_at) <= ge and (aware(e.ended_at) or ge) >= gs]
            if hits:
                tp += 1
                first = min(hits, key=lambda e: aware(e.confirmed_at))
                latencies.append((aware(first.confirmed_at) - aware(g.started_at)).total_seconds())
                matched_events.update(e.id for e in hits)
        fn = len(kl) - tp
        fp = len([e for e in ke if e.id not in matched_events])
        prec, rec = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
        f1 = round(2 * prec * rec / (prec + rec), 3) if prec and rec else None
        out["event_level"][kind] = {
            "labels": len(kl), "tp": tp, "fp": fp, "fn": fn,
            "precision": prec, "recall_sensitivity": rec, "f1": f1,
            "latency_s_mean": round(statistics.mean(latencies), 1) if latencies else None,
            "latency_s_median": round(statistics.median(latencies), 1) if latencies else None,
        }

    flow_labels = [g for g in labels if g.kind in FLOW_CLASSES]
    if flow_labels:
        ms = db.scalars(
            select(Measurement).where(
                Measurement.device_id == device_id, Measurement.source.in_(_sources(group)),
                Measurement.ts >= t_min, Measurement.ts <= t_max,
            )
        ).all()
        pairs = []
        for m in ms:
            ts = aware(m.ts)
            g = next((g for g in flow_labels if aware(g.started_at) <= ts <= aware(g.ended_at)), None)
            if g is None:
                continue
            pred = m.flow_state if m.flow_state_status == "confirmed" else "UNCONFIRMED"
            pairs.append((g.kind, pred))
        for cls in FLOW_CLASSES:
            tp = sum(1 for t, p in pairs if t == cls and p == cls)
            fn = sum(1 for t, p in pairs if t == cls and p != cls)
            fp = sum(1 for t, p in pairs if t != cls and p == cls)
            tn = sum(1 for t, p in pairs if t != cls and p != cls)
            if tp + fn == 0 and fp == 0:
                continue
            prec, rec = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
            out["sample_level"][cls] = {
                "tp": tp, "fp": fp, "fn": fn, "tn": tn,
                "sensitivity": rec, "specificity": _ratio(tn, tn + fp), "precision": prec,
                "f1": round(2 * prec * rec / (prec + rec), 3) if prec and rec else None,
            }
        out["sample_level_n"] = len(pairs)
    return out


def calibration_summary(db: Session, device_id: str, group: str) -> dict:
    rows = db.scalars(
        select(CalibrationCheck).where(CalibrationCheck.device_id == device_id, CalibrationCheck.source.in_(_sources(group)))
        .order_by(CalibrationCheck.created_at)
    ).all()
    out: dict = {"group": group, "n": len(rows), "checks": [
        {"id": r.id, "known_mass_g": r.known_mass_g, "measured_g": round(r.measured_g, 2),
         "error_g": round(r.measured_g - r.known_mass_g, 2), "std_g": r.std_g, "n_samples": r.n_samples,
         "created_at": r.created_at, "notes": r.notes}
        for r in rows
    ]}
    if not rows:
        out["note"] = "No calibration checks recorded yet."
        return out
    errs = [r.measured_g - r.known_mass_g for r in rows]
    out["mae_g"] = round(statistics.mean(abs(e) for e in errs), 3)
    out["rmse_g"] = round(math.sqrt(statistics.mean(e * e for e in errs)), 3)
    out["bias_g"] = round(statistics.mean(errs), 3)
    masses = {r.known_mass_g for r in rows}
    if len(masses) >= 2:
        xs = [r.measured_g for r in rows]
        ys = [r.known_mass_g for r in rows]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx > 0:
            a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
            b = my - a * mx
            out["correction"] = {
                "true_g = a * measured_g + b": {"a": round(a, 6), "b": round(b, 3)},
                "how_to_apply": "new WEIGHT_SCALE = a * WEIGHT_SCALE; new WEIGHT_OFFSET_G = a * WEIGHT_OFFSET_G + b",
            }
    return out


def summary(db: Session, device_id: str, source: str, since: datetime) -> dict:
    """Simple descriptive counts for the research panel."""
    evs = db.scalars(select(Event).where(Event.device_id == device_id, Event.source == source, Event.started_at >= since)).all()
    by_kind: dict[str, dict] = {}
    for e in evs:
        d = by_kind.setdefault(e.kind, {"events": 0, "confirmed": 0, "dismissed": 0, "total_duration_s": 0.0})
        d["events"] += 1
        d["confirmed"] += 1 if e.confirmed_at else 0
        d["dismissed"] += 1 if e.status == "dismissed" else 0
        if e.ended_at:
            d["total_duration_s"] += (aware(e.ended_at) - aware(e.started_at)).total_seconds()
    return {"since": since, "source": source, "by_kind": by_kind}
