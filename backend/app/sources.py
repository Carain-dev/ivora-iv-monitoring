"""Data sources: poll the real Blynk device, or run the labelled simulator.

Each source runs in one background thread and feeds its Pipeline.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select

from .blynk_client import BlynkApiError, BlynkClient, BlynkError, BlynkUnavailable
from .config import Settings, blynk_token
from .db import GroundTruth, SessionLocal
from .pipeline import Pipeline, parse_number
from . import simulator as sim

log = logging.getLogger("iv_monitor.sources")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def extract_pin_value(body, pin: str):
    """Blynk `get` for one pin: accept a bare value, a one-item list, or {"v1": value}."""
    if isinstance(body, dict):
        for k, v in body.items():
            if k.lower() == pin.lower():
                return v
        return None
    return body


class SourceStatus:
    def __init__(self):
        self.running = False
        self.configured = True
        self.message = ""
        self.last_poll_at: datetime | None = None
        self.last_ok_at: datetime | None = None
        self.last_error: str | None = None
        self.device_online: bool | None = None
        self.online_checked_at: datetime | None = None
        self.change_times: list[float] = []
        self.polls = 0
        self.errors = 0

    def observed_interval_s(self) -> float | None:
        t = self.change_times[-60:]
        gaps = [b - a for a, b in zip(t, t[1:])]
        return round(statistics.median(gaps), 2) if gaps else None

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "configured": self.configured,
            "message": self.message,
            "last_poll_at": self.last_poll_at,
            "last_ok_at": self.last_ok_at,
            "last_error": self.last_error,
            "device_online": self.device_online,
            "online_checked_at": self.online_checked_at,
            "observed_update_interval_s": self.observed_interval_s(),
            "polls": self.polls,
            "errors": self.errors,
        }


class BaseSource(threading.Thread):
    def __init__(self, cfg: Settings, pipeline: Pipeline):
        super().__init__(daemon=True, name=self.__class__.__name__)
        self.cfg = cfg
        self.pipeline = pipeline
        self.status = SourceStatus()
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def _ingest(self, **kw):
        with SessionLocal() as db:
            m = self.pipeline.ingest(db, **kw)
            db.commit()
            return m


class BlynkSource(BaseSource):
    """Polls the documented read endpoints only (no writes to the device)."""

    def run(self):
        token = blynk_token()
        st = self.status
        if not token:
            st.configured = False
            st.message = "BLYNK_TOKEN is not set. Put it in the .env file and restart."
            log.warning(st.message)
            return
        client = BlynkClient(token, base_url=self.cfg.blynk_base_url)
        st.running = True
        st.message = f"polling pin {self.cfg.blynk_weight_pin} every {self.cfg.poll_interval_s:g} s (token {client.label})"
        log.info(st.message)
        next_online = 0.0
        last_val = None
        while not self._stop.is_set():
            t0 = time.monotonic()
            if t0 >= next_online:
                try:
                    r = client.is_hardware_connected()
                    st.device_online = r.body is True or str(r.body).strip().lower() == "true"
                    st.online_checked_at = r.received_at
                except BlynkError as e:
                    st.device_online = None
                    st.last_error = str(e)
                next_online = t0 + self.cfg.online_check_interval_s

            api_error = None
            value_raw = None
            value_num = None
            received_at = utcnow()
            try:
                r = client.get_pins(self.cfg.blynk_weight_pin)
                received_at = r.received_at
                v = extract_pin_value(r.body, self.cfg.blynk_weight_pin)
                value_raw = r.raw_text.strip()[:200]
                value_num = parse_number(v)
                st.last_ok_at = received_at
                if value_raw != last_val:
                    st.change_times.append(received_at.timestamp())
                    st.change_times = st.change_times[-120:]
                    last_val = value_raw
            except BlynkApiError as e:
                api_error = f"Blynk error {e.status}: {e.message}"
            except BlynkUnavailable as e:
                api_error = f"Blynk unreachable: {e}"
            except BlynkError as e:
                api_error = str(e)
            if api_error:
                st.errors += 1
                st.last_error = api_error
            st.polls += 1
            st.last_poll_at = received_at
            try:
                self._ingest(
                    received_at=received_at,
                    value_raw=value_raw,
                    value_num=value_num,
                    device_online=st.device_online,
                    api_error=api_error,
                    pin=self.cfg.blynk_weight_pin,
                )
            except Exception:  # keep polling even if one write fails
                log.exception("pipeline failed for one reading")
            self._stop.wait(max(0.0, self.cfg.poll_interval_s - (time.monotonic() - t0)))
        st.running = False


class SimulatorSource(BaseSource):
    """Clearly labelled simulated data. Writes simulated ground truth too."""

    def run(self):
        st = self.status
        st.running = True
        st.device_online = True
        st.message = "SIMULATED DATA: no real device is being read"
        cfg = self.cfg
        with SessionLocal() as db:
            self.pipeline.proc.reset()
            self.pipeline.start_bag(
                db, tare_g=sim.TARE_G, initial_g=sim.INITIAL_G, expected_rate_g_min=sim.NORMAL_RATE,
                initial_source="simulated", notes="simulated bag (starts partly used)",
            )
            db.commit()
        gt_open: dict[str, int] = {}
        current_phase_gt: int | None = None
        for s in sim.generate(cfg.sim_interval_s, cfg.sim_seed):
            if self._stop.is_set():
                break
            t0 = time.monotonic()
            now = utcnow()
            with SessionLocal() as db:
                if s.phase_start:
                    if current_phase_gt:
                        g = db.get(GroundTruth, current_phase_gt)
                        g.ended_at = now
                    g = GroundTruth(device_id=self.pipeline.device_id, source="simulated", kind=s.label,
                                    started_at=now, notes="written by the simulator")
                    db.add(g)
                    db.flush()
                    current_phase_gt = g.id
                    if s.label == "BAG_REPLACEMENT" and "LOW_VOLUME" in gt_open:
                        db.get(GroundTruth, gt_open.pop("LOW_VOLUME")).ended_at = now
                remaining = 100 * (s.true_g - sim.TARE_G) / (sim.INITIAL_G - sim.TARE_G)
                if 0 < remaining < cfg.low_volume_pct and "LOW_VOLUME" not in gt_open and s.true_g > sim.TARE_G * 0.5:
                    g = GroundTruth(device_id=self.pipeline.device_id, source="simulated", kind="LOW_VOLUME",
                                    started_at=now, notes="written by the simulator")
                    db.add(g)
                    db.flush()
                    gt_open["LOW_VOLUME"] = g.id
                self.pipeline.ingest(
                    db,
                    received_at=now,
                    value_raw=None if s.value is None else f"{s.value}",
                    value_num=s.value,
                    device_online=True,
                    api_error="simulated API failure" if s.value is None else None,
                    pin="SIM",
                    sim_scenario=s.label,
                )
                db.commit()
            st.polls += 1
            st.last_poll_at = now
            if s.value is not None:
                st.last_ok_at = now
            self._stop.wait(max(0.0, cfg.sim_interval_s - (time.monotonic() - t0)))
        st.running = False


def close_open_simulated_truth(device_id: str) -> None:
    with SessionLocal() as db:
        for g in db.scalars(select(GroundTruth).where(
            GroundTruth.device_id == device_id, GroundTruth.source == "simulated", GroundTruth.ended_at.is_(None)
        )):
            g.ended_at = utcnow()
        db.commit()
