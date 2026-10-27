// Drink Refill Monitor — MCU side (STM32U585 on the Arduino UNO Q)
//
// Job of this sketch:
//   1. Read raw counts from an HX711 load-cell amplifier under the coaster and
//      stream every sample to Python with Bridge.notify("hx711_sample", raw).
//   2. Show what Python decided: a mug on the 8x13 LED matrix that drains as
//      you drink, the RGB status LED (LED3), and an optional buzzer.
//
// All the "is the drink empty?" logic lives in python/drink_monitor.py, where
// it is easy to tune and test. If Python goes quiet, the board shows an
// "offline" pattern so stale info is never mistaken for a live reading.
//
// Wiring (see README.md for the full table):
//   HX711 VCC -> 3.3V   GND -> GND   DT -> D3   SCK -> D2
//   Optional active buzzer (+) -> D8, (-) -> GND

#include <Arduino_RouterBridge.h>
#include <Arduino_LED_Matrix.h>
#include "hx711_lite.h"

// ----------------------------------------------------------------- config ---
const uint8_t PIN_HX711_DOUT = 3;
const uint8_t PIN_HX711_SCK  = 2;
const uint8_t PIN_BUZZER     = 8;
#define ENABLE_BUZZER 1                 // set to 0 if nothing is wired to D8

const uint32_t PYTHON_TIMEOUT_MS  = 15000;  // no display update for this long -> offline
const uint32_t SENSOR_TIMEOUT_MS  = 1000;   // HX711 silent for this long -> report fault
const uint32_t FAULT_REPORT_MS    = 2000;
const uint32_t BLINK_PERIOD_MS    = 500;

// Must match DisplayState in python/drink_monitor.py
enum DisplayState : int {
  DS_SETUP   = 0,
  DS_NO_CUP  = 1,
  DS_OK      = 2,
  DS_LOW     = 3,
  DS_EMPTY   = 4,
  DS_LIFTED  = 5,
  DS_FAULT   = 6,
};

// ---------------------------------------------------------------- globals ---
HX711Lite scale(PIN_HX711_DOUT, PIN_HX711_SCK);
Arduino_LED_Matrix matrix;

const uint8_t ROWS = 8;
const uint8_t COLS = 13;
uint8_t frame[ROWS * COLS];

// Written from Bridge handlers, read in loop()
volatile int      g_state       = DS_SETUP;
volatile int      g_percent     = 0;
volatile uint32_t g_lastUpdate  = 0;
volatile int      g_beepsQueued = 0;
volatile bool     g_gotUpdate   = false;

uint32_t lastSampleMs   = 0;
uint32_t lastFaultMs    = 0;
uint32_t lastRenderKey  = 0xFFFFFFFF;

// ------------------------------------------------------------ Bridge API ---
// Called by Python whenever the drink state or level changes (and as a heartbeat).
void set_display(int state, int percent) {
  if (percent < 0) percent = 0;
  if (percent > 100) percent = 100;
  g_state = state;
  g_percent = percent;
  g_lastUpdate = millis();
  g_gotUpdate = true;
}

// Called by Python to sound the buzzer `count` times (e.g. when the cup empties).
void beep(int count) {
  if (count < 0) count = 0;
  if (count > 10) count = 10;
  g_beepsQueued = count;
}

// --------------------------------------------------------------- drawing ---
void px(int col, int row, uint8_t level) {
  if (col < 0 || col >= COLS || row < 0 || row >= ROWS) return;
  frame[row * COLS + col] = level;
}

// A mug: walls at columns 4 and 8, base on row 7, handle on the right.
// The inside (columns 5-7, rows 1-6) holds up to 6 rows of "liquid".
void drawMug(uint8_t wall, int liquidRows, uint8_t liquid) {
  for (int r = 1; r <= 7; r++) { px(4, r, wall); px(8, r, wall); }
  for (int c = 4; c <= 8; c++) px(c, 7, wall);
  px(9, 2, wall); px(10, 3, wall); px(10, 4, wall); px(9, 5, wall);
  for (int i = 0; i < liquidRows; i++) {
    int r = 6 - i;
    for (int c = 5; c <= 7; c++) px(c, r, liquid);
  }
}

void drawBang(uint8_t level) {          // "!" in the left margin
  for (int r = 1; r <= 4; r++) px(1, r, level);
  px(1, 6, level);
}

void drawCoaster(uint8_t level) {
  for (int c = 2; c <= 10; c++) px(c, 7, level);
}

void drawQuestion(uint8_t level) {      // "?" centred
  px(5, 1, level); px(6, 0, level); px(7, 1, level);
  px(7, 2, level); px(6, 3, level); px(6, 4, level);
  px(6, 6, level);
}

void render(int state, int percent, bool blinkOn, bool offline) {
  memset(frame, 0, sizeof(frame));

  if (offline) {
    if (blinkOn) drawQuestion(4);
    drawCoaster(1);
  } else {
    int rows = (percent * 6 + 50) / 100;  // 0..6 rows of liquid
    switch (state) {
      case DS_OK:
        drawMug(3, rows, 7);
        break;
      case DS_LOW:
        drawMug(3, rows < 1 ? 1 : rows, 7);
        drawBang(3);
        break;
      case DS_EMPTY:
        drawMug(blinkOn ? 7 : 2, 0, 0);
        if (blinkOn) drawBang(7);
        break;
      case DS_LIFTED:
        drawMug(1, 0, 0);
        drawCoaster(3);
        break;
      case DS_NO_CUP:
        drawCoaster(3);
        break;
      case DS_FAULT:
        if (blinkOn) drawBang(7);
        drawCoaster(blinkOn ? 4 : 1);
        break;
      case DS_SETUP:
      default: {
        drawCoaster(1);
        int dot = 2 + (int)((millis() / 200) % 9);  // a dot walking along the coaster
        px(dot, 7, 6);
        break;
      }
    }
  }
  matrix.draw(frame);
}

// ------------------------------------------------------------ status LED ---
// LED3 is an RGB LED driven by the MCU. It is active-low.
void setRgb(bool r, bool g, bool b) {
  digitalWrite(LED3_R, r ? LOW : HIGH);
  digitalWrite(LED3_G, g ? LOW : HIGH);
  digitalWrite(LED3_B, b ? LOW : HIGH);
}

void updateRgb(int state, bool blinkOn, bool offline) {
  if (offline)              { setRgb(false, false, blinkOn); return; }
  switch (state) {
    case DS_OK:             setRgb(false, true,  false); break;
    case DS_LOW:            setRgb(true,  true,  false); break;   // amber
    case DS_EMPTY:          setRgb(blinkOn, false, false); break;
    case DS_FAULT:          setRgb(blinkOn, false, blinkOn); break;
    case DS_SETUP:          setRgb(false, false, true); break;
    default:                setRgb(false, false, false); break;
  }
}

// ---------------------------------------------------------------- buzzer ---
// Non-blocking: each beep is 120 ms on, 180 ms off.
void updateBuzzer(uint32_t now) {
#if ENABLE_BUZZER
  static uint32_t phaseStart = 0;
  static bool on = false;
  if (on) {
    if (now - phaseStart >= 120) { digitalWrite(PIN_BUZZER, LOW); on = false; phaseStart = now; }
  } else if (g_beepsQueued > 0 && now - phaseStart >= 180) {
    g_beepsQueued = g_beepsQueued - 1;
    digitalWrite(PIN_BUZZER, HIGH);
    on = true;
    phaseStart = now;
  }
#else
  (void)now;
#endif
}

// ------------------------------------------------------------ setup/loop ---
void setup() {
  pinMode(LED3_R, OUTPUT);
  pinMode(LED3_G, OUTPUT);
  pinMode(LED3_B, OUTPUT);
  setRgb(false, false, false);

#if ENABLE_BUZZER
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);
#endif

  scale.begin();

  matrix.begin();
  matrix.setGrayscaleBits(3);   // brightness 0..7
  matrix.clear();

  Bridge.begin();
  Bridge.provide("set_display", set_display);
  Bridge.provide("beep", beep);

  lastSampleMs = millis();
}

void loop() {
  uint32_t now = millis();

  // 1) Stream raw load-cell samples to Python as soon as each one is ready.
  if (scale.isReady()) {
    int32_t raw = scale.read();
    Bridge.notify("hx711_sample", (int)raw);
    lastSampleMs = now;
  } else if (now - lastSampleMs > SENSOR_TIMEOUT_MS && now - lastFaultMs > FAULT_REPORT_MS) {
    // DOUT never went low: HX711 unpowered, unplugged, or wired to the wrong pins.
    Bridge.notify("hx711_fault", (int)(now - lastSampleMs));
    lastFaultMs = now;
  }

  // 2) Refresh the matrix and LED only when something visible changed.
  bool offline = !g_gotUpdate || (now - g_lastUpdate > PYTHON_TIMEOUT_MS);
  bool blinkOn = ((now / BLINK_PERIOD_MS) % 2) == 0;
  int state = g_state;
  int percent = g_percent;

  uint32_t key = ((uint32_t)state << 16) | ((uint32_t)percent << 8) |
                 (blinkOn ? 2u : 0u) | (offline ? 1u : 0u);
  if (state == DS_SETUP && !offline) key ^= (now / 200) << 20;  // keep the dot walking

  if (key != lastRenderKey) {
    render(state, percent, blinkOn, offline);
    updateRgb(state, blinkOn, offline);
    lastRenderKey = key;
  }

  // 3) Buzzer pattern.
  updateBuzzer(now);

  delay(5);
}
