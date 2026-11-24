# Connecting the working model: bench guide (11 Nov 2026)

Benchtop research prototype, not a medical device. Use water bags only, never a patient.

Who does what: **Sruthi** brings the hardware, **Meera** opens the Blynk app/console, **Carain** runs the software on the laptop.

## What to bring
- The node: load cell (40 kg), HX711, ESP32, LCD, buzzer, 5 V USB adapter + cable
- A stand/hook so the bag hangs freely from the load cell
- Known masses (anything weighed on a kitchen/lab scale works: e.g. 100 g, 200 g, 500 g, 1 kg)
- A 500 mL or 1000 mL bag of water with a giving set and roller clamp, plus a cup/beaker to catch the outflow
- Laptop with Docker Desktop and the `iv_monitor` folder, phone with the Blynk app, Wi-Fi the ESP32 knows

## Step 1. Hardware check (10 min, Sruthi)
1. Load cell → HX711: red E+, black E−, white A−, green A+ (colours vary; follow the module's labels).
2. HX711 → ESP32: VCC 3.3 V (or 5 V per your sketch), GND, DT and SCK to the pins your sketch uses.
3. LCD (I2C): SDA/SCL to the sketch's pins (usually GPIO 21/22), buzzer to its GPIO and GND.
4. Mount the load cell so only the hook end is loaded, nothing touching the bar, and the arrow on the cell pointing the direction of load.
5. Plug in the 5 V USB adapter. The LCD should show a weight. Press on the hook lightly and watch it change.

**Pass:** LCD shows a number that goes up when you load it and returns when you remove the load.

## Step 2. Blynk online (5 min, Meera)
1. Make sure the ESP32 is on Wi-Fi it knows (the SSID/password are in the sketch).
2. Blynk app → device "IOT" should say **Online**.
3. Note the value on the weight widget and compare it with the LCD.

**Pass:** device Online, Blynk value equals the LCD value.

## Step 3. Start the software in live mode (10 min, Carain)
1. In the `iv_monitor` folder open `.env` and set:
   ```
   DATA_SOURCE=blynk
   BLYNK_TOKEN=<device IOT auth token from Device Info>
   BLYNK_WEIGHT_PIN=V1
   LOAD_CELL_CAPACITY_G=40000
   ```
   `LOAD_CELL_CAPACITY_G=40000` is important: the default is 1000, and with the 40 kg cell a full 1000 mL bag (over 1000 g) would otherwise be rejected as "invalid".
   Keep `.env` private, never paste the token in chat.
2. Open a terminal in the folder and run `docker compose up -d --build`.
3. Open http://localhost:8000 → **Bag & device setup** → **Check Blynk connection**.

**Pass:** `device_online: true`, V1 equals the LCD, badge top right says **LIVE** (green).

If it says STALE: the ESP32 is offline. If UNAVAILABLE / invalid token: copy the token again. If redirect error: set `BLYNK_BASE_URL` to the server shown in Blynk.Console (e.g. `https://blr1.blynk.cloud`).

## Step 4. Calibration (20 min, everyone)
1. Empty hook. Wait 30 s. Note the reading (should be about 0). If not, tare the node (reset/tare as your sketch does it).
2. Dashboard → **Research & export** → **Calibration with known masses**.
3. Hang each known mass, wait until the value stops changing (about 20 s), type the true mass in grams, press **Record**. Do at least 3 masses, ideally 100 g, 200 g, 500 g, 1000 g. Repeat each once.
4. Read the panel's suggested correction. If it suggests a scale/offset, put them in `.env` as `WEIGHT_SCALE` and `WEIGHT_OFFSET_G`, then `docker compose up -d`.
5. Record the masses again to check the error is now small. Then set `UNITS_VERIFIED=true` and `docker compose up -d`.
6. Stationary test: leave the 500 g mass on for 5 minutes untouched. This gives your stability/noise figure.

**Pass:** readings within a few grams of the known masses. Write down MAE/RMSE as shown; these are real figures for the paper.

Take photos of the setup and screenshots of the calibration panel now.

## Step 5. Bag test (30-40 min)
1. Weigh the empty bag + giving set + hook (or hang the empty hook and note the reading). This is the **tare**.
2. Hang the full water bag, clamp closed. **Bag & device setup** → **Start a new bag**, enter the tare, tick "use current weight as initial weight".
3. Open the roller clamp to a slow drip into the beaker. Let it run 10-15 minutes. The dashboard should show NORMAL_FLOW, a mass-loss rate in g/min, and remaining % going down.
4. Flow interruption: **Research & export** → **Ground-truth labels** → kind `FLOW_INTERRUPTION` → **Start label now**, then close the clamp. Wait 3-4 minutes. Open the clamp and press **End now**. The dashboard should raise FLOW_INTERRUPTION after about 2 minutes.
5. Optional, if time: open the clamp fully for a minute (label `HIGH_MASS_LOSS_RATE`), then lift the bag off and rehang a fuller one (label `BAG_REPLACEMENT`).
6. Weigh the collected water at the end and compare with the drop in bag weight.
7. **Research & export** → **Export data**: download all CSVs. Screenshot the dashboard.

Press **Enable alarm sound** once in the browser to hear the alarm patterns. The node's own buzzer still follows its firmware.

## Step 6. After the session
- Train the sensor-health model: **Research & export** → **Sensor-health model** → **Train on the last 60 min** (only if that period was normal flow with no faults).
- Send the CSVs, screenshots and the connection-check output to the project so the paper/report can be updated with real results.

## If something fails
| Symptom | Likely cause / fix |
|---|---|
| LCD blank | Power or I2C wiring; adjust the LCD contrast pot |
| LCD reading jumps wildly | Loose load-cell wires, or the cell bar touching something |
| Reading goes down when loaded | Load cell mounted upside down or A+/A− swapped |
| Blynk Offline | Wrong Wi-Fi, or 2.4 GHz not available (ESP32 has no 5 GHz) |
| Dashboard "invalid reading" with bag on | `LOAD_CELL_CAPACITY_G` still 1000 |
| Rate is noisy around 0.5 g/min | Expected with whole-gram readings on a 40 kg cell; note as a limitation, or send one-decimal grams from the ESP32 |
