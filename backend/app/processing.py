"""Signal processing: filtering, mass-loss rate, remaining %, signal statistics.

Formulas (requirements §5):

  Mass-loss rate (g/min) = -60 x weight change (g) / elapsed time (s)

  We estimate "weight change / elapsed time" as the least-squares slope of the
  raw weights against their actual timestamps over the last RATE_WINDOW_S
  seconds. For two points this is exactly the formula above; with more points
  it averages out load-cell noise and handles irregular sampling, because
  each sample sits at its real time rather than an assumed fixed interval.

  Remaining (%) = 100 x (current weight - tare) / (initial weight - tare)

  "current weight" is the moving-average-filtered weight.

Mass-loss rate is NOT a volumetric flow rate. Converting needs the fluid's
density (about 1.00 g/mL for water, about 1.005 g/mL for 0.9 % saline at room
temperature); we do not apply any conversion in the backend.

Other statistics over the same window:
  rolling_std / rolling_var   spread of raw weights (includes the trend)
  noise_g                     standard deviation of residuals around the fitted
                              line, i.e. noise with the trend removed
  r2                          how well a straight line explains the window (trend consistency)
  baseline_dev_g              mean of the last BASELINE_RECENT_S seconds minus the
                              value the PREVIOUS window's line predicts for now;
                              a sudden offset or drift shows up here
  flat_duration_s             how long the reading has stayed within
                              FLAT_TOLERANCE_G of the current value
  rate_se_g_min               standard error of the mass-loss rate (60 x the
                              standard error of the fitted slope); the rules
                              use it to ignore rate jitter caused by noise and
                              by whole-gram rounding
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

BASELINE_RECENT_S = 10.0
STEP_RECENT_S = 3.0
MAX_BUFFER_S = 900.0  # keep 15 minutes of samples in memory


@dataclass
class Sample:
    ts: datetime
    weight_g: float


@dataclass
class Fit:
    slope_g_s: float
    intercept: float
    t0: float
    r2: float | None
    noise_g: float
    n: int
    span_s: float
    slope_se: float = 0.0

    def predict(self, t: float) -> float:
        return self.intercept + self.slope_g_s * (t - self.t0)


def linear_fit(ts: list[float], ws: list[float]) -> Fit | None:
    if len(ts) < 2:
        return None
    t = np.asarray(ts, dtype=float)
    w = np.asarray(ws, dtype=float)
    t0 = t[0]
    x = t - t0
    if np.ptp(x) <= 0:
        return None
    slope, intercept = np.polyfit(x, w, 1)
    resid = w - (intercept + slope * x)
    ss_res = float(np.sum(resid**2))
    ss_tot = float(np.sum((w - w.mean()) ** 2))
    r2 = None if ss_tot <= 1e-12 else max(0.0, 1.0 - ss_res / ss_tot)
    dof = max(1, len(w) - 2)
    sxx = float(np.sum((x - x.mean()) ** 2))
    return Fit(
        slope_se=math.sqrt(ss_res / dof / sxx) if sxx > 0 else 0.0,
        slope_g_s=float(slope),
        intercept=float(intercept),
        t0=float(t0),
        r2=r2,
        noise_g=math.sqrt(ss_res / dof),
        n=len(w),
        span_s=float(x[-1]),
    )


def rate_from_slope(slope_g_s: float) -> float:
    """Mass-loss rate in g/min; positive while the bag is emptying."""
    return -60.0 * slope_g_s


def remaining_pct(current_g: float | None, tare_g: float | None, initial_g: float | None) -> float | None:
    if current_g is None or tare_g is None or initial_g is None:
        return None
    full = initial_g - tare_g
    if full <= 0:
        return None
    return 100.0 * (current_g - tare_g) / full


@dataclass
class Stats:
    weight_g: float | None = None
    weight_filtered_g: float | None = None
    slope_g_s: float | None = None
    rate_g_min: float | None = None
    rate_se_g_min: float | None = None
    rolling_std_g: float | None = None
    rolling_var_g2: float | None = None
    noise_g: float | None = None
    r2: float | None = None
    baseline_dev_g: float | None = None
    flat_duration_s: float | None = None
    window_s: float | None = None
    window_n: int = 0
    step_g: float | None = None  # recent mean minus mean ~STEP_LOOKBACK_S ago
    window_ok: bool = False  # enough data for rate-based rules

    def features(self) -> list[float] | None:
        """Feature vector for the sensor-health model (None if incomplete)."""
        vals = [
            self.slope_g_s,
            self.rolling_std_g,
            self.rolling_var_g2,
            self.noise_g,
            self.baseline_dev_g,
            self.flat_duration_s,
        ]
        if any(v is None or not math.isfinite(v) for v in vals):
            return None
        return [float(v) for v in vals]


FEATURE_NAMES = ["slope_g_s", "rolling_std_g", "rolling_var_g2", "noise_g", "baseline_dev_g", "flat_duration_s"]


@dataclass
class SignalProcessor:
    """Keeps the recent accepted samples for one device and computes stats."""

    moving_avg_samples: int = 5
    rate_window_s: float = 60.0
    min_window_samples: int = 5
    flat_tolerance_g: float = 0.0
    step_lookback_s: float = 10.0
    buf: deque = field(default_factory=deque)
    flat_ref: float | None = None
    flat_since: datetime | None = None

    def reset(self, keep_after: datetime | None = None) -> None:
        """Drop samples (all, or those before keep_after), e.g. after a weight step."""
        if keep_after is None:
            self.buf.clear()
        else:
            while self.buf and self.buf[0].ts < keep_after:
                self.buf.popleft()

    def add(self, ts: datetime, weight_g: float) -> None:
        self.buf.append(Sample(ts, weight_g))
        while self.buf and (ts - self.buf[0].ts).total_seconds() > MAX_BUFFER_S:
            self.buf.popleft()
        if self.flat_ref is None or abs(weight_g - self.flat_ref) > self.flat_tolerance_g:
            self.flat_ref = weight_g
            self.flat_since = ts

    def compute(self, now: datetime) -> Stats:
        s = Stats()
        if not self.buf:
            return s
        last = self.buf[-1]
        s.weight_g = last.weight_g
        recent = [x.weight_g for x in list(self.buf)[-self.moving_avg_samples :]]
        s.weight_filtered_g = float(np.mean(recent))
        s.flat_duration_s = (now - self.flat_since).total_seconds() if self.flat_since else None

        tnow = now.timestamp()
        win = [x for x in self.buf if tnow - x.ts.timestamp() <= self.rate_window_s]
        ts = [x.ts.timestamp() for x in win]
        ws = [x.weight_g for x in win]
        s.window_n = len(win)
        if len(win) >= 2:
            s.window_s = ts[-1] - ts[0]
            s.rolling_std_g = float(np.std(ws, ddof=1))
            s.rolling_var_g2 = s.rolling_std_g**2
        fit = linear_fit(ts, ws)
        if fit is not None:
            s.slope_g_s = fit.slope_g_s
            s.rate_g_min = rate_from_slope(fit.slope_g_s)
            s.rate_se_g_min = 60.0 * fit.slope_se
            s.noise_g = fit.noise_g
            s.r2 = fit.r2
        s.window_ok = (
            fit is not None
            and len(win) >= self.min_window_samples
            and fit.span_s >= 0.5 * self.rate_window_s
        )

        # Baseline deviation: compare the last few seconds with what the
        # preceding window's trend predicts for now.
        prev = [x for x in self.buf if BASELINE_RECENT_S < tnow - x.ts.timestamp() <= BASELINE_RECENT_S + self.rate_window_s]
        rec = [x.weight_g for x in self.buf if tnow - x.ts.timestamp() <= BASELINE_RECENT_S]
        pfit = linear_fit([x.ts.timestamp() for x in prev], [x.weight_g for x in prev])
        if pfit is not None and rec and len(prev) >= self.min_window_samples:
            s.baseline_dev_g = float(np.mean(rec)) - pfit.predict(tnow)
        elif s.window_ok:
            s.baseline_dev_g = 0.0

        # Step detection for bag replacement / handling.
        step_recent = [x.weight_g for x in self.buf if tnow - x.ts.timestamp() <= STEP_RECENT_S]
        step_past = [
            x.weight_g
            for x in self.buf
            if self.step_lookback_s - STEP_RECENT_S <= tnow - x.ts.timestamp() <= self.step_lookback_s + STEP_RECENT_S
        ]
        if step_recent and step_past:
            s.step_g = float(np.mean(step_recent) - np.mean(step_past))
        return s

    def stable_mean(self, now: datetime, seconds: float, max_std: float) -> float | None:
        """Mean weight if the last `seconds` are stable (std <= max_std), else None."""
        tnow = now.timestamp()
        win = [x for x in self.buf if tnow - x.ts.timestamp() <= seconds]
        if len(win) < 3 or (win[-1].ts - win[0].ts).total_seconds() < 0.9 * seconds:
            return None
        ws = [x.weight_g for x in win]
        if float(np.std(ws, ddof=1)) > max_std:
            return None
        return float(np.mean(ws))

    def first_ts(self) -> datetime | None:
        return self.buf[0].ts if self.buf else None
