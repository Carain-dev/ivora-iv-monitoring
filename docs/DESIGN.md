# Design notes

Status on 2026-10-10: the code is complete and tested on **simulated data and a fake Blynk server only**. Nothing has been checked against the real device yet. The device was offline and this build environment couldn't reach blynk.cloud. The items in the "Unverified" section below must be checked first.

Update 2026-10-11: a live read of Blynk V1 works (whole grams, about 4 s between device updates), but the load cell is not calibrated yet. The alarm manager (section 8) and the rate-noise guards (section 5) were added the same day and are tested only on simulated input and one digitised live static-load series.

## 1. Data flow

```
Load cell → HX711 → ESP32 → Blynk Cloud ──(read-only HTTPS poll)──► sources.py
                                                                       │
            raw_reading (stored exactly as received, with quality) ◄───┤
                                                                       ▼
   processing.py → rules.py + sensor_health.py → evidence.py → events + alerts → PostgreSQL
                                                                       │
                                               FastAPI /api/* ◄────────┘ ──► React dashboard
```

The backend is authoritative: the dashboard only displays stored values. The firmware, LCD and buzzer are untouched, and the Blynk client can't write to the device.

## 2. Unverified, to check as soon as the device is online

| Item | Current default | How to verify |
|---|---|---|
| Weight pin | `V1` (from the supplied link) | Dashboard > Bag & device setup > Check Blynk connection |
| Unit and scale | grams, scale 1.0, shown as "unverified" | Known-mass calibration (Research tab) |
| Response format of `get` | accepts `81`, `"81"`, `["81"]` or `{"v1": 81}` | The connection check shows the raw body |
| Device update interval | unknown | "Observed update interval" in the setup tab |
| Regional server | `https://blynk.cloud` (redirects are followed only to `*.blynk.cloud`) | Connection check `host` |
| History export | not used by the poller | `blynk_ingest/probe.py` |

Limitation of Blynk polling: `get` returns the last stored value with no device timestamp. A repeated value can mean "stable" or "nothing new". The system stores our receipt time (`received_at`), checks `isHardwareConnected` every 10 s, and treats readings taken while the device is offline as stale. For better timestamps, the firmware could send a sample counter on a second pin (Option B in `blynk_ingest/INGESTION_DESIGN.md`).

## 3. Formulas

- Weight in grams = raw value × `WEIGHT_SCALE` + `WEIGHT_OFFSET_G`.
- Filtered weight = moving average of the last `MOVING_AVG_SAMPLES` (5) accepted readings.
- Mass-loss rate (g/min) = −60 × slope. The slope (g/s) is the least-squares fit of raw weight against **actual timestamps** over the last `RATE_WINDOW_S` (60 s). For two points this equals −60 × Δweight / Δt exactly. The rate is reported only when the window holds at least 5 samples spanning at least 30 s.
- Remaining (%) = 100 × (filtered weight − tare) / (initial weight − tare).
- Mass-loss rate is not volumetric flow. mL/h ≈ g/min × 60 / density. The dashboard shows mL/h only as an estimate labelled "if density is 1.00 g/mL". 0.9 % saline is about 1.005 g/mL.
- Also computed over the window: rolling SD and variance, residual noise around the fit, R², baseline deviation (the mean of the last 10 s minus what the previous window's trend predicts), and flat duration (time the reading has stayed unchanged).

## 4. Data quality (checked first, first match wins)

`api_error` → `device_offline` → `invalid` (non-numeric, or outside −50 g to `LOAD_CELL_CAPACITY_G`) → `repeated` → `ok`. Only `ok` and `repeated` readings enter the processing window. If there has been no good reading for `STALE_S` (30 s), the state is SENSOR_UNCERTAIN. Missing data is never treated as normal flow.

## 5. Rules and precedence (`rules.py`)

| Precedence | State | Condition (prototype values) | Confirms after |
|---|---|---|---|
| 1 | SENSOR_UNCERTAIN | bad or stale data, or not enough data yet | immediately |
| 2 | BAG_REPLACEMENT | step up > 50 g, then SD ≤ 2 g for 30 s | 30 s stable; resets the bag baseline, keeps tare |
| 3 | SENSOR_UNCERTAIN | step down > 50 g (handling or removal), or weight rising > 0.5 g/min | immediately or after 30 s |
| 4 | HIGH_MASS_LOSS_RATE | rate > 2 × expected rate, or > 8 g/min if none is set | 60 s |
| 5 | FLOW_INTERRUPTION | rate ≤ 0.5 g/min while fluid remains | 120 s |
| 6 | NORMAL_FLOW | otherwise | 30 s |
| flag | LOW_VOLUME | remaining < 15 % (not while uncertain or replacing) | 10 s |

Only one primary state exists at a time, so contradictory confirmed states can't happen; a test checks this.

Low-false-alarm guards (added 2026-10-11). On the live static-load run of 2026-10-11, Blynk V1 arrived in whole grams and the 60 s rate jittered by about ±0.45 g/min (SD). That jitter flipped the state between FLOW_INTERRUPTION and NORMAL_FLOW and raised "weight rising" drift flags. Replaying an hour of simulated whole-gram static load through the old code gave 88 interruption alerts, 47 drift alerts and 47 sensor-uncertain alerts. The rules now use:
- **Rate standard error.** `processing.py` reports `rate_se_g_min` (60 × standard error of the fitted slope).
- **Significant weight gain only.** A gain counts if it is faster than the largest of `WEIGHT_GAIN_G_MIN` (0.5), `60 × WEIGHT_RESOLUTION_G / RATE_WINDOW_S` (1.0 g/min for 1 g resolution and a 60 s window) and `RATE_SIGMA_K` (3) × SE. It must also hold for 20 s before it replaces the flow state. Polling every 2 s while the device updates every ~4 s repeats samples, which makes the SE look smaller than it is; k = 3 allows for that.
- **Significant high rate only.** HIGH_MASS_LOSS_RATE starts when rate − 3 × SE is above the threshold.
- **Hysteresis.** FLOW_INTERRUPTION starts at ≤ 0.5 g/min and continues until the rate reaches `INTERRUPTION_CLEAR_G_MIN` (1.0). HIGH_MASS_LOSS_RATE continues until the rate is ≤ `HIGH_RATE_CLEAR_FRACTION` (0.8) × threshold.
- **Grace.** The persistence timers for high rate, interruption and weight gain survive lapses of up to `RULE_GRACE_S` (20 s). A short gain blip does not restart the interruption timer.

The same hour of input now gives one interruption alarm and no drift or sensor alarms (`tests/test_alarms.py`). This shows the logic works on simulated input. It is not a measured false-alarm rate.

Limits that follow from whole-gram data: a flow slower than about 1 g/min can't be told apart from a stopped flow within one 60 s window, and a weight gain slower than 1 g/min isn't flagged by the gain check (the baseline-shift check still runs). "Possible free flow" is never asserted: the label is "High mass-loss rate (possible free flow, unvalidated)". All thresholds are environment variables.

## 6. Sensor-health layer (`sensor_health.py`)

- Heuristics, always on:
  - POSSIBLE_STUCK_SIGNAL: unchanged for ≥ 300 s.
  - ABNORMAL_NOISE: residual noise > 3 g.
  - POSSIBLE_DRIFT: baseline deviation > 5 g without a step, or weight rising.
- Isolation Forest (scikit-learn, 200 trees, contamination 0.02):
  - Features: slope, rolling SD, variance, residual noise, baseline deviation, flat duration.
  - It is trained only on a period the experimenter marks as normal.
  - It outputs an anomaly index = −decision_function. Above 0 means flagged. This is **not a probability**.
  - When it flags a sample the heuristics didn't, the state comes from the feature with the largest deviation from the training mean.
  - The model records whether it was trained on simulated or physical data, and the dashboard shows this.
- If FLOW_INTERRUPTION and POSSIBLE_STUCK_SIGNAL are both present, the alert and the dashboard say the data can't tell them apart.

Known limits:
- If the firmware rounds weights to whole grams, slow flow produces long runs of identical values, and the stuck-signal heuristic may fire. Tune `STUCK_S` or `FLAT_TOLERANCE_G` from real data.
- Slow drift during normal flow looks like a change in rate. Only drift that makes the weight rise, or a sudden offset, is detectable from weight alone.

## 7. Evidence score (`evidence.py`)

score = 100 × (0.30 persistence + 0.20 trend consistency + 0.15 signal quality + 0.20 freshness + 0.15 sensor health). Each factor is 0 to 1 and is defined in the module docstring. The weights are design choices. The score ranks support for a state; it is not a calibrated probability. The dashboard shows every factor.

## 8. Alarms (`alarms.py`)

Events record every detected condition, including candidates that never confirm. Alarms are the subset that needs a person, and `alarms.py` decides which events become alarms. Before 2026-10-11 every event created an alert immediately. Now an alarm row is created only when the trigger below is met. Every number is a configurable prototype default. None is a clinical threshold, and none has been validated.

| Event | Priority | Trigger | Persistence | Recovery (must hold `ALARM_CLEAR_S` = 30 s) | Buzzer |
|---|---|---|---|---|---|
| FLOW_INTERRUPTION | HIGH | rate ≤ 0.5 g/min while fluid remains; bag session active | 120 s, lapses ≤ 20 s bridged | rate ≥ 1.0 g/min with good data, or a new bag | 10-pulse burst every 5 s |
| HIGH_MASS_LOSS_RATE ("possible free flow, unvalidated") | HIGH | rate − 3 SE > 2 × expected rate (8 g/min if none set); bag session active | 60 s, lapses ≤ 20 s bridged | rate ≤ 0.8 × threshold, or a new bag | 10-pulse burst every 5 s |
| LOW_VOLUME | MEDIUM | remaining < 15 %; bag session active | 10 s | remaining ≥ 20 %, or a new bag | 3-pulse burst every 15 s |
| SENSOR_UNCERTAIN (data loss, sudden drop, significant gain) | MEDIUM | condition present | 60 s (`ALARM_SENSOR_PERSIST_S`) | good data and a non-uncertain flow state | 3-pulse burst every 15 s |
| POSSIBLE_STUCK_SIGNAL | MEDIUM | unchanged for 300 s | 60 s more | sensor normal | 3-pulse burst every 15 s |
| ABNORMAL_NOISE, POSSIBLE_DRIFT | LOW, then MEDIUM after 600 s | heuristic or Isolation Forest flag | 60 s | sensor normal | none while LOW |
| BAG_REPLACEMENT | INFO | confirmed replacement | – | stored already resolved | none |

Further rules:
- **One alarm per type at a time.** While an alarm is active, the same detection only updates it.
- **Missing data never clears an alarm.** If the device goes offline during an interruption, the interruption alarm stays latched and a data-loss alarm joins it after 60 s.
- **Recurrence reopens.** A same-type alarm that comes back within `ALARM_REARM_S` (300 s) of clearing reopens the old row, and `alert.recurrences` goes up by one.
- **Acknowledge silences.** The buzzer stops but the alarm stays until recovery. A HIGH alarm still active `ALARM_RESOUND_S` (300 s) after acknowledgement sounds again. A manual Resolve while the condition persists isn't re-raised until the condition has cleared once.
- **Infusion alarms need a bag.** They are armed only while a bag session exists (`ALARMS_REQUIRE_BAG`), so a weight placed during setup or calibration is not alarmed. Starting a new bag resolves the old bag's infusion alarms.
- **Each alarm stores:** event type and label, time, weight, rate, remaining %, evidence score, priority (`severity`), sensor status, state (`confirmed`, `acknowledged`, `resolved`) and recurrences.

Buzzer:
- The backend picks the pattern for the highest unacknowledged HIGH or MEDIUM alarm and returns it as `buzzer` in `/api/status`.
- The dashboard plays it with the Web Audio API once the user presses "Enable alarm sound" (browsers block sound until a click).
- The pulse counts follow the convention of IEC 60601-1-8 (high = 10 pulses, medium = 3). The timings are prototype choices, and the system has not been tested against that standard.
- `/api/alarms/policy` returns the table above, built from the current settings.

The device's own buzzer is not changed. It is driven by the supplied ESP32 firmware, which hasn't been inspected; the project-centre slides describe only a low-level alarm. So the device buzzer and the dashboard buzzer can disagree.

**Optional firmware step (not done, not tested).** To let the physical buzzer play the backend's pattern:
1. Add a virtual pin to the Blynk template, for example V10, an integer from 0 to 2.
2. In the firmware, add `BLYNK_WRITE(V10) { buzzerCode = param.asInt(); }`. Then play the pattern with non-blocking `millis()` timing: 0 is silent, 1 is the medium pattern, 2 is the high pattern.
3. Give the backend a writer that calls Blynk's documented `GET /external/api/update?token=…&V10=<code>` whenever the code changes. That writer doesn't exist yet. The current Blynk client is read-only by design, and nothing in this code writes to the device.
4. Decide whether the firmware's own low-weight alarm should stay. Then test both buzzers on the bench before relying on either.

## 9. Database tables (`db.py`)

- `raw_reading`: as received, with `received_at`, `source_ts`, `quality`, `device_online` and `source`.
- `measurement`: all computed values and states per reading.
- `event`
- `alert` (alarms; `severity` holds the priority; `recurrences` was added 2026-10-11 and is added automatically to an existing database at startup)
- `bag_session`: tare, initial weight, expected rate.
- `ground_truth`: experimenter labels, physical or simulated.
- `calibration_check`

Every table has `source` (`blynk_poll` / `simulated`), so simulated rows can't mix with physical ones. Multi-bed later means adding a pipeline per `DEVICE_ID`; every table is already keyed by `device_id`.

## 10. Validation plan (phases, outputs, acceptance)

| Phase | Test | Output / metric | Accept when |
|---|---|---|---|
| 1 API | Connection check with the device online | pins, values, interval | the weight pin is identified and LIVE is shown |
| 3 Calibration | 3 to 5 known masses, 3 repeats each | MAE, RMSE, bias, correction | MAE within your target (set it before testing) |
| 3 Stability | fixed load for 30 min | SD, drift, false alerts | no confirmed false events |
| 4 Rate | normal drip; weigh the collected fluid | rate error vs collected mass / time | agreed tolerance |
| 5 Rules | clamp closed; open roller clamp; swap bag; run to low | event-level TP/FP/FN, latency | per-rule targets set in advance |
| 6 Sensor | unplug HX711 wire (stuck); tap or vibrate (noise); slow loading (drift) | detections, false positives | report what is observed |
| 6 API failure | turn off Wi-Fi | SENSOR_UNCERTAIN within STALE_S | never NORMAL_FLOW while offline |

Report only metrics computed from labelled physical runs. Keep simulated results in a separate, clearly marked section. The simulated metrics in the Research tab only show that the code works.

## 11. Paper alignment

The paper describes two beds and an ESP32 posting directly to Spring Boot at 1 Hz, and its Section V/VI results come from a simulated dataset. The built system uses one bed via Blynk and FastAPI, with no Spring Boot. Update the paper's architecture and results sections to match what is built and measured.
