"""Rule-based infusion event detection (requirements §6).

One PRIMARY flow state is reported at a time, chosen by fixed precedence so
two contradictory states can never both be confirmed:

  1. SENSOR_UNCERTAIN (data quality)  latest data is missing, stale, offline,
                                       invalid, or there is not yet enough data
  2. BAG_REPLACEMENT                   positive step > BAG_JUMP_G, then stable
                                       for BAG_STABLE_S; confirms a new bag
  3. SENSOR_UNCERTAIN (signal)         sudden drop (bag handling/removal?) or
                                       weight rising without a bag change
  4. HIGH_MASS_LOSS_RATE               rate above the high threshold for
                                       HIGH_RATE_PERSIST_S ("possible free flow,
                                       unvalidated": weight alone cannot prove it)
  5. FLOW_INTERRUPTION                 rate <= INTERRUPTION_RATE_G_MIN for
                                       INTERRUPTION_PERSIST_S while fluid remains
  6. NORMAL_FLOW                       rate between the two thresholds

LOW_VOLUME is a separate flag (remaining % below LOW_VOLUME_PCT for
LOW_VOLUME_PERSIST_S). It describes the bag, not the flow, so it can coexist
with the flow states 4-6 without contradiction. It is not evaluated while the
state is SENSOR_UNCERTAIN or BAG_REPLACEMENT, because the weight is then not
trustworthy as a measure of what is left in the bag.

Each state is a CANDIDATE while its condition holds but the persistence time
has not elapsed, and CONFIRMED afterwards. Missing data is never treated as
normal flow: with no valid window the state is SENSOR_UNCERTAIN.

Low-false-alarm guards (added 2026-10-11 after a live static-load run showed
whole-gram rounding makes the 60 s rate jitter by about +/-0.45 g/min):
  * a weight gain counts only if it is faster than the largest of
    WEIGHT_GAIN_G_MIN, WEIGHT_RESOLUTION_G / RATE_WINDOW_S, and RATE_SIGMA_K
    standard errors of the rate (see gain_threshold);
  * a high mass-loss rate counts only if rate - RATE_SIGMA_K x SE is above the
    high threshold, so noise alone cannot push it over;
  * hysteresis: once FLOW_INTERRUPTION has started it continues until the
    rate reaches INTERRUPTION_CLEAR_G_MIN (default 1.0, entry is <= 0.5), and
    HIGH_MASS_LOSS_RATE continues until the rate falls to
    HIGH_RATE_CLEAR_FRACTION x its threshold;
  * the persistence timers of HIGH_MASS_LOSS_RATE, FLOW_INTERRUPTION and the
    weight-gain check survive lapses of up to RULE_GRACE_S, so a brief jitter
    across a threshold neither restarts the timer nor ends a confirmed state.
    While a lapse is being bridged the reason says so;
  * a weight gain must hold for GAIN_ENTRY_S before it replaces the flow
    state, and a shorter gain blip does not restart the interruption or
    high-rate timers.

Persistence is measured from the first evaluation at which the windowed rate
met the condition. Because the rate is a fit over RATE_WINDOW_S, the weight
has typically been flat for somewhat longer than the persistence time when a
FLOW_INTERRUPTION confirms; detection latency must be measured experimentally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import Settings
from .processing import SignalProcessor, Stats

NORMAL_PERSIST_S = 30.0
BAG_PENDING_TIMEOUT_S = 600.0
GRACE_KEYS = ("high", "interrupt", "gain")
GAIN_ENTRY_S = 20.0  # a weight gain must hold this long before it overrides the flow state

FLOW_STATES = [
    "SENSOR_UNCERTAIN",
    "BAG_REPLACEMENT",
    "HIGH_MASS_LOSS_RATE",
    "FLOW_INTERRUPTION",
    "NORMAL_FLOW",
]

LABELS = {
    "NORMAL_FLOW": "Normal flow",
    "FLOW_INTERRUPTION": "Flow interruption",
    "HIGH_MASS_LOSS_RATE": "High mass-loss rate (possible free flow, unvalidated)",
    "BAG_REPLACEMENT": "Bag replacement",
    "LOW_VOLUME": "Low volume",
    "SENSOR_UNCERTAIN": "Sensor data uncertain",
}


def gain_threshold(cfg: Settings, stats: Stats) -> float:
    """Smallest weight-gain rate (g/min) treated as real rather than noise or rounding."""
    floor = 60.0 * cfg.weight_resolution_g / cfg.rate_window_s if cfg.rate_window_s > 0 else 0.0
    return max(cfg.weight_gain_g_min, floor, cfg.rate_sigma_k * (stats.rate_se_g_min or 0.0))


@dataclass
class RuleResult:
    flow_state: str
    status: str  # candidate | confirmed
    reason: str
    since: datetime
    required_s: float  # persistence needed to confirm (0 = immediate)
    low_volume: bool = False
    low_volume_status: str | None = None
    low_volume_since: datetime | None = None
    new_bag_initial_g: float | None = None  # set once BAG_REPLACEMENT confirms
    alertable: bool = True
    high_threshold_g_min: float | None = None


@dataclass
class RuleEngine:
    cfg: Settings
    since: dict = field(default_factory=dict)  # condition key -> first time it held
    last_true: dict = field(default_factory=dict)  # condition key -> last time it held
    bag_pending_since: datetime | None = None
    bag_pre_jump_g: float | None = None
    handling_since: datetime | None = None

    def _held(self, key: str, active: bool, now: datetime) -> datetime | None:
        last = self.last_true.get(key)
        within_grace = key in GRACE_KEYS and last is not None and (now - last).total_seconds() <= self.cfg.rule_grace_s
        if active:
            if key in GRACE_KEYS and key in self.since and not within_grace:
                self.since.pop(key)  # not evaluated for longer than the grace: start again
            self.last_true[key] = now
            return self.since.setdefault(key, now)
        if within_grace and key in self.since:
            return self.since[key]  # brief lapse: keep the timer running
        self.since.pop(key, None)
        self.last_true.pop(key, None)
        return None

    def _bridged(self, key: str, now: datetime) -> str:
        last = self.last_true.get(key)
        if last is None or last == now:
            return ""
        return f" (condition lapsed {(now - last).total_seconds():.0f} s ago; within {self.cfg.rule_grace_s:g} s grace)"

    def high_threshold(self, expected_rate_g_min: float | None) -> float:
        if expected_rate_g_min and expected_rate_g_min > 0:
            return expected_rate_g_min * self.cfg.high_rate_factor
        return self.cfg.high_rate_abs_g_min

    def evaluate(
        self,
        now: datetime,
        stats: Stats,
        proc: SignalProcessor,
        quality_problem: str | None,
        remaining: float | None,
        expected_rate_g_min: float | None,
    ) -> RuleResult:
        c = self.cfg
        hi = self.high_threshold(expected_rate_g_min)

        def result(state, status, reason, since, required, alertable=True, **kw):
            r = RuleResult(state, status, reason, since, required, alertable=alertable, high_threshold_g_min=hi, **kw)
            self._low_volume(r, now, remaining, quality_problem)
            return r

        # 1. data quality first
        if quality_problem:
            since = self._held("quality", True, now)
            self._clear_flow_conditions(keep="quality")
            return result("SENSOR_UNCERTAIN", "confirmed", quality_problem, since, 0)
        self._held("quality", False, now)

        # 2./3. weight steps (bag replacement or handling)
        step = stats.step_g
        if step is not None and step > c.bag_jump_g and self.bag_pending_since is None:
            self.bag_pending_since = now
            self.bag_pre_jump_g = (stats.weight_filtered_g or 0) - step
            self.handling_since = None
            proc.reset(keep_after=now)  # rate window must not span the step
        elif step is not None and step < -c.bag_jump_g and self.handling_since is None and self.bag_pending_since is None:
            self.handling_since = now
            proc.reset(keep_after=now)

        if self.bag_pending_since is not None:
            elapsed = (now - self.bag_pending_since).total_seconds()
            stable = proc.stable_mean(now, c.bag_stable_s, c.stable_std_g)
            if stable is not None and stable > (self.bag_pre_jump_g or 0) + c.bag_jump_g:
                since = self.bag_pending_since
                self.bag_pending_since = None
                self.bag_pre_jump_g = None
                self._clear_flow_conditions()
                return result(
                    "BAG_REPLACEMENT", "confirmed",
                    f"weight rose by more than {c.bag_jump_g:g} g and was stable for {c.bag_stable_s:g} s; new initial weight {stable:.1f} g",
                    since, c.bag_stable_s, new_bag_initial_g=stable,
                )
            if elapsed > BAG_PENDING_TIMEOUT_S:
                self.bag_pending_since = None  # never stabilised: give up
            else:
                return result(
                    "BAG_REPLACEMENT", "candidate",
                    f"weight jumped up; waiting for {c.bag_stable_s:g} s of stable readings",
                    self.bag_pending_since, c.bag_stable_s,
                )

        if self.handling_since is not None:
            if proc.stable_mean(now, c.bag_stable_s, c.stable_std_g) is not None and stats.window_ok:
                self.handling_since = None
            else:
                return result(
                    "SENSOR_UNCERTAIN", "confirmed",
                    "sudden weight drop (bag handled or removed?); waiting for stable readings",
                    self.handling_since, 0,
                )

        if not stats.window_ok or stats.rate_g_min is None:
            self._clear_flow_conditions()
            return result(
                "SENSOR_UNCERTAIN", "confirmed",
                "collecting data: not enough readings in the rate window yet",
                now, 0, alertable=False,
            )

        rate = stats.rate_g_min
        se = stats.rate_se_g_min or 0.0
        gaining = rate < -gain_threshold(c, stats)
        since = self._held("gain", gaining, now)
        if gaining and (now - since).total_seconds() >= GAIN_ENTRY_S:
            self._clear_flow_conditions(keep=("gain", "gain_state", "high", "interrupt"))
            shown = self.since.setdefault("gain_state", now)  # when it became the flow state
            return result(
                "SENSOR_UNCERTAIN", "confirmed" if (now - since).total_seconds() >= NORMAL_PERSIST_S else "candidate",
                f"weight increasing at {-rate:.2f} g/min without a bag change (possible drift or handling)"
                + self._bridged("gain", now),
                shown, NORMAL_PERSIST_S - GAIN_ENTRY_S,
            )

        # 4. high mass-loss rate
        if "high" in self.since:  # hysteresis: stay until clearly below
            high_cond = rate > hi * c.high_rate_clear_fraction
        else:
            high_cond = rate - c.rate_sigma_k * se > hi
        since = self._held("high", high_cond, now)
        if since:
            self._clear_flow_conditions(keep=("high", "gain"))
            dur = (now - since).total_seconds()
            return result(
                "HIGH_MASS_LOSS_RATE", "confirmed" if dur >= c.high_rate_persist_s else "candidate",
                f"mass-loss rate {rate:.2f} ± {se:.2f} g/min above {hi:.2f} g/min for {dur:.0f} s"
                + self._bridged("high", now),
                since, c.high_rate_persist_s,
            )

        # 5. flow interruption (only while fluid remains)
        fluid_left = remaining is None or remaining > 0.5
        limit = c.interruption_clear_g_min if "interrupt" in self.since else c.interruption_rate_g_min
        since = self._held("interrupt", rate <= limit and fluid_left, now)
        if since:
            self._clear_flow_conditions(keep=("interrupt", "gain"))
            dur = (now - since).total_seconds()
            return result(
                "FLOW_INTERRUPTION", "confirmed" if dur >= c.interruption_persist_s else "candidate",
                f"mass-loss rate {rate:.2f} g/min at or below {limit:g} g/min for {dur:.0f} s"
                + self._bridged("interrupt", now),
                since, c.interruption_persist_s,
            )

        # 6. normal flow
        since = self._held("normal", True, now)
        self._clear_flow_conditions(keep=("normal", "gain"))
        dur = (now - since).total_seconds()
        return result(
            "NORMAL_FLOW", "confirmed" if dur >= NORMAL_PERSIST_S else "candidate",
            f"steady weight decrease at {rate:.2f} g/min",
            since, NORMAL_PERSIST_S, alertable=False,
        )

    def _clear_flow_conditions(self, keep: str | tuple = ()) -> None:
        keep = (keep,) if isinstance(keep, str) else keep
        for k in ("high", "interrupt", "normal", "gain", "gain_state", "quality"):
            if k not in keep:
                self.since.pop(k, None)
                self.last_true.pop(k, None)

    def _low_volume(self, r: RuleResult, now: datetime, remaining: float | None, quality_problem: str | None) -> None:
        active = (
            remaining is not None
            and quality_problem is None
            and remaining < self.cfg.low_volume_pct
            and r.flow_state not in ("BAG_REPLACEMENT", "SENSOR_UNCERTAIN")
        )
        since = self._held("low_volume", active, now)
        if since:
            r.low_volume = True
            r.low_volume_since = since
            r.low_volume_status = (
                "confirmed" if (now - since).total_seconds() >= self.cfg.low_volume_persist_s else "candidate"
            )
