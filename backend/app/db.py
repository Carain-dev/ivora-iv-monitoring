"""Database tables. PostgreSQL in Docker; SQLite works for a quick local run.

Every table that holds measurements carries `source` ('blynk_poll',
'blynk_history' or 'simulated') so simulated data can never be mixed into
physical results.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


TS = DateTime(timezone=True)
ID = BigInteger().with_variant(Integer, "sqlite")  # SQLite autoincrement needs INTEGER


class RawReading(Base):
    """Exactly what we received, before any processing."""

    __tablename__ = "raw_reading"
    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    pin: Mapped[str | None] = mapped_column(String(8))
    value_raw: Mapped[str | None] = mapped_column(Text)
    value_num: Mapped[float | None] = mapped_column(Float)
    weight_g: Mapped[float | None] = mapped_column(Float)  # value_num * scale + offset
    unit: Mapped[str] = mapped_column(String(16))  # 'g' or 'g (unverified)'
    source_ts: Mapped[datetime | None] = mapped_column(TS)  # device/Blynk time when known
    received_at: Mapped[datetime] = mapped_column(TS, index=True)  # our receipt time
    source: Mapped[str] = mapped_column(String(16), index=True)
    device_online: Mapped[bool | None] = mapped_column(Boolean)
    quality: Mapped[str] = mapped_column(String(24))
    error: Mapped[str | None] = mapped_column(Text)
    sim_scenario: Mapped[str | None] = mapped_column(String(48))  # ground truth for simulated rows


class BagSession(Base):
    """One bag on one bed: tare, initial filled weight and expected rate."""

    __tablename__ = "bag_session"
    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(TS)
    tare_g: Mapped[float | None] = mapped_column(Float)  # empty bag + hanger
    initial_g: Mapped[float | None] = mapped_column(Float)  # filled bag at start
    expected_rate_g_min: Mapped[float | None] = mapped_column(Float)
    initial_source: Mapped[str] = mapped_column(String(24), default="manual")  # manual | auto_stable | bag_replacement
    source: Mapped[str] = mapped_column(String(16))
    notes: Mapped[str | None] = mapped_column(Text)


class Measurement(Base):
    """Processed values computed by the backend for one accepted raw reading."""

    __tablename__ = "measurement"
    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    raw_id: Mapped[int | None] = mapped_column(ForeignKey("raw_reading.id"))
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[datetime] = mapped_column(TS, index=True)
    source: Mapped[str] = mapped_column(String(16), index=True)
    bag_session_id: Mapped[int | None] = mapped_column(ForeignKey("bag_session.id"))
    weight_g: Mapped[float | None] = mapped_column(Float)
    weight_filtered_g: Mapped[float | None] = mapped_column(Float)
    rate_g_min: Mapped[float | None] = mapped_column(Float)
    remaining_pct: Mapped[float | None] = mapped_column(Float)
    slope_g_s: Mapped[float | None] = mapped_column(Float)
    rolling_std_g: Mapped[float | None] = mapped_column(Float)
    rolling_var_g2: Mapped[float | None] = mapped_column(Float)
    noise_g: Mapped[float | None] = mapped_column(Float)
    baseline_dev_g: Mapped[float | None] = mapped_column(Float)
    flat_duration_s: Mapped[float | None] = mapped_column(Float)
    r2: Mapped[float | None] = mapped_column(Float)
    window_s: Mapped[float | None] = mapped_column(Float)
    window_n: Mapped[int | None] = mapped_column(Integer)
    data_age_s: Mapped[float | None] = mapped_column(Float)
    quality: Mapped[str] = mapped_column(String(24))
    flow_state: Mapped[str] = mapped_column(String(32))
    flow_state_status: Mapped[str] = mapped_column(String(16))  # candidate | confirmed
    low_volume: Mapped[bool] = mapped_column(Boolean, default=False)
    sensor_state: Mapped[str] = mapped_column(String(32))
    anomaly_index: Mapped[float | None] = mapped_column(Float)
    evidence_score: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict | None] = mapped_column(JSON)


class Event(Base):
    """A detected condition with a start and (once cleared) an end."""

    __tablename__ = "event"
    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(16), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # FLOW_INTERRUPTION, POSSIBLE_DRIFT ...
    layer: Mapped[str] = mapped_column(String(16))  # rule | sensor_health
    status: Mapped[str] = mapped_column(String(16))  # candidate | confirmed | ended | dismissed
    started_at: Mapped[datetime] = mapped_column(TS)
    confirmed_at: Mapped[datetime | None] = mapped_column(TS)
    ended_at: Mapped[datetime | None] = mapped_column(TS)
    reason: Mapped[str | None] = mapped_column(Text)
    evidence_score: Mapped[float | None] = mapped_column(Float)
    evidence: Mapped[dict | None] = mapped_column(JSON)


class Alert(Base):
    __tablename__ = "alert"
    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id"), index=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(16), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16))  # candidate | confirmed | acknowledged | resolved
    severity: Mapped[str] = mapped_column(String(16))  # alarm priority: info | low | medium | high
    created_at: Mapped[datetime] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS)
    confirmed_at: Mapped[datetime | None] = mapped_column(TS)
    acknowledged_at: Mapped[datetime | None] = mapped_column(TS)
    acknowledged_by: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(TS)
    weight_g: Mapped[float | None] = mapped_column(Float)
    rate_g_min: Mapped[float | None] = mapped_column(Float)
    remaining_pct: Mapped[float | None] = mapped_column(Float)
    evidence_score: Mapped[float | None] = mapped_column(Float)
    sensor_status: Mapped[str | None] = mapped_column(String(32))
    message: Mapped[str | None] = mapped_column(Text)
    recurrences: Mapped[int | None] = mapped_column(Integer, default=0)  # times reopened after clearing


class GroundTruth(Base):
    """Experimenter's labels: what really happened during a test."""

    __tablename__ = "ground_truth"
    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(16))  # physical | simulated
    kind: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(TS)
    ended_at: Mapped[datetime | None] = mapped_column(TS)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class CalibrationCheck(Base):
    """A known mass placed on the load cell and what the system measured."""

    __tablename__ = "calibration_check"
    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16))
    known_mass_g: Mapped[float] = mapped_column(Float)
    measured_g: Mapped[float] = mapped_column(Float)
    n_samples: Mapped[int] = mapped_column(Integer)
    std_g: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    notes: Mapped[str | None] = mapped_column(Text)


def make_engine(url: str | None = None):
    url = url or settings.database_url
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _wal(dbapi_conn, _):  # pragma: no cover - sqlite tuning only
            dbapi_conn.execute("PRAGMA journal_mode=WAL")

    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(eng=None) -> None:
    eng = eng or engine
    Base.metadata.create_all(eng)
    _add_missing_columns(eng)


def _add_missing_columns(eng) -> None:
    """create_all() never changes existing tables, so add columns introduced by
    later versions (e.g. alert.recurrences) to a database created earlier.
    New columns are always nullable; no data is changed or removed."""
    insp = inspect(eng)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in have:
                with eng.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(eng.dialect)}"))
