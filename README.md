# IVORA: IV bag weight monitor

Benchtop research prototype. It is not a medical device and must not be used on patients. Test with water bags only.

IVORA reads the IV bag weight from the ESP32 node through Blynk, works out how fast the bag is emptying and how much is left, detects events (flow interruption, high mass-loss rate, bag replacement, low volume, sensor problems), raises alerts, stores everything in PostgreSQL and shows it on a web dashboard.

```
Load cell (40 kg) -> HX711 -> ESP32 (LCD, buzzer) -> Blynk Cloud (V1 = grams)
      -> backend (FastAPI: filtering, rules, sensor-health AI, evidence, alarms, PostgreSQL)
      -> frontend (React dashboard at http://localhost:8000)
```

## What is in this zip

| Folder / file | What it is |
|---|---|
| `backend/` | FastAPI backend. All calculations happen here. `app/main.py` is the API, `requirements.txt` the Python packages, `.env.example` the settings template, `tests/` the automated tests. |
| `frontend/` | React + Vite dashboard (`package.json`, `index.html`, `vite.config.js`, `src/`). |
| `firmware/ivora_esp32.ino` | ESP32 sketch: HX711 + LCD + buzzer + Blynk (weight on V1). |
| `docker-compose.yml`, `Dockerfile` | Start the database, backend and dashboard with one command. |
| `docs/` | `DESIGN.md` (formulas, rules, evidence score, validation plan), bench connection guide, alarm testing guide. |

Backend files: `blynk_client.py` (read-only Blynk client), `sources.py` (Blynk poller and simulator), `processing.py` (filtering, mass-loss rate, remaining %), `rules.py` (event rules), `sensor_health.py` (checks + Isolation Forest), `evidence.py` (evidence score), `alarms.py` (alarm priorities and buzzer patterns), `pipeline.py` (runs each reading through everything), `analytics.py` (calibration error and detection metrics), `db.py`, `config.py`.

Two data modes: **simulator** (made-up data, always labelled SIMULATED) and **blynk** (the real device).

Secrets: the Blynk token and Wi-Fi password go only into `backend/.env` and your local copy of the firmware. Neither file in this zip contains a real token. Don't share or zip your filled-in copies.

---

## Part 1: Install the tools (one time)

1. **Docker Desktop**: https://www.docker.com/products/docker-desktop/ . On Windows accept the WSL 2 option. Start it and wait for "Engine running".
2. **Arduino IDE 2** (only for the firmware): https://www.arduino.cc/en/software
3. Optional, only if you want to run without Docker or edit the dashboard: Python 3.11+ and Node.js 20+.

## Part 2: Unzip

Unzip `IVORA.zip`, for example to `Documents\IVORA`. You should see `backend`, `frontend`, `firmware`, `docs`, `docker-compose.yml`, `Dockerfile` and this README.

## Part 3: Firmware (ESP32 node)

Skip this part if the node already works with its current sketch (LCD shows weight, Blynk V1 shows the same number). The backend only needs grams on V1. **Back up the sketch that is on the board before flashing this one.**

1. Arduino IDE > **Boards Manager**: install "esp32 by Espressif Systems". Select board **ESP32 Dev Module** and the right COM port.
2. **Library Manager**: install `Blynk` (Volodymyr Shymanskyy), `HX711` (Bogdan Necula) and `LiquidCrystal I2C` (Frank de Brabander).
3. Open `firmware/ivora_esp32.ino`. The IDE asks to put it in a folder called `ivora_esp32`: click OK.
4. Section 1: paste `BLYNK_TEMPLATE_ID`, `BLYNK_TEMPLATE_NAME` and the device auth token from Blynk.Console, and your 2.4 GHz Wi-Fi name and password. Change `BLYNK_SERVER` if your account is regional (e.g. `blr1.blynk.cloud`).
5. Section 2: **check the pin numbers against your wiring.** The defaults are HX711 DT = GPIO16, SCK = GPIO17, LCD SDA = GPIO21, SCL = GPIO22 (address 0x27, try 0x3F if blank), buzzer = GPIO25. They are common defaults, not read from your board.
6. **Calibrate** (section 3): set `CALIBRATION_MODE 1`, upload, open Serial Monitor at 115200. Follow the prompts: empty hook, then a known mass (e.g. 500 g). It prints `CALIBRATION_FACTOR = ...`. Paste that number into `CALIBRATION_FACTOR`, set `CALIBRATION_MODE 0`, upload again.
7. In Blynk.Console make sure the template has datastreams **V1** (Double, grams, 0 to 40000, 1 decimal) and **V2** (String, status).
8. Check: the LCD shows `Wt: ... g` and a status (`OK`, `NO BAG`, `LOW LEVEL`, `SENSOR ERROR`) with `ON` when Blynk is connected. The Blynk app V1 equals the LCD.

Node behaviour: weight is averaged over about 1 s and sent to V1 every second. The buzzer gives 3 short beeps every 15 s below `LOW_LEVEL_G` (100 g by default) and a long beep every 10 s if the HX711 stops answering. To re-tare, empty the hook and hold the **BOOT** button for 3 seconds; the tare is saved and survives power cuts. The node keeps showing weight and beeping when Wi-Fi is down.

This sketch has not yet been compiled or tested on your board. Report any compile error and it can be fixed.

## Part 4: Try the dashboard with simulated data first

1. In the `backend` folder copy `.env.example` to a new file named exactly `.env` (on Windows turn on View > Show > File name extensions, so it isn't `.env.txt`).
2. Leave `DATA_SOURCE=simulator`.
3. Open a terminal in the `IVORA` folder (Windows: type `cmd` in the File Explorer address bar; Mac: right-click > New Terminal at Folder) and run:
   ```
   docker compose up --build
   ```
   The first run takes several minutes. It is ready at `Application startup complete`.
4. Open **http://localhost:8000**. A purple **SIMULATED DATA** banner shows. Over about 40 minutes the simulator plays normal flow, an interruption, high rate, a stuck sensor, noise, drift, an API failure, low volume and a bag replacement.
5. Stop with `Ctrl + C`. Next time `docker compose up` (add `-d` to run in the background).

## Part 5: Connect the real device

1. In `backend/.env` set:
   ```
   DATA_SOURCE=blynk
   BLYNK_TOKEN=paste-your-device-auth-token-here
   BLYNK_WEIGHT_PIN=V1
   LOAD_CELL_CAPACITY_G=40000
   ```
   Use the device's Auth Token (Blynk app > device > Device Info). Keep `.env` private.
2. Power the node and wait until Blynk shows the device **Online**.
3. Run `docker compose up -d --build`.
4. Dashboard > **Bag & device setup** > **Check Blynk connection**: `device_online` should be `true` and the V1 value should equal the LCD. If the weight is on another pin, change `BLYNK_WEIGHT_PIN`.
5. The badge at the top right should say **LIVE** (green). **STALE** = device offline; **UNAVAILABLE** = wrong token or server.

## Part 6: Check the units and calibrate in the dashboard

Until you check against known masses the dashboard shows "g (unverified)".

1. Empty hook: the reading should be about 0 (otherwise re-tare the node, Part 3).
2. **Research & export** > **Calibration with known masses**: hang known masses (e.g. 100, 200, 500, 1000 g), wait until the value settles, type the mass, press **Record**.
3. The panel shows MAE, RMSE and bias, and with two or more masses suggests a correction. If needed put it in `backend/.env` as `WEIGHT_SCALE` / `WEIGHT_OFFSET_G`.
4. When readings match, set `UNITS_VERIFIED=true` and run `docker compose up -d`.

## Part 7: Start a bag

1. Weigh the empty bag plus whatever hangs on the load cell (hook, giving set). This is the **tare**.
2. Hang the full bag. Go to **Bag & device setup** > **Start a new bag**. Enter the tare and either tick "Use the current weight as the initial weight" or leave it empty, in which case the system captures it automatically after 30 s of stable readings.
3. Optional: enter the expected mass-loss rate in g/min. The high-rate warning then triggers at twice that rate. Without it, the default threshold is 8 g/min.
4. When you hang a new bag later, the system detects it: a jump of more than 50 g followed by 30 s of stable readings. It then starts a new bag with the same tare automatically.

## Part 8: Train the sensor-health AI (optional, after you have normal data)

The Isolation Forest only learns what "normal" looks like from data you know was normal.

1. Run a normal test for at least 10 to 15 minutes, with nothing wrong with the sensor.
2. Go to **Research & export** > **Sensor-health model** > **Train on the last 60 min**.
3. Until then, the simple transparent checks for stuck readings, noise and drift still run. In simulator mode you can press "Train on simulated normal data". That model is labelled simulated.

## Part 9: Alarms and the buzzer

The system raises few alarms on purpose. A condition must persist before it alarms, it needs clear evidence of recovery before it clears, and a repeat of the same problem reopens the existing alarm instead of adding a new one. The full table is in the **Bag & device setup** tab under **Alarm policy**, and in `docs/DESIGN.md` section 8.

- **HIGH** (10 quick beeps every 5 s): suspected flow interruption, or an unusually high mass-loss rate.
- **MEDIUM** (3 beeps every 15 s): low bag volume, data lost for 60 s, a stuck sensor, or a sensor problem that has lasted 10 minutes.
- **LOW** (no sound, shown on screen): sensor noise or drift that hasn't lasted long.
- **INFO** (no sound): bag replaced.

How to use it:
1. Infusion alarms only work while a bag is started (Part 7). Without a bag, a weight on the load cell won't set off interruption or low-volume alarms.
2. The sound plays in your browser. Press **Enable alarm sound** in the bar under the header once per browser tab. The bar always shows which pattern the system wants to play, even when sound is off.
3. **Acknowledge (silence)** stops the beeping, but the alarm stays on screen until the problem is gone. A HIGH alarm that is still active 5 minutes after you acknowledge it beeps again.
4. **Resolve** removes the alarm by hand. It won't come back until the condition has cleared once.

The node's own buzzer is driven by the firmware only (low level and sensor error, see Part 3). The backend never writes to the device, so the dashboard alarm patterns play in the browser, not on the node.

All alarm thresholds and timings are prototype defaults you can change in `.env`. They are not clinical values and haven't been validated. Step-by-step tests are in `docs/Alarm_Testing_Guide.md`.

## Part 10: Run experiments

For each test (for example closing the clamp to cause a flow interruption), go to **Research & export** > **Ground-truth labels**:

1. Choose the kind (for example `FLOW_INTERRUPTION`) and press **Start label now** when you start the condition.
2. Press **End now** when you stop it.
3. The **Detection metrics** panel then calculates precision, recall, F1, sensitivity, specificity and detection latency from your physical labels only. Simulated results always appear separately and are marked as simulated.
4. Export CSV files from **Export data** for your paper. Every file has a `source` column, so simulated and physical rows stay separate.

---

## Troubleshooting

| What you see | What to do |
|---|---|
| `docker: command not found` or `Cannot connect to the Docker daemon` | Start Docker Desktop and wait for "Engine running". |
| `port is already allocated` | Something else uses port 8000. In `docker-compose.yml` change `"8000:8000"` to `"8080:8000"` and open http://localhost:8080. |
| Red banner "BLYNK_TOKEN is not set" | `backend/.env` is missing the token, or the file is actually named `.env.txt`. |
| Check says `Invalid token` | Copy the Auth Token again from the Blynk app (device > Device Info). |
| Check says something about a redirect or wrong server | Your Blynk account is on a regional server. Put it in `.env`, e.g. `BLYNK_BASE_URL=https://blr1.blynk.cloud` (the address is shown in Blynk.Console). |
| Badge says STALE, "device offline" | The ESP32 isn't connected to Wi-Fi/Blynk. The value Blynk returns is then the last stored one, so the system deliberately won't treat it as live flow. |
| Weight pin error or `invalid` quality | `BLYNK_WEIGHT_PIN` points to a pin with no number on it. Use the connection check to pick the right one. |
| Values look 1000x too big or small | Fix `WEIGHT_SCALE` (Part 6). |
| LCD blank after flashing | Turn the contrast pot on the I2C backpack; try `LCD_ADDR 0x3F`; check SDA/SCL pins in the sketch. |
| LCD shows `SENSOR ERROR` | HX711 DT/SCK pins in the sketch don't match the wiring, or a loose wire. |
| Weight goes negative when loaded | Make `CALIBRATION_FACTOR` negative, or swap A+/A- on the HX711. |
| LCD shows `--` instead of `ON` | Wrong Wi-Fi name/password, 5 GHz-only Wi-Fi, or wrong Blynk token/server in the sketch. |
| Want to wipe all stored data | `docker compose down -v` deletes the database. This can't be undone. |

## Running without Docker (alternative)

Backend (Python 3.11+). Without `DATABASE_URL` it uses a local SQLite file instead of PostgreSQL.

```
cd backend
python -m venv .venv
.venv\Scripts\activate          (Mac/Linux: source .venv/bin/activate)
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

The backend reads `backend/.env`. API docs: http://localhost:8000/docs

Dashboard, either:
- **Build once** so the backend serves it on port 8000: `cd frontend`, `npm ci`, `npm run build` (creates `frontend/dist`), then restart the backend; or
- **Development mode** with live reload: `cd frontend`, `npm ci`, `npm run dev`, open http://localhost:5173 (API calls are forwarded to the backend on 8000).

Tests: `pip install -r requirements-dev.txt`, then `python -m pytest -q` inside `backend`.

## Status and limits

- Backend tests pass on simulated data and a fake Blynk server. A live Blynk read of V1 has worked; calibration and a continuous-flow bag test are still to be done.
- The Isolation Forest is untrained until you train it on a known-normal physical period (Part 8).
- All thresholds are prototype defaults, not clinical values. Mass-loss rate in g/min is not an exact volumetric flow rate (density assumption in `docs/DESIGN.md`).
- Keep simulated and physical results separate; the exports carry a `source` column for this.
