/*
  IVORA node firmware: IV bag weight -> 16x2 LCD + buzzer + Blynk (V1)
  ---------------------------------------------------------------------
  Benchtop research prototype. NOT a medical device. Water bags only.

  Hardware: ESP32 DevKit v1, 40 kg bar load cell + HX711, 16x2 LCD with I2C
  backpack, active buzzer, 5 V USB power.

  What it does
    * Reads the HX711 about 10 times a second, averages, converts to grams.
    * Shows weight and status on the LCD.
    * Sends weight in grams (one decimal) to Blynk virtual pin V1 every second.
      The IVORA backend reads V1, so keep V1 = grams.
    * Sends a short status text to V2 ("OK", "LOW LEVEL", "SENSOR ERROR").
    * Beeps locally when the weight falls below LOW_LEVEL_G (an empty-ish bag)
      or when the HX711 stops answering. These local beeps are a simple
      threshold only; the smarter alarms live in the backend/dashboard.
    * Keeps working (LCD + buzzer) when Wi-Fi or Blynk is down, and retries.
    * Hold the BOOT button (GPIO0) for 3 s with the hook EMPTY to tare.
      The tare is saved in flash and survives power cuts.

  BEFORE YOU FLASH
    1. Back up the sketch that is on the board now. This file replaces it.
    2. Check the PIN numbers below against your actual wiring and change them
       if they differ. They are common defaults, NOT read from your board.
    3. Fill in Wi-Fi and Blynk placeholders. Never share the filled-in file.
    4. Do the calibration (CALIBRATION_MODE) once and set CALIBRATION_FACTOR.

  Libraries (Arduino IDE > Tools > Manage Libraries):
    * Blynk                by Volodymyr Shymanskyy  (1.3.x)
    * HX711                by Bogdan Necula (bogde)  (0.7.x)
    * LiquidCrystal I2C    by Frank de Brabander     (1.1.2)
  Board: "ESP32 Dev Module" (esp32 by Espressif Systems, Boards Manager).
*/

// ============ 1. Blynk template + credentials (PLACEHOLDERS) ==============
// Copy these three lines from Blynk.Console > your template > Firmware info.
#define BLYNK_TEMPLATE_ID   "TMPLxxxxxxxxx"
#define BLYNK_TEMPLATE_NAME "IVORA"
#define BLYNK_AUTH_TOKEN    "YOUR_DEVICE_AUTH_TOKEN"

// 2.4 GHz Wi-Fi only (the ESP32 has no 5 GHz radio).
const char WIFI_SSID[] = "YOUR_WIFI_NAME";
const char WIFI_PASS[] = "YOUR_WIFI_PASSWORD";

// If your Blynk account is on a regional server (e.g. blr1.blynk.cloud),
// put it here; otherwise leave "blynk.cloud".
const char BLYNK_SERVER[] = "blynk.cloud";

// #define BLYNK_PRINT Serial   // uncomment to see Blynk debug on Serial

#include <WiFi.h>
#include <WiFiClient.h>
#include <BlynkSimpleEsp32.h>
#include <HX711.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <Preferences.h>

// ============ 2. Pins (CHECK AGAINST YOUR WIRING) ==========================
const int PIN_HX711_DT  = 16;   // HX711 DT / DOUT
const int PIN_HX711_SCK = 17;   // HX711 SCK / PD_SCK
const int PIN_LCD_SDA   = 21;   // I2C SDA (ESP32 default)
const int PIN_LCD_SCL   = 22;   // I2C SCL (ESP32 default)
const int PIN_BUZZER    = 25;   // active buzzer + (other leg to GND)
const int PIN_TARE_BTN  = 0;    // on-board BOOT button (active LOW)
const uint8_t LCD_ADDR  = 0x27; // try 0x3F if the LCD stays blank

// Blynk virtual pins (the backend expects weight on V1)
#define VPIN_WEIGHT V1
#define VPIN_STATUS V2

// ============ 3. Calibration (SET THIS) ====================================
// Raw HX711 counts per gram. The value below is only a placeholder; every
// load cell differs. Find yours with CALIBRATION_MODE (see section 7) and
// paste it here. If weights read negative, flip the sign.
float CALIBRATION_FACTOR = 21.5f;   // <<< SET ME after calibration

// Set to 1, flash, open Serial Monitor at 115200 and follow the prompts to
// find CALIBRATION_FACTOR. Set back to 0 for normal use.
#define CALIBRATION_MODE 0

// ============ 4. Behaviour settings ========================================
const float LOW_LEVEL_G        = 100.0f;  // local beep below this (bag + set, g)
const float MIN_LOADED_G       = 20.0f;   // below this the hook counts as empty
const float LOAD_CELL_MAX_G    = 40000.0f;
const uint8_t SAMPLES_PER_READ = 5;       // HX711 averaging per reading
const uint32_t READ_MS         = 100;     // read interval
const uint32_t SEND_MS         = 1000;    // Blynk send interval (keep >= 1 s)
const uint32_t LCD_MS          = 500;
const uint32_t SENSOR_TIMEOUT_MS = 2000;  // no HX711 data for this long = error
const uint8_t MOVING_AVG_N     = 10;      // display/send smoothing (1 s at 100 ms)

// ===========================================================================
HX711 scale;
LiquidCrystal_I2C lcd(LCD_ADDR, 16, 2);
Preferences prefs;
BlynkTimer timer;

float ring[MOVING_AVG_N];
uint8_t ringCount = 0, ringIdx = 0;
float weightG = 0.0f;
bool sensorOk = false;
uint32_t lastGoodReadMs = 0;
uint32_t lastWifiTryMs = 0, lastBlynkTryMs = 0;
uint32_t btnDownMs = 0;
bool wasLow = false;

enum Status { ST_OK, ST_EMPTY, ST_LOW, ST_SENSOR_ERR };
Status status = ST_SENSOR_ERR;

const char* statusText(Status s) {
  switch (s) {
    case ST_OK:         return "OK";
    case ST_EMPTY:      return "NO BAG";
    case ST_LOW:        return "LOW LEVEL";
    default:            return "SENSOR ERROR";
  }
}

// ---------------------------------------------------------------- tare -----
void saveTare(long offset) {
  prefs.putLong("offset", offset);
  prefs.putBool("hasTare", true);
}

void doTare() {
  lcd.clear();
  lcd.print("Taring... keep");
  lcd.setCursor(0, 1);
  lcd.print("hook EMPTY");
  scale.tare(20);
  saveTare(scale.get_offset());
  ringCount = 0;
  ringIdx = 0;
  Serial.printf("Tare saved, offset=%ld\n", scale.get_offset());
  delay(800);
}

// ---------------------------------------------------------- reading --------
void readSensor() {
  if (scale.wait_ready_timeout(50)) {
    float g = scale.get_units(SAMPLES_PER_READ);
    if (isfinite(g) && g > -LOAD_CELL_MAX_G && g < LOAD_CELL_MAX_G * 1.1f) {
      ring[ringIdx] = g;
      ringIdx = (ringIdx + 1) % MOVING_AVG_N;
      if (ringCount < MOVING_AVG_N) ringCount++;
      float sum = 0;
      for (uint8_t i = 0; i < ringCount; i++) sum += ring[i];
      weightG = sum / ringCount;
      lastGoodReadMs = millis();
    }
  }
  sensorOk = (millis() - lastGoodReadMs) < SENSOR_TIMEOUT_MS && ringCount > 0;

  if (!sensorOk)                 status = ST_SENSOR_ERR;
  else if (weightG < MIN_LOADED_G) status = ST_EMPTY;
  else if (weightG < LOW_LEVEL_G)  status = ST_LOW;
  else                            status = ST_OK;
}

// ---------------------------------------------------------- buzzer ---------
// LOW LEVEL: 3 short beeps every 15 s.  SENSOR ERROR: 1 long beep every 10 s.
// Silent when OK or when no bag is hanging.
void buzzerTick() {
  static uint32_t patternStart = 0;
  uint32_t now = millis();
  bool on = false;

  if (status == ST_LOW) {
    uint32_t t = (now - patternStart) % 15000;
    on = (t < 150) || (t >= 300 && t < 450) || (t >= 600 && t < 750);
  } else if (status == ST_SENSOR_ERR) {
    uint32_t t = (now - patternStart) % 10000;
    on = t < 800;
  } else {
    patternStart = now;   // restart pattern cleanly next time
  }
  digitalWrite(PIN_BUZZER, on ? HIGH : LOW);
}

// ---------------------------------------------------------- LCD ------------
void updateLcd() {
  char line[17];
  lcd.setCursor(0, 0);
  if (sensorOk) snprintf(line, sizeof line, "Wt:%9.1f g  ", weightG);
  else          snprintf(line, sizeof line, "Wt:   ---- g    ");
  lcd.print(line);

  lcd.setCursor(0, 1);
  const char* net = Blynk.connected() ? "ON" : (WiFi.status() == WL_CONNECTED ? "WF" : "--");
  snprintf(line, sizeof line, "%-12s %2s", statusText(status), net);
  lcd.print(line);
}

// ---------------------------------------------------------- Blynk ----------
void sendToBlynk() {
  if (!Blynk.connected()) return;
  // Only send real readings; if the sensor is down, Blynk keeps the last value
  // and the status pin says SENSOR ERROR (the backend also sees no change).
  if (sensorOk) Blynk.virtualWrite(VPIN_WEIGHT, roundf(weightG * 10.0f) / 10.0f);

  static Status lastSent = (Status)255;
  if (status != lastSent) {
    Blynk.virtualWrite(VPIN_STATUS, statusText(status));
    lastSent = status;
  }
}

BLYNK_CONNECTED() {
  Blynk.virtualWrite(VPIN_STATUS, statusText(status));
}

void keepConnected() {
  uint32_t now = millis();
  if (WiFi.status() != WL_CONNECTED) {
    if (now - lastWifiTryMs > 15000 || lastWifiTryMs == 0) {
      lastWifiTryMs = now;
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASS);
    }
    return;
  }
  if (!Blynk.connected() && (now - lastBlynkTryMs > 10000 || lastBlynkTryMs == 0)) {
    lastBlynkTryMs = now;
    Blynk.connect(3000);   // short timeout so LCD/buzzer keep running
  }
}

// ---------------------------------------------------------- tare button ----
void checkTareButton() {
  if (digitalRead(PIN_TARE_BTN) == LOW) {
    if (btnDownMs == 0) btnDownMs = millis();
    if (millis() - btnDownMs > 3000) {
      doTare();
      btnDownMs = 0;
      while (digitalRead(PIN_TARE_BTN) == LOW) delay(10);
    }
  } else {
    btnDownMs = 0;
  }
}

// ---------------------------------------------------------- calibration ----
#if CALIBRATION_MODE
void runCalibration() {
  Serial.println("\n=== IVORA calibration ===");
  Serial.println("1) Remove everything from the hook, then send any key.");
  while (!Serial.available()) delay(50);
  while (Serial.available()) Serial.read();
  scale.set_scale();     // factor 1: get_units returns raw counts
  scale.tare(20);
  saveTare(scale.get_offset());
  Serial.println("2) Hang a KNOWN mass, type its grams (e.g. 500) and press Enter.");
  while (!Serial.available()) delay(50);
  float known = Serial.parseFloat();
  while (Serial.available()) Serial.read();
  float raw = scale.get_units(30);
  float factor = raw / known;
  Serial.printf("Raw counts: %.1f   Known: %.1f g\n", raw, known);
  Serial.printf(">>> CALIBRATION_FACTOR = %.4f\n", factor);
  Serial.println("Paste it into section 3, set CALIBRATION_MODE 0, flash again.");
  Serial.println("Then check with 2-3 other known masses (dashboard calibration panel).");
  scale.set_scale(factor);
  while (true) {
    Serial.printf("Check: %.1f g\n", scale.get_units(10));
    delay(1000);
  }
}
#endif

// ---------------------------------------------------------- setup/loop -----
void setup() {
  Serial.begin(115200);
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);
  pinMode(PIN_TARE_BTN, INPUT_PULLUP);

  Wire.begin(PIN_LCD_SDA, PIN_LCD_SCL);
  lcd.init();
  lcd.backlight();
  lcd.print("IVORA node");
  lcd.setCursor(0, 1);
  lcd.print("Research proto");

  prefs.begin("ivora", false);
  scale.begin(PIN_HX711_DT, PIN_HX711_SCK);

#if CALIBRATION_MODE
  runCalibration();
#endif

  scale.set_scale(CALIBRATION_FACTOR);
  if (prefs.getBool("hasTare", false)) {
    scale.set_offset(prefs.getLong("offset", 0));   // keep tare across reboots
  } else {
    delay(1500);
    doTare();                                       // first boot: hook must be empty
  }

  WiFi.mode(WIFI_STA);
  Blynk.config(BLYNK_AUTH_TOKEN, BLYNK_SERVER, 80);

  timer.setInterval(READ_MS, readSensor);
  timer.setInterval(SEND_MS, sendToBlynk);
  timer.setInterval(LCD_MS, updateLcd);
  lcd.clear();
}

void loop() {
  keepConnected();
  if (Blynk.connected()) Blynk.run();
  timer.run();
  buzzerTick();
  checkTareButton();
}
