# Alarm system: testing guide

For the IV bag weight monitor in `code/iv_monitor` (updated 2026-10-11). Benchtop research prototype only. Use water in the bag, never a patient.

Every alarm threshold and timing in this guide is a configurable **prototype default**. None is a clinical value, and none has been validated. The automated tests show that the logic does what it is designed to do on simulated input. Only the physical tests in Part C can tell you how it behaves on your rig, so report only what you measure there.

---

## Part A: Install the update

1. Replace your `iv_monitor` folder with the new one from the project files, or unzip the new `code/iv_monitor.zip`. **Keep your own `.env` file**: copy it into the new folder before you start. The new settings have defaults, so your old `.env` still works. `.env.example` lists them if you want to change any.
2. In the `iv_monitor` folder run:
   ```
   docker compose up -d --build
   ```
3. Your stored data is kept. At startup the app adds the one new database column (`alert.recurrences`) by itself.
4. Optional, to run the automated tests without Docker:
   ```
   cd backend
   pip install -r requirements-dev.txt
   python -m pytest -q
   ```
   You should see `28 passed`. The 11 alarm tests are in `backend/tests/test_alarms.py`.

## Part B: Simulator mode

### B1. Start it

1. In `.env` set `DATA_SOURCE=simulator`, then run `docker compose up -d`.
2. Open http://localhost:8000. Check that the purple **SIMULATED DATA** banner is showing.
3. Under the header there is a **Dashboard buzzer** bar. Press **Enable alarm sound**. Browsers block sound until you click, and you need to do this once per tab.
4. Open **Bag & device setup** and scroll to **Alarm policy**. This table is the live configuration: priority, trigger, persistence, recovery and buzzer for each event.

The simulator starts a bag by itself, so infusion alarms are armed. It plays a fixed script of about 40 minutes and then repeats it.

### B2. What should happen

Times are minutes:seconds after the app started and are approximate, give or take a few seconds. They come from replaying the same script in the automated tests.

| Time | Simulated phase | Expected alarm | Sound |
|---|---|---|---|
| 0:00–4:00 | Normal flow | None | Silent |
| 4:00–8:00 | Flow stopped | **HIGH Flow interruption** at about 6:50 | 10 beeps every 5 s |
| 8:00–10:30 | Flow resumes | Interruption clears at about 9:10, after 30 s of recovered flow | Silent |
| 10:30–13:00 | Fast weight loss | **HIGH High mass-loss rate** at about 12:00 | 10 beeps |
| 13:00–15:30 | Normal | High-rate alarm clears at about 14:30 | Silent |
| 15:30–22:10 | Stuck reading | **HIGH Flow interruption** at about 18:10, with the note "may be a real interruption or a stuck sensor"; then **MEDIUM Possible stuck signal** at about 21:30 | 10 beeps |
| about 22:20–23:30 | Stuck reading releases | A short **HIGH High mass-loss rate**. This is a known artefact: the reading catches up all at once. | 10 beeps |
| 24:40–26:40 | Noisy sensor | **LOW Abnormal noise** at about 26:00 | Silent (LOW) |
| 29:10–32:10 | Drifting sensor | **LOW Possible drift**, then **MEDIUM Sensor data uncertain** ("weight increasing") | 3 beeps every 15 s |
| 34:10–35:10 | API failure for 60 s | None. The badge goes STALE and the event is logged, but the outage is shorter than the alarm's 60 s persistence. | Silent |
| about 37:30 | Bag below 15 % | **MEDIUM Low volume** | 3 beeps |
| 39:10–39:35 | Bag removed | None | Silent |
| about 40:00 | New bag hung | **INFO Bag replacement** notice; Low volume resolves with "new bag started" | Silent |

### B3. Things to try while it runs

1. **Acknowledge.** During the flow-interruption alarm, press **Acknowledge (silence)**. The buzzer bar should switch to silent while the alarm stays listed as `acknowledged`. If the condition lasts another 5 minutes, it sounds again.
2. **One alarm per problem.** Open **Events & alerts**. The event history still logs every detection, including short candidates. The alarm list should show only one alarm per problem.
3. **Manual resolve.** During an alarm, press **Resolve**. It should not come back while the same condition continues.
4. **Priority order.** When a HIGH and a MEDIUM alarm are active together, the bar plays the HIGH pattern. Acknowledge the HIGH one and the MEDIUM pattern takes over.
5. **Export.** In **Research & export**, download `alerts.csv`. Check the `severity` (priority), `state` and `recurrences` columns.
6. **API check.** `http://localhost:8000/api/status` shows a `buzzer` block, and `http://localhost:8000/api/alarms/policy` returns the policy table.

## Part C: Real hardware

### C0. Before you start

1. Set `DATA_SOURCE=blynk` with your device token, run `docker compose up -d`, and wait for the **LIVE** badge.
2. Calibrate with known masses first (README Part 5). Until you have, weights read "g (unverified)", and any rate you report is uncalibrated.
3. `WEIGHT_RESOLUTION_G=1` matches what Blynk V1 sent on 2026-10-11 (whole grams). If the firmware is later changed to send decimals, set it to the new step, for example `0.1`.
4. Use a flow of at least about 2 g/min for the normal-flow tests. With whole-gram data and a 60 s window, a flow under about 1 g/min can't be told apart from a stopped flow.
5. For every test, open **Research & export** > **Ground-truth labels**. Press **Start label now** when you start the condition and **End now** when you stop it. Write down the times in a table like the one in C10.

### C1. Static load, no bag started (false-alarm check)

1. Hang a known mass (for example 200 g). Do **not** start a bag.
2. Label it `STATIONARY_LOAD` and leave it for 30 minutes without touching it.
3. **Expected:** no alarms at all. Infusion alarms are disarmed without a bag, and whole-gram jitter should no longer cause drift or "weight rising" alarms. The event history may still list short candidates; those are detections, not alarms.
4. Record the number of alarms and their types.

### C2. Static load with a bag started (repeat-suppression check)

1. Keep the same mass. Start a bag: tare = hook weight, tick "use current weight as initial".
2. **Expected:** exactly **one HIGH Flow interruption** after about 2 to 3 minutes. A still weight really is "no flow". It should not clear and re-raise during the next 30 minutes, and `recurrences` in `alerts.csv` should stay 0.
3. Acknowledge it and check that the sound stops. If `ALARM_RESOUND_S=300`, it sounds again 5 minutes later.

### C3. Normal flow

1. Hang a water bag with a giving set. Set the roller clamp to a steady drip into a beaker on a separate scale. Start a bag with the correct tare, and enter the expected rate in g/min if you know it.
2. Run for at least 20 minutes. Label it `NORMAL_FLOW`.
3. **Expected:** no alarms. Compare the dashboard rate with the beaker's mass gain over the same time.

### C4. Flow interruption (repeat 5 times)

1. During normal flow, close the roller clamp fully. Label it `FLOW_INTERRUPTION`.
2. **Expected:** **HIGH Flow interruption** about 2 to 3 minutes after closing. That is the 120 s persistence plus the lag of the 60 s rate window. Write down the latency: alarm `confirmed_at` minus label start.
3. Reopen the clamp and end the label. **Expected:** the alarm clears about 1 to 1.5 minutes after flow restarts. The rate has to climb back above 1 g/min and stay there for 30 s.

### C5. Unusually rapid weight loss (repeat 3 times)

1. Make sure an expected rate is entered for the bag. The threshold is 2 × that rate, or 8 g/min if none is entered.
2. Open the roller clamp fully, or draw water out steadily with a syringe, so the weight falls clearly faster than the threshold. Label it `HIGH_MASS_LOSS_RATE`.
3. **Expected:** **HIGH High mass-loss rate (possible free flow, unvalidated)** about 1 to 1.5 minutes after the fast loss starts. It clears when the rate falls to 0.8 × threshold for 30 s. Weight alone can't prove free flow, so keep the "unvalidated" wording in your notes and the paper.

### C6. Low volume

1. Start a bag with the correct tare and initial weight, then let it run down, or start with a nearly empty bag.
2. **Expected:** **MEDIUM Low volume** about 10 s after the remaining % drops below 15. It clears only above 20 %, or when a new bag is hung.

### C7. Bag replacement

1. Swap the bag for one at least 50 g heavier and leave it still for 30 s.
2. **Expected:** an **INFO Bag replacement** notice with no sound. Any interruption, high-rate or low-volume alarm from the old bag resolves with "new bag started".

### C8. Data loss

1. During normal flow, switch off the ESP32 (or its Wi-Fi). Label it `SENSOR_UNCERTAIN`.
2. **Expected:** the badge shows **STALE** once Blynk reports the device offline, or after 30 s without a fresh value. **MEDIUM Sensor data uncertain** follows 60 s after that, so expect it 1 to 2 minutes after switch-off.
3. Repeat during an active flow-interruption alarm. **Expected:** the interruption alarm stays active and is not cleared by the outage.
4. Switch the device back on. **Expected:** the data alarm clears once good data has returned, the rate window has refilled, and 30 s have passed.

### C9. Sensor noise (optional)

1. Tap the stand lightly and repeatedly for about 2 minutes. Label it `ABNORMAL_NOISE`.
2. **Expected:** **LOW Abnormal noise** (silent) about 1 to 1.5 minutes after the noise starts, if the noise exceeds `NOISE_STD_G` (3 g). It becomes MEDIUM if it lasts 10 minutes.
3. Don't pull wires off the HX711 to fake a stuck sensor unless you know it is safe for the board. If you do, record what the firmware sends; it hasn't been inspected.

### C10. Results table

| Test | Repeat | Label start | Alarm raised (`confirmed_at`) | Latency (s) | Cleared | Unexpected alarms | Recurrences |
|---|---|---|---|---|---|---|---|
| C1 static, no bag | 1 | | n/a | n/a | n/a | | |
| C4 interruption | 1–5 | | | | | | |
| ... | | | | | | | |

The Research tab computes detection metrics from your **physical** labels only. Report latency and false alarms per test. Don't report sensitivity or specificity from only a handful of repeats without saying how many there were.

## Part D: If something looks wrong

| What you see | Why, and what to change |
|---|---|
| No sound | Press **Enable alarm sound**. The bar still shows the pattern even when sound is off. |
| An alarm is listed but the bar says silent | It is acknowledged, or it is LOW or INFO priority. Those are silent by design. |
| No interruption or low-volume alarm at all | No bag is started. Start one, or set `ALARMS_REQUIRE_BAG=false`. |
| Interruption alarms during a slow but real flow | The flow is near the whole-gram resolution limit. Use a faster flow, or set `RATE_WINDOW_S=120` (alarms then come later). |
| Alarms clear too slowly | Lower `ALARM_CLEAR_S`. Changes too small to see stay in force. |
| Drift or "weight rising" alarms on a still load | Note the time and export `measurements.csv`. The `details` column holds `rate_se_g_min` and `gain_threshold_g_min`, which show whether the threshold was crossed. Raising `RATE_SIGMA_K` to 4 makes the gain check stricter. |
| The device's own buzzer disagrees with the dashboard | Expected. The ESP32 firmware drives its buzzer by itself. Making it follow the backend needs the optional firmware step in `DESIGN.md` section 8, which hasn't been done. |

After changing `.env`, run `docker compose up -d` for the change to take effect.
