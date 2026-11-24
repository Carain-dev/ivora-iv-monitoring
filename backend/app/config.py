"""All settings come from environment variables (or a .env file next to the
project root). Nothing here is a secret except BLYNK_TOKEN, which is read from
the environment only and never logged, stored, or returned by the API.

Every threshold below is a PROTOTYPE value from the project requirements. They
must be validated experimentally before any result is reported.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

try:  # optional: lets a beginner put settings in a .env file
    from dotenv import load_dotenv

    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env",
                      Path(__file__).resolve().parents[2] / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)
            break
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default, cast=str):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return cast(raw.strip())


@dataclass
class Settings:
    # ---- data source -------------------------------------------------------
    # "blynk" polls the real device; "simulator" generates clearly labelled
    # simulated data so the system can be exercised offline.
    data_source: str = field(default_factory=lambda: _env("DATA_SOURCE", "simulator"))
    device_id: str = field(default_factory=lambda: _env("DEVICE_ID", "bed1"))

    # ---- Blynk -------------------------------------------------------------
    blynk_base_url: str = field(default_factory=lambda: _env("BLYNK_BASE_URL", "https://blynk.cloud"))
    # Which virtual pin carries the weight. UNVERIFIED until checked with
    # /api/blynk/check or probe.py; the link supplied used V1.
    blynk_weight_pin: str = field(default_factory=lambda: _env("BLYNK_WEIGHT_PIN", "V1"))
    poll_interval_s: float = field(default_factory=lambda: _env("POLL_INTERVAL_S", 2.0, float))
    online_check_interval_s: float = field(default_factory=lambda: _env("ONLINE_CHECK_INTERVAL_S", 10.0, float))

    # ---- units and calibration --------------------------------------------
    # grams = raw_value * WEIGHT_SCALE + WEIGHT_OFFSET_G
    weight_scale: float = field(default_factory=lambda: _env("WEIGHT_SCALE", 1.0, float))
    weight_offset_g: float = field(default_factory=lambda: _env("WEIGHT_OFFSET_G", 0.0, float))
    # Set to true only after the unit has been confirmed with known masses.
    units_verified: bool = field(default_factory=lambda: _env("UNITS_VERIFIED", False, bool))
    min_valid_g: float = field(default_factory=lambda: _env("MIN_VALID_G", -50.0, float))
    load_cell_capacity_g: float = field(default_factory=lambda: _env("LOAD_CELL_CAPACITY_G", 1000.0, float))

    # ---- storage -----------------------------------------------------------
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///./iv_monitor.db"))
    model_dir: str = field(default_factory=lambda: _env("MODEL_DIR", "./models"))

    # ---- signal processing -------------------------------------------------
    moving_avg_samples: int = field(default_factory=lambda: _env("MOVING_AVG_SAMPLES", 5, int))
    rate_window_s: float = field(default_factory=lambda: _env("RATE_WINDOW_S", 60.0, float))
    min_window_samples: int = field(default_factory=lambda: _env("MIN_WINDOW_SAMPLES", 5, int))
    flat_tolerance_g: float = field(default_factory=lambda: _env("FLAT_TOLERANCE_G", 0.0, float))

    # ---- data freshness ----------------------------------------------------
    fresh_s: float = field(default_factory=lambda: _env("FRESH_S", 10.0, float))
    stale_s: float = field(default_factory=lambda: _env("STALE_S", 30.0, float))

    # ---- rule thresholds (prototype, validate experimentally) -------------
    interruption_rate_g_min: float = field(default_factory=lambda: _env("INTERRUPTION_RATE_G_MIN", 0.5, float))
    interruption_persist_s: float = field(default_factory=lambda: _env("INTERRUPTION_PERSIST_S", 120.0, float))
    high_rate_abs_g_min: float = field(default_factory=lambda: _env("HIGH_RATE_ABS_G_MIN", 8.0, float))
    high_rate_factor: float = field(default_factory=lambda: _env("HIGH_RATE_FACTOR", 2.0, float))
    high_rate_persist_s: float = field(default_factory=lambda: _env("HIGH_RATE_PERSIST_S", 60.0, float))
    bag_jump_g: float = field(default_factory=lambda: _env("BAG_JUMP_G", 50.0, float))
    step_lookback_s: float = field(default_factory=lambda: _env("STEP_LOOKBACK_S", 10.0, float))
    bag_stable_s: float = field(default_factory=lambda: _env("BAG_STABLE_S", 30.0, float))
    stable_std_g: float = field(default_factory=lambda: _env("STABLE_STD_G", 2.0, float))
    low_volume_pct: float = field(default_factory=lambda: _env("LOW_VOLUME_PCT", 15.0, float))
    low_volume_persist_s: float = field(default_factory=lambda: _env("LOW_VOLUME_PERSIST_S", 10.0, float))
    weight_gain_g_min: float = field(default_factory=lambda: _env("WEIGHT_GAIN_G_MIN", 0.5, float))
    # A rule's persistence timer survives brief lapses of its condition up to
    # this long, so rate jitter around a threshold does not restart it.
    rule_grace_s: float = field(default_factory=lambda: _env("RULE_GRACE_S", 20.0, float))

    # ---- rate noise (low-false-alarm guards) ------------------------------
    # Smallest weight step the device reports. Blynk V1 was observed in whole
    # grams on 2026-10-11, so 1 g. A weight gain slower than
    # RESOLUTION / RATE_WINDOW cannot be resolved and is not flagged.
    weight_resolution_g: float = field(default_factory=lambda: _env("WEIGHT_RESOLUTION_G", 1.0, float))
    # A rate must exceed RATE_SIGMA_K standard errors of the fitted slope to
    # count as a significant weight gain or a high mass-loss rate.
    rate_sigma_k: float = field(default_factory=lambda: _env("RATE_SIGMA_K", 3.0, float))

    # ---- alarms (prototype values, NOT clinically validated) --------------
    # Infusion alarms (interruption, high rate, low volume) are armed only
    # while a bag session exists, so a load placed during setup is not alarmed.
    alarms_require_bag: bool = field(default_factory=lambda: _env("ALARMS_REQUIRE_BAG", True, bool))
    # Recovery must hold this long before an alarm clears (hysteresis).
    alarm_clear_s: float = field(default_factory=lambda: _env("ALARM_CLEAR_S", 30.0, float))
    # FLOW_INTERRUPTION clears only when the rate is at least this.
    interruption_clear_g_min: float = field(default_factory=lambda: _env("INTERRUPTION_CLEAR_G_MIN", 1.0, float))
    # HIGH_MASS_LOSS_RATE clears only when rate <= this fraction of its threshold.
    high_rate_clear_fraction: float = field(default_factory=lambda: _env("HIGH_RATE_CLEAR_FRACTION", 0.8, float))
    # LOW_VOLUME clears only when remaining >= LOW_VOLUME_PCT + this margin.
    low_volume_clear_margin_pct: float = field(default_factory=lambda: _env("LOW_VOLUME_CLEAR_MARGIN_PCT", 5.0, float))
    # Sensor and data-loss conditions must hold this long to raise an alarm.
    alarm_sensor_persist_s: float = field(default_factory=lambda: _env("ALARM_SENSOR_PERSIST_S", 60.0, float))
    # Low-priority sensor alarms (noise, drift) become medium after this long.
    alarm_escalate_s: float = field(default_factory=lambda: _env("ALARM_ESCALATE_S", 600.0, float))
    # A same-type alarm that recurs within this time of clearing reopens the
    # previous alarm instead of creating a new one.
    alarm_rearm_s: float = field(default_factory=lambda: _env("ALARM_REARM_S", 300.0, float))
    # An acknowledged HIGH alarm that is still active sounds again after this
    # long (0 = never).
    alarm_resound_s: float = field(default_factory=lambda: _env("ALARM_RESOUND_S", 300.0, float))

    # ---- sensor health heuristics -----------------------------------------
    stuck_s: float = field(default_factory=lambda: _env("STUCK_S", 300.0, float))
    noise_std_g: float = field(default_factory=lambda: _env("NOISE_STD_G", 3.0, float))
    drift_g: float = field(default_factory=lambda: _env("DRIFT_G", 5.0, float))
    if_contamination: float = field(default_factory=lambda: _env("IF_CONTAMINATION", 0.02, float))
    if_min_train_samples: int = field(default_factory=lambda: _env("IF_MIN_TRAIN_SAMPLES", 200, int))

    # ---- simulator ---------------------------------------------------------
    sim_interval_s: float = field(default_factory=lambda: _env("SIM_INTERVAL_S", 1.0, float))
    sim_seed: int = field(default_factory=lambda: _env("SIM_SEED", 7, int))

    # ---- web ---------------------------------------------------------------
    cors_origins: str = field(default_factory=lambda: _env("CORS_ORIGINS", "http://localhost:5173"))

    def public(self) -> dict:
        """Settings safe to show on the dashboard (no token, no DB password)."""
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        out.pop("database_url", None)
        out["database"] = "postgresql" if self.database_url.startswith("postgres") else "sqlite"
        out["blynk_token_set"] = bool(blynk_token())
        return out


def blynk_token() -> str:
    """The device token. Environment only; never stored or returned."""
    return os.environ.get("BLYNK_TOKEN", "").strip()


settings = Settings()
