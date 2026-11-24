"""A frozen reading that starts moving again must not look like fast flow."""

from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.db import Event, SessionLocal, init_db
from app.pipeline import Pipeline

T0 = datetime(2026, 2, 1, tzinfo=timezone.utc)


def test_release_after_stuck_signal_is_not_high_rate():
    init_db()
    p = Pipeline(Settings(), "bed-resync", "simulated", None)
    with SessionLocal() as db:
        p.start_bag(db, tare_g=40, initial_g=540, expected_rate_g_min=4.0, now=T0)
        w, t = 500.0, 0.0
        frozen = None
        while t < 1200:
            w -= 4.0 / 60 * 2  # fluid keeps flowing throughout
            if 300 <= t < 800:
                frozen = frozen if frozen is not None else round(w, 1)
                v = frozen
            else:
                v = round(w + (0.3 if int(t) % 4 else -0.3), 1)
            p.ingest(db, received_at=T0 + timedelta(seconds=t), value_raw=f"{v:.1f}", value_num=v, device_online=True)
            t += 2
        db.commit()
        kinds = {e.kind for e in db.query(Event).filter(Event.device_id == "bed-resync", Event.confirmed_at.is_not(None))}
    assert "POSSIBLE_STUCK_SIGNAL" in kinds
    assert "HIGH_MASS_LOSS_RATE" not in kinds
