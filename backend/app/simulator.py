"""Simulated load-cell data for offline development and testing.

EVERYTHING produced here is labelled source='simulated'. It exists so the
pipeline, dashboard and tests can run without the device. It must never be
reported as a physical measurement or used for accuracy claims.

The scenario is a fixed script of phases. Each phase has a ground-truth label
that is written to the ground_truth table (source='simulated') so the
evaluation code can be exercised end to end.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterator

TARE_G = 40.0
INITIAL_G = 560.0  # 500 mL bag + tare, full
START_G = 260.0  # the demo bag is already partly used so LOW_VOLUME appears
NORMAL_RATE = 4.0  # g/min (about 240 mL/h at 1 g/mL)
NOISE_G = 0.3


@dataclass
class Phase:
    name: str  # ground-truth label
    seconds: float
    rate_g_min: float = NORMAL_RATE
    noise_g: float = NOISE_G
    stuck: bool = False
    drift_g_s: float = 0.0
    api_failure: bool = False
    set_weight_g: float | None = None  # weight jumps to this at phase start


SCENARIO = [
    Phase("NORMAL_FLOW", 240),
    Phase("FLOW_INTERRUPTION", 240, rate_g_min=0.0),
    Phase("NORMAL_FLOW", 150),
    Phase("HIGH_MASS_LOSS_RATE", 150, rate_g_min=16.0),
    Phase("NORMAL_FLOW", 150),
    Phase("POSSIBLE_STUCK_SIGNAL", 400, stuck=True),
    Phase("NORMAL_FLOW", 150),
    Phase("ABNORMAL_NOISE", 120, noise_g=6.0),
    Phase("NORMAL_FLOW", 150),
    Phase("POSSIBLE_DRIFT", 180, drift_g_s=0.12),
    Phase("NORMAL_FLOW", 120),
    Phase("SENSOR_UNCERTAIN", 60, api_failure=True),
    Phase("NORMAL_FLOW", 240),
    Phase("BAG_REMOVED", 25, rate_g_min=0.0, set_weight_g=0.0),
    Phase("BAG_REPLACEMENT", 60, rate_g_min=0.0, set_weight_g=INITIAL_G),
]


@dataclass
class SimSample:
    t: float  # seconds since simulation start
    true_g: float
    value: float | None  # what the "device" reports; None = API failure
    label: str
    phase_index: int
    phase_start: bool


def generate(interval_s: float = 1.0, seed: int = 7, scenario: list[Phase] | None = None,
             start_g: float = START_G, loop: bool = True) -> Iterator[SimSample]:
    rng = random.Random(seed)
    scenario = scenario or SCENARIO
    t = 0.0
    weight = start_g
    drift = 0.0
    while True:
        for i, ph in enumerate(scenario):
            if ph.set_weight_g is not None:
                weight = ph.set_weight_g
                drift = 0.0
            stuck_value = None
            elapsed = 0.0
            first = True
            while elapsed < ph.seconds:
                if weight > TARE_G:  # an empty bag stops draining; a removed bag reads ~0
                    weight = max(TARE_G, weight - ph.rate_g_min / 60.0 * interval_s)
                drift += ph.drift_g_s * interval_s
                reading = weight + drift + rng.gauss(0, ph.noise_g)
                reading = round(reading, 1)  # 0.1 g resolution, as recommended for firmware
                if ph.stuck:
                    stuck_value = reading if stuck_value is None else stuck_value
                    reading = stuck_value
                yield SimSample(t, weight, None if ph.api_failure else reading, ph.name, i, first)
                first = False
                t += interval_s
                elapsed += interval_s
        if not loop:
            return


def normal_only(seconds: float, interval_s: float = 1.0, seed: int = 11, start_g: float = 500.0) -> Iterator[SimSample]:
    """Clean simulated normal flow, for training a simulated-only sensor-health model."""
    phases = [
        Phase("NORMAL_FLOW", seconds / 3, rate_g_min=3.0),
        Phase("FLOW_INTERRUPTION", seconds / 3, rate_g_min=0.0),
        Phase("NORMAL_FLOW", seconds / 3, rate_g_min=6.0),
    ]
    return generate(interval_s, seed, phases, start_g=start_g, loop=False)
