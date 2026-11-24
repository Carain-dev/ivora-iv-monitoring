"""Minimal AI sensor-health layer (requirements §7).

Two parts, reported together:

1. Transparent heuristic checks (always on):
     POSSIBLE_STUCK_SIGNAL  reading unchanged (within FLAT_TOLERANCE_G) for >= STUCK_S
     ABNORMAL_NOISE         residual noise around the trend > NOISE_STD_G
     POSSIBLE_DRIFT         |baseline deviation| > DRIFT_G without a bag step,
                            or weight rising faster than rules.gain_threshold
                            (which ignores whole-gram rounding jitter)
2. An Isolation Forest trained ONLY on data the experimenter marks as normal.
   Its output is an anomaly INDEX: -decision_function(x). Values above 0 mean
   "more isolated than the training contamination cut-off"; larger is more
   unusual. It is NOT a probability and is not calibrated. When it flags a
   sample the heuristics did not, the state is chosen from the feature that
   deviates most (in training standard deviations) from the training data.

A flat signal can be a real flow interruption OR a stuck sensor. The rule
engine judges the infusion, this layer judges the sensor, and the pipeline
reports the ambiguity when both point at a flat signal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .config import Settings
from .processing import FEATURE_NAMES, Stats
from .rules import gain_threshold

SENSOR_STATES = ["SENSOR_NORMAL", "POSSIBLE_DRIFT", "POSSIBLE_STUCK_SIGNAL", "ABNORMAL_NOISE", "SENSOR_UNCERTAIN"]
FEATURE_TO_STATE = {
    "slope_g_s": "POSSIBLE_DRIFT",
    "baseline_dev_g": "POSSIBLE_DRIFT",
    "rolling_std_g": "ABNORMAL_NOISE",
    "rolling_var_g2": "ABNORMAL_NOISE",
    "noise_g": "ABNORMAL_NOISE",
    "flat_duration_s": "POSSIBLE_STUCK_SIGNAL",
}


@dataclass
class HealthResult:
    state: str
    reason: str
    anomaly_index: float | None
    method: str  # heuristic | isolation_forest | data_quality | none


class SensorHealthModel:
    """Wraps a trained IsolationForest plus the metadata that says what it was trained on."""

    def __init__(self, model_dir: str):
        self.dir = Path(model_dir)
        self.model = None
        self.meta: dict | None = None
        self.mean = None
        self.std = None
        self.load()

    @property
    def path(self) -> Path:
        return self.dir / "isolation_forest.joblib"

    def load(self) -> None:
        import joblib

        if self.path.exists():
            blob = joblib.load(self.path)
            self.model, self.meta = blob["model"], blob["meta"]
            self.mean, self.std = np.array(blob["mean"]), np.array(blob["std"])

    def train(self, X: list[list[float]], meta: dict, contamination: float, seed: int = 0) -> dict:
        import joblib
        from sklearn.ensemble import IsolationForest

        arr = np.asarray(X, dtype=float)
        model = IsolationForest(n_estimators=200, contamination=contamination, random_state=seed)
        model.fit(arr)
        scores = -model.decision_function(arr)
        meta = {
            **meta,
            "n_samples": int(arr.shape[0]),
            "features": FEATURE_NAMES,
            "contamination": contamination,
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "train_index_p50": float(np.median(scores)),
            "train_index_p99": float(np.percentile(scores, 99)),
            "note": "anomaly_index = -IsolationForest.decision_function; >0 means flagged; not a probability",
        }
        self.dir.mkdir(parents=True, exist_ok=True)
        mean, std = arr.mean(axis=0), arr.std(axis=0) + 1e-9
        joblib.dump({"model": model, "meta": meta, "mean": mean.tolist(), "std": std.tolist()}, self.path)
        (self.dir / "isolation_forest.meta.json").write_text(json.dumps(meta, indent=2))
        self.model, self.meta, self.mean, self.std = model, meta, mean, std
        return meta

    def score(self, feats: list[float]) -> tuple[float, str]:
        x = np.asarray([feats], dtype=float)
        index = float(-self.model.decision_function(x)[0])
        z = np.abs((x[0] - self.mean) / self.std)
        worst = FEATURE_NAMES[int(np.argmax(z))]
        return index, worst


def assess(
    cfg: Settings,
    stats: Stats,
    quality_problem: str | None,
    step_recent: bool,
    model: SensorHealthModel | None,
) -> HealthResult:
    if quality_problem:
        return HealthResult("SENSOR_UNCERTAIN", quality_problem, None, "data_quality")
    feats = stats.features()
    index = None
    worst = None
    if model is not None and model.model is not None and feats is not None:
        index, worst = model.score(feats)

    if stats.flat_duration_s is not None and stats.flat_duration_s >= cfg.stuck_s:
        return HealthResult(
            "POSSIBLE_STUCK_SIGNAL",
            f"reading unchanged for {stats.flat_duration_s:.0f} s (>= {cfg.stuck_s:g} s)",
            index, "heuristic",
        )
    if stats.noise_g is not None and stats.window_ok and stats.noise_g > cfg.noise_std_g:
        return HealthResult(
            "ABNORMAL_NOISE",
            f"noise around trend {stats.noise_g:.2f} g (> {cfg.noise_std_g:g} g)",
            index, "heuristic",
        )
    if (
        not step_recent
        and stats.baseline_dev_g is not None
        and abs(stats.baseline_dev_g) > cfg.drift_g
        and stats.window_ok
    ):
        return HealthResult(
            "POSSIBLE_DRIFT",
            f"baseline moved {stats.baseline_dev_g:+.1f} g from the recent trend (> {cfg.drift_g:g} g)",
            index, "heuristic",
        )
    if stats.rate_g_min is not None and stats.window_ok and stats.rate_g_min < -gain_threshold(cfg, stats) and not step_recent:
        return HealthResult(
            "POSSIBLE_DRIFT",
            f"weight rising at {-stats.rate_g_min:.2f} g/min without a bag change",
            index, "heuristic",
        )
    if index is not None and index > 0 and not step_recent:
        state = FEATURE_TO_STATE[worst]
        return HealthResult(
            state,
            f"isolation forest anomaly index {index:.3f} (> 0); most unusual feature: {worst}",
            index, "isolation_forest",
        )
    if feats is None:
        return HealthResult("SENSOR_UNCERTAIN", "not enough data to assess the sensor", index, "none")
    return HealthResult("SENSOR_NORMAL", "no sensor issue detected", index, "heuristic" if index is None else "isolation_forest")
