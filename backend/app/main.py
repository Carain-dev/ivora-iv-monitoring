"""FastAPI app: starts the data source, serves the API and the built dashboard."""

from __future__ import annotations

import csv
import io
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import desc, select

from . import analytics
from .alarms import BUZZER, PRIORITY, buzzer_for
from .blynk_client import BlynkClient, BlynkError
from .config import blynk_token, settings
from .db import (
    Alert, BagSession, CalibrationCheck, Event, GroundTruth, Measurement, RawReading, SessionLocal, init_db,
)
from .pipeline import GOOD_QUALITY, Pipeline, aware
from .processing import FEATURE_NAMES
from .sensor_health import SensorHealthModel
from .sources import BlynkSource, SimulatorSource, close_open_simulated_truth

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("iv_monitor")

SOURCE_NAME = {"blynk": "blynk_poll", "simulator": "simulated"}
STATIC_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"


class Runtime:
    def __init__(self):
        self.health_model = SensorHealthModel(settings.model_dir)
        self.source_name = SOURCE_NAME.get(settings.data_source)
        if self.source_name is None:
            raise RuntimeError("DATA_SOURCE must be 'blynk' or 'simulator'")
        self.pipeline = Pipeline(settings, settings.device_id, self.source_name, self.health_model)
        self.source = None

    def start(self):
        init_db()
        with SessionLocal() as db:
            self.pipeline.restore(db, datetime.now(timezone.utc))
        if self.source_name == "simulated":
            close_open_simulated_truth(settings.device_id)
            self.source = SimulatorSource(settings, self.pipeline)
        else:
            self.source = BlynkSource(settings, self.pipeline)
        self.source.start()

    def stop(self):
        if self.source:
            self.source.stop()


rt: Runtime | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global rt
    rt = Runtime()
    rt.start()
    log.info("data source: %s, device: %s, database: %s", settings.data_source, settings.device_id,
             settings.public()["database"])
    yield
    rt.stop()


app = FastAPI(title="IVORA: IV bag weight monitor (research prototype)", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _src(source: str | None) -> str:
    return source or rt.source_name


def _row(obj, exclude: tuple = ()) -> dict:
    out = {}
    for c in obj.__table__.columns:
        if c.name in exclude:
            continue
        v = getattr(obj, c.name)
        out[c.name] = aware(v) if isinstance(v, datetime) else v
    return out


# ---------------------------------------------------------------------------
# status


def data_label(source: str, latest: Measurement | None, src_status: dict) -> str:
    """LIVE | SIMULATED | STALE | UNAVAILABLE, as shown on the dashboard."""
    if latest is None:
        return "UNAVAILABLE"
    age = (_now() - aware(latest.ts)).total_seconds()
    if latest.data_age_s is not None:
        age = max(age, latest.data_age_s)
    if source == "simulated":
        return "SIMULATED" if age <= settings.stale_s else "STALE"
    if not src_status.get("configured", True) or latest.quality == "api_error" and latest.data_age_s is None:
        return "UNAVAILABLE"
    if latest.quality == "device_offline" or age > settings.stale_s:
        return "STALE"
    if latest.quality in GOOD_QUALITY:
        return "LIVE"
    return "UNAVAILABLE" if latest.quality == "api_error" else "STALE"


@app.get("/api/status")
def status(source: str | None = None):
    source = _src(source)
    with SessionLocal() as db:
        latest = db.scalars(
            select(Measurement).where(Measurement.device_id == settings.device_id, Measurement.source == source)
            .order_by(desc(Measurement.id)).limit(1)
        ).first()
        bag = db.scalars(
            select(BagSession).where(BagSession.device_id == settings.device_id, BagSession.source == source,
                                     BagSession.ended_at.is_(None)).order_by(desc(BagSession.id))
        ).first()
        active_alerts = db.scalars(
            select(Alert).where(Alert.device_id == settings.device_id, Alert.source == source,
                                Alert.state.in_(["candidate", "confirmed", "acknowledged"]))
        ).all()
    src_status = rt.source.status.as_dict() if rt.source and source == rt.source_name else {}
    return {
        "device_id": settings.device_id,
        "source": source,
        "active_source": rt.source_name,
        "data_label": data_label(source, latest, src_status),
        "server_time": _now(),
        "seconds_since_last_measurement": (_now() - aware(latest.ts)).total_seconds() if latest else None,
        "latest": _row(latest) if latest else None,
        "bag_session": _row(bag) if bag else None,
        "active_alert_count": len(active_alerts),
        "buzzer": buzzer_for(active_alerts),
        "source_status": src_status,
        "units_verified": settings.units_verified,
        "weight_pin": settings.blynk_weight_pin,
        "sensor_model": rt.health_model.meta,
    }


@app.get("/api/config")
def config():
    return settings.public()


# ---------------------------------------------------------------------------
# time series and history


def _since(minutes: float) -> datetime:
    return _now() - timedelta(minutes=minutes)


@app.get("/api/measurements")
def measurements(minutes: float = Query(30, gt=0, le=60 * 24 * 7), source: str | None = None, max_points: int = 2000):
    with SessionLocal() as db:
        rows = db.scalars(
            select(Measurement).where(Measurement.device_id == settings.device_id, Measurement.source == _src(source),
                                      Measurement.ts >= _since(minutes)).order_by(Measurement.ts)
        ).all()
    step = max(1, len(rows) // max_points)
    keep = ("id", "ts", "weight_g", "weight_filtered_g", "rate_g_min", "remaining_pct", "quality", "flow_state",
            "flow_state_status", "sensor_state", "evidence_score", "anomaly_index", "low_volume", "noise_g", "data_age_s")
    return [{k: (aware(getattr(r, k)) if k == "ts" else getattr(r, k)) for k in keep} for r in rows[::step]]


@app.get("/api/raw")
def raw(minutes: float = Query(10, gt=0, le=60 * 24 * 7), source: str | None = None, limit: int = 2000):
    with SessionLocal() as db:
        rows = db.scalars(
            select(RawReading).where(RawReading.device_id == settings.device_id, RawReading.source == _src(source),
                                     RawReading.received_at >= _since(minutes))
            .order_by(desc(RawReading.received_at)).limit(limit)
        ).all()
    return [_row(r) for r in rows]


@app.get("/api/events")
def events(source: str | None = None, limit: int = 100, include_normal: bool = False):
    with SessionLocal() as db:
        q = select(Event).where(Event.device_id == settings.device_id, Event.source == _src(source))
        if not include_normal:
            q = q.where(Event.kind != "NORMAL_FLOW")
        rows = db.scalars(q.order_by(desc(Event.started_at)).limit(limit)).all()
    return [_row(r) for r in rows]


@app.get("/api/alerts")
def alerts(source: str | None = None, state: str = "active", limit: int = 100):
    with SessionLocal() as db:
        q = select(Alert).where(Alert.device_id == settings.device_id, Alert.source == _src(source))
        if state == "active":
            q = q.where(Alert.state.in_(["candidate", "confirmed", "acknowledged"]))
        elif state != "all":
            q = q.where(Alert.state == state)
        rows = db.scalars(q.order_by(desc(Alert.updated_at)).limit(limit)).all()
    return [_row(r) for r in rows]


@app.get("/api/alarms/policy")
def alarm_policy():
    """Priority, trigger, persistence, recovery and buzzer pattern for every alarm type.
    All values are configurable prototype defaults, not validated clinical thresholds."""
    c = settings
    infusion_arm = " Armed only while a bag session exists." if c.alarms_require_bag else ""
    sensor_persist = f"condition held for {c.alarm_sensor_persist_s:g} s"
    rows = [
        ("FLOW_INTERRUPTION", f"rate <= {c.interruption_rate_g_min:g} g/min while fluid remains." + infusion_arm,
         f"{c.interruption_persist_s:g} s (brief lapses up to {c.rule_grace_s:g} s bridged)",
         f"rate >= {c.interruption_clear_g_min:g} g/min for {c.alarm_clear_s:g} s, or a new bag"),
        ("HIGH_MASS_LOSS_RATE", f"rate minus {c.rate_sigma_k:g} standard errors above {c.high_rate_factor:g} x expected rate "
         f"(or {c.high_rate_abs_g_min:g} g/min with no expected rate). Possible free flow, unvalidated." + infusion_arm,
         f"{c.high_rate_persist_s:g} s (brief lapses up to {c.rule_grace_s:g} s bridged)",
         f"rate <= {c.high_rate_clear_fraction:g} x threshold for {c.alarm_clear_s:g} s, or a new bag"),
        ("LOW_VOLUME", f"remaining < {c.low_volume_pct:g} %." + infusion_arm, f"{c.low_volume_persist_s:g} s",
         f"remaining >= {c.low_volume_pct + c.low_volume_clear_margin_pct:g} % for {c.alarm_clear_s:g} s, or a new bag"),
        ("SENSOR_UNCERTAIN", "stale, offline, invalid or failed readings; sudden drop; significant weight gain",
         sensor_persist, f"good data and a normal flow state for {c.alarm_clear_s:g} s"),
        ("POSSIBLE_STUCK_SIGNAL", f"reading unchanged for {c.stuck_s:g} s", sensor_persist,
         f"sensor normal for {c.alarm_clear_s:g} s"),
        ("ABNORMAL_NOISE", f"noise around trend > {c.noise_std_g:g} g", sensor_persist + f"; medium after {c.alarm_escalate_s:g} s",
         f"sensor normal for {c.alarm_clear_s:g} s"),
        ("POSSIBLE_DRIFT", f"baseline shift > {c.drift_g:g} g or significant weight gain", sensor_persist + f"; medium after {c.alarm_escalate_s:g} s",
         f"sensor normal for {c.alarm_clear_s:g} s"),
        ("BAG_REPLACEMENT", f"jump > {c.bag_jump_g:g} g then stable {c.bag_stable_s:g} s", "-", "notice only"),
    ]
    return {
        "validated": False,
        "note": "Prototype defaults chosen for a benchtop rig. Not clinical thresholds; validate experimentally.",
        "alarms": [
            {"event_type": k, "priority": PRIORITY[k], "trigger": t, "persistence": p, "recovery": r,
             "buzzer": BUZZER.get(PRIORITY[k], {}).get("description", "none (dashboard only)")}
            for k, t, p, r in rows
        ],
        "repeat_suppression": f"One alarm per type at a time; recurrence within {c.alarm_rearm_s:g} s of clearing reopens it",
        "acknowledge": "silences the buzzer; the alarm stays until recovery"
                       + (f"; high alarms sound again after {c.alarm_resound_s:g} s if still active" if c.alarm_resound_s > 0 else ""),
        "patterns": BUZZER,
    }


class AckBody(BaseModel):
    by: str | None = None


@app.post("/api/alerts/{alert_id}/acknowledge")
def acknowledge(alert_id: int, body: AckBody = Body(default=AckBody())):
    with SessionLocal() as db:
        a = db.get(Alert, alert_id)
        if a is None:
            raise HTTPException(404, "alert not found")
        if a.state == "resolved":
            raise HTTPException(409, "alert already resolved")
        a.state, a.acknowledged_at, a.acknowledged_by, a.updated_at = "acknowledged", _now(), body.by, _now()
        db.commit()
        return _row(a)


@app.post("/api/alerts/{alert_id}/resolve")
def resolve(alert_id: int):
    with SessionLocal() as db:
        a = db.get(Alert, alert_id)
        if a is None:
            raise HTTPException(404, "alert not found")
        a.state, a.resolved_at, a.updated_at = "resolved", _now(), _now()
        a.message = (a.message or "") + " | resolved manually"
        db.commit()
        return _row(a)


# ---------------------------------------------------------------------------
# bag session (tare, initial weight, expected rate)


class BagBody(BaseModel):
    tare_g: float | None = None
    initial_g: float | None = None
    expected_rate_g_min: float | None = None
    use_current_as_initial: bool = False
    notes: str | None = None


@app.get("/api/bag-sessions")
def bag_sessions(source: str | None = None, limit: int = 20):
    with SessionLocal() as db:
        rows = db.scalars(select(BagSession).where(BagSession.device_id == settings.device_id,
                                                   BagSession.source == _src(source))
                          .order_by(desc(BagSession.id)).limit(limit)).all()
    return [_row(r) for r in rows]


@app.post("/api/bag-sessions")
def new_bag(body: BagBody):
    if body.tare_g is not None and body.initial_g is not None and body.initial_g <= body.tare_g:
        raise HTTPException(400, "initial weight must be greater than tare")
    with SessionLocal() as db:
        bag = rt.pipeline.start_bag(db, tare_g=body.tare_g, initial_g=body.initial_g,
                                    expected_rate_g_min=body.expected_rate_g_min,
                                    use_current_as_initial=body.use_current_as_initial, notes=body.notes)
        db.commit()
        return _row(bag)


# ---------------------------------------------------------------------------
# calibration and ground truth


class CalBody(BaseModel):
    known_mass_g: float
    seconds: float = 15
    notes: str | None = None


@app.post("/api/calibration")
def add_calibration(body: CalBody):
    """Average the last `seconds` of accepted readings and store them against a known mass."""
    import statistics as st

    with SessionLocal() as db:
        rows = db.scalars(select(RawReading).where(
            RawReading.device_id == settings.device_id, RawReading.source == rt.source_name,
            RawReading.received_at >= _now() - timedelta(seconds=body.seconds),
            RawReading.quality.in_(GOOD_QUALITY))).all()
        ws = [r.weight_g for r in rows if r.weight_g is not None]
        if len(ws) < 3:
            raise HTTPException(409, f"only {len(ws)} valid readings in the last {body.seconds:g} s; need at least 3")
        c = CalibrationCheck(device_id=settings.device_id, source=rt.source_name, known_mass_g=body.known_mass_g,
                             measured_g=st.mean(ws), n_samples=len(ws), std_g=st.stdev(ws), notes=body.notes)
        db.add(c)
        db.commit()
        return _row(c)


@app.get("/api/calibration")
def get_calibration(group: str = "physical"):
    with SessionLocal() as db:
        return analytics.calibration_summary(db, settings.device_id, group)


class TruthBody(BaseModel):
    kind: str
    started_at: datetime
    ended_at: datetime | None = None
    notes: str | None = None


@app.get("/api/ground-truth")
def list_truth(group: str = "physical", limit: int = 200):
    with SessionLocal() as db:
        rows = db.scalars(select(GroundTruth).where(GroundTruth.device_id == settings.device_id,
                                                    GroundTruth.source == group)
                          .order_by(desc(GroundTruth.started_at)).limit(limit)).all()
    return [_row(r) for r in rows]


@app.post("/api/ground-truth")
def add_truth(body: TruthBody):
    """Experimenter labels for PHYSICAL tests only (the simulator writes its own)."""
    with SessionLocal() as db:
        g = GroundTruth(device_id=settings.device_id, source="physical", kind=body.kind,
                        started_at=body.started_at, ended_at=body.ended_at, notes=body.notes)
        db.add(g)
        db.commit()
        return _row(g)


@app.patch("/api/ground-truth/{gid}/end")
def end_truth(gid: int):
    with SessionLocal() as db:
        g = db.get(GroundTruth, gid)
        if g is None or g.source != "physical":
            raise HTTPException(404, "physical label not found")
        g.ended_at = _now()
        db.commit()
        return _row(g)


@app.delete("/api/ground-truth/{gid}")
def delete_truth(gid: int):
    with SessionLocal() as db:
        g = db.get(GroundTruth, gid)
        if g is None or g.source != "physical":
            raise HTTPException(404, "physical label not found")
        db.delete(g)
        db.commit()
    return {"deleted": gid}


@app.get("/api/analytics")
def get_analytics(group: str = "physical", hours: float = 24):
    with SessionLocal() as db:
        src = rt.source_name if analytics.group_of(rt.source_name) == group else ("simulated" if group == "simulated" else "blynk_poll")
        return {
            "evaluation": analytics.evaluate(db, settings.device_id, group),
            "summary": analytics.summary(db, settings.device_id, src, _now() - timedelta(hours=hours)),
            "calibration": analytics.calibration_summary(db, settings.device_id, group),
        }


# ---------------------------------------------------------------------------
# sensor-health model


class TrainBody(BaseModel):
    start: datetime | None = None
    end: datetime | None = None
    minutes: float = 60
    source: str | None = None


@app.post("/api/sensor-health/train")
def train(body: TrainBody):
    """Train on a period the experimenter KNOWS was sensor-normal."""
    source = _src(body.source)
    end = body.end or _now()
    start = body.start or end - timedelta(minutes=body.minutes)
    with SessionLocal() as db:
        rows = db.scalars(select(Measurement).where(
            Measurement.device_id == settings.device_id, Measurement.source == source,
            Measurement.ts >= start, Measurement.ts <= end, Measurement.quality.in_(GOOD_QUALITY))).all()
    X = [[getattr(r, f) for f in FEATURE_NAMES] for r in rows]
    X = [x for x in X if all(v is not None for v in x)]
    if len(X) < settings.if_min_train_samples:
        raise HTTPException(409, f"only {len(X)} usable samples in that period; need {settings.if_min_train_samples}")
    meta = rt.health_model.train(X, {"source": source, "source_group": analytics.group_of(source),
                                     "start": start.isoformat(), "end": end.isoformat()},
                                 settings.if_contamination)
    return meta


@app.post("/api/sensor-health/train-simulated")
def train_simulated(seconds: float = 3600):
    """Train on clean SIMULATED normal data so the AI layer can be exercised offline.
    The model is labelled 'simulated'; retrain on physical data before any real evaluation."""
    from .config import Settings
    from .simulator import normal_only

    from .processing import SignalProcessor

    cfg = Settings()
    proc = SignalProcessor(cfg.moving_avg_samples, cfg.rate_window_s, cfg.min_window_samples,
                           cfg.flat_tolerance_g, cfg.step_lookback_s)
    X = []
    t0 = datetime(2000, 1, 1, tzinfo=timezone.utc)
    for s in normal_only(seconds):
        proc.add(t0 + timedelta(seconds=s.t), s.value)
        f = proc.compute(t0 + timedelta(seconds=s.t))
        if f.window_ok and f.features():
            X.append(f.features())
    meta = rt.health_model.train(X, {"source": "simulated", "source_group": "simulated",
                                     "note_data": "generated clean normal flow + interruption"}, settings.if_contamination)
    return meta


@app.get("/api/sensor-health/model")
def model_info():
    return {"trained": rt.health_model.model is not None, "meta": rt.health_model.meta}


# ---------------------------------------------------------------------------
# Blynk connection check (read-only)


@app.get("/api/blynk/check")
def blynk_check():
    """Is the token accepted, is the device online, which pins hold values?"""
    token = blynk_token()
    if not token:
        return {"ok": False, "error": "BLYNK_TOKEN is not set in .env"}
    c = BlynkClient(token, base_url=settings.blynk_base_url)
    out: dict = {"token": c.label, "weight_pin_configured": settings.blynk_weight_pin}
    try:
        r = c.is_hardware_connected()
        out["device_online"] = r.body
        out["host"] = r.final_host
    except BlynkError as e:
        return {**out, "ok": False, "error": str(e)}
    try:
        r = c.get_all()
        out["pins"] = r.body
    except BlynkError as e:
        out["pins_error"] = str(e)
    try:
        r = c.get_pins(settings.blynk_weight_pin)
        out["weight_pin_value"] = r.body
    except BlynkError as e:
        out["weight_pin_error"] = str(e)
    out["ok"] = True
    return out


# ---------------------------------------------------------------------------
# export

EXPORTS = {
    "raw": (RawReading, RawReading.received_at),
    "measurements": (Measurement, Measurement.ts),
    "events": (Event, Event.started_at),
    "alerts": (Alert, Alert.created_at),
    "ground_truth": (GroundTruth, GroundTruth.started_at),
    "calibration": (CalibrationCheck, CalibrationCheck.created_at),
    "bag_sessions": (BagSession, BagSession.started_at),
}


@app.get("/api/export/{table}.csv")
def export(table: str, hours: float = 24, source: str | None = None):
    if table not in EXPORTS:
        raise HTTPException(404, f"unknown table; choose one of {sorted(EXPORTS)}")
    model, ts_col = EXPORTS[table]
    with SessionLocal() as db:
        q = select(model).where(ts_col >= _now() - timedelta(hours=hours))
        if hasattr(model, "device_id"):
            q = q.where(model.device_id == settings.device_id)
        if table not in ("ground_truth",) and hasattr(model, "source"):
            q = q.where(model.source == _src(source))
        rows = db.scalars(q.order_by(ts_col)).all()
    cols = [c.name for c in model.__table__.columns]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([getattr(r, c) for c in cols])
    name = f"{table}_{_src(source)}_{_now():%Y%m%d_%H%M%S}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------------------
# dashboard (built React app)

if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        f = (STATIC_DIR / path).resolve()
        if path and f.is_file() and f.is_relative_to(STATIC_DIR.resolve()):
            return FileResponse(f)
        return FileResponse(STATIC_DIR / "index.html")
