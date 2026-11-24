"""Transparent evidence score (requirements §8).

  evidence = 100 x (0.30 x persistence
                  + 0.20 x trend_consistency
                  + 0.15 x signal_quality
                  + 0.20 x freshness
                  + 0.15 x sensor_health)

Each factor is between 0 and 1:

  persistence        time the condition has held / time required to confirm
                     (1 for conditions that confirm immediately)
  trend_consistency  R^2 of the straight-line fit for rate-based states
                     (NORMAL_FLOW, HIGH_MASS_LOSS_RATE); for FLOW_INTERRUPTION
                     1 - |rate| / interruption threshold, since a flat line has
                     no meaningful R^2; 1 for other states
  signal_quality     1 - noise / NOISE_STD_G (0 when noise reaches the limit)
  freshness          1 if data age <= FRESH_S, falling linearly to 0 at STALE_S
  sensor_health      1 SENSOR_NORMAL, 0.4 POSSIBLE_* or ABNORMAL_NOISE,
                     0 SENSOR_UNCERTAIN

The weights are design choices, not fitted values. The score ranks how much
the CURRENT DATA supports an event; it is not a probability and has not been
calibrated against ground truth.
"""

from __future__ import annotations

WEIGHTS = {
    "persistence": 0.30,
    "trend_consistency": 0.20,
    "signal_quality": 0.15,
    "freshness": 0.20,
    "sensor_health": 0.15,
}
SENSOR_FACTOR = {"SENSOR_NORMAL": 1.0, "SENSOR_UNCERTAIN": 0.0}


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def freshness(data_age_s: float | None, fresh_s: float, stale_s: float) -> float:
    if data_age_s is None:
        return 0.0
    if data_age_s <= fresh_s:
        return 1.0
    if data_age_s >= stale_s:
        return 0.0
    return 1.0 - (data_age_s - fresh_s) / (stale_s - fresh_s)


def score(
    *,
    kind: str,
    held_s: float,
    required_s: float,
    r2: float | None,
    rate_g_min: float | None,
    interruption_rate_g_min: float,
    noise_g: float | None,
    noise_limit_g: float,
    data_age_s: float | None,
    fresh_s: float,
    stale_s: float,
    sensor_state: str,
) -> dict:
    persistence = 1.0 if required_s <= 0 else _clip(held_s / required_s)
    if kind in ("NORMAL_FLOW", "HIGH_MASS_LOSS_RATE"):
        consistency = _clip(r2) if r2 is not None else 0.0
    elif kind == "FLOW_INTERRUPTION":
        consistency = _clip(1 - abs(rate_g_min or 0) / interruption_rate_g_min) if interruption_rate_g_min > 0 else 0.0
    else:
        consistency = 1.0
    quality = _clip(1 - (noise_g / noise_limit_g)) if noise_g is not None and noise_limit_g > 0 else 0.0
    factors = {
        "persistence": round(persistence, 3),
        "trend_consistency": round(consistency, 3),
        "signal_quality": round(quality, 3),
        "freshness": round(freshness(data_age_s, fresh_s, stale_s), 3),
        "sensor_health": SENSOR_FACTOR.get(sensor_state, 0.4),
    }
    total = 100 * sum(WEIGHTS[k] * v for k, v in factors.items())
    return {"score": round(total, 1), "factors": factors, "weights": WEIGHTS}
