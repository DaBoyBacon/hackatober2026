# Drink Refill Monitor 🥤

An Arduino App for the **Arduino UNO Q** that weighs your cup on a load-cell
coaster and tells you when your drink is **running low** or **empty**.

- **On the board:** the 8×13 LED matrix shows a mug that drains as you drink and
  flashes `!` when it's empty; the RGB status LED goes green → amber → blinking red.
- **In the browser:** a live dashboard at `http://<board-ip>:7000` with the fill
  level, a 15-minute history, calibration steps and alert settings.
- **On your phone (optional):** a push notification through [ntfy](https://ntfy.sh)
  or a Discord webhook when the cup runs dry, with reminders until you refill.

It knows the difference between *empty* and *picked up for a sip*, and it spots
refills automatically — whether you pour into the cup on the coaster or carry it
to the kitchen and back.

---

## Why weight?

| Approach | Problem |
|---|---|
| Liquid-level probe in the cup | Something in your drink; needs cleaning; one cup only |
| IR / ultrasonic from above | Fooled by ice, foam, straws, clear liquids |
| **Load cell under a coaster** | **Works with any cup and any drink, nothing touches the liquid** |

The app remembers the weight of your empty cup, so *weight − cup = drink left*.

---

## Hardware

| Part | Notes |
|---|---|
| Arduino UNO Q | 2 GB or 4 GB |
| Bar load cell, 1 kg or 5 kg | 1 kg gives finer resolution for mugs; use 5 kg for big tumblers (cup + drink must stay under the rating) |
| HX711 load-cell amplifier module | The common green/red breakout boards work |
| Two rigid plates + spacers/screws | Coaster top and base; the bar is clamped between them, cantilever-style |
| *Optional:* active buzzer (3.3 V) | Beeps when the cup empties |

### Wiring

| HX711 pin | UNO Q pin |
|---|---|
| VCC | **3.3V** |
| GND | GND |
| DT (DOUT) | D3 |
| SCK | D2 |

Load cell → HX711: red → E+, black → E−, white → A−, green → A+ (colours vary by
vendor; if weight reads *negative*, it still works — calibration handles the sign).

Optional buzzer: `+` → D8, `−` → GND. No buzzer? Set `ENABLE_BUZZER` to `0` in
`sketch/sketch.ino` (leaving it at 1 is harmless, D8 just toggles).

```
   ┌───────── coaster top ─────────┐
   │                               │
   └──spacer──[==== load cell ====]│     screw the bar to the top plate at one
              │                    │     end and to the base at the other, with
   ┌──────────┴── base ────────────┘     spacers so the middle can flex
```

---

## Install

Copy this folder onto the board as `~/ArduinoApps/drink-refill-monitor`, then it
shows up under **My Apps** in Arduino App Lab.

```bash
# from your computer (replace the IP with your board's)
scp -r drink-refill-monitor arduino@192.168.1.50:~/ArduinoApps/
```

Then either press **Run** in App Lab, or on the board:

```bash
arduino-app-cli app start ~/ArduinoApps/drink-refill-monitor
arduino-app-cli app logs  ~/ArduinoApps/drink-refill-monitor --follow
```

App Lab compiles and flashes `sketch/` to the microcontroller and starts
`python/main.py` on Linux with the `web_ui` brick. Open
`http://<board-ip>:7000`.

---

## First-time setup (2 minutes, in the dashboard)

1. **Tare** — clear the coaster, press *Tare*.
2. **Calibrate** — put something of known weight on it (a sealed 500 ml water
   bottle is ≈ 520 g; a kitchen scale is better), type the grams, press *Calibrate*.
3. **Set empty cup** — put your empty cup down, wait for "Reading steady",
   press *Set empty cup*.

Fill the cup. That's it. Calibration is saved to `data/settings.json` and
survives restarts. Switching cups? Just do step 3 again.

---

## How detection works

```
HX711 ──raw counts──► sketch.ino ──Bridge.notify("hx711_sample")──► main.py
                                                                       │
                                                         drink_monitor.py
                                    median filter → grams → stable? → liquid = grams − cup
                                                                       │
   LED matrix + RGB LED + buzzer ◄──Bridge.call("set_display"/"beep")──┤
   Dashboard (socket.io)        ◄──WebUI.send_message("status"/"alert")┤
   Phone push (ntfy/Discord)    ◄──notifier.send()─────────────────────┘
```

- **Stable readings only.** Weight must hold within ±3 g for 1.5 s before it
  counts, so pouring, bumping, or stirring never triggers anything.
- **Lifted ≠ empty.** If the coaster suddenly reads ~0 g, the cup is in your
  hand. Status becomes *Cup lifted*; after 90 s with no cup it becomes *No cup*.
- **Refill detection.** A stable jump of ≥ 30 g (on the coaster, or after a
  lift) resets "full" to the new level.
- **States:** `ok` → `low` (≤ 25 % of the last fill) → `empty` (≤ 12 g of drink
  left). A few grams of hysteresis stop it flickering at the boundaries.
- **Alerts:** one on *low* (dashboard only), one on *empty* (dashboard, buzzer,
  phone), then a reminder every 10 minutes until you refill or press *Snooze*.
  Putting down a cup that's already empty doesn't alert — that's not news.

All thresholds are editable in the dashboard (*Alerts* card) or as defaults in
`Settings` in `python/drink_monitor.py`.

### Phone notifications (optional)

Install the ntfy app, subscribe to a hard-to-guess topic like
`my-drink-7f3k9q`, and paste `https://ntfy.sh/my-drink-7f3k9q` into
*Phone notification URL*. Press *Send test alert* to check. A Discord webhook
URL works too; any other URL receives a JSON POST `{title, message, event}`.

---

## Project layout

```
drink-refill-monitor/
├── app.yaml                 App Lab descriptor (web_ui brick, port 7000)
├── sketch/
│   ├── sketch.ino           MCU: HX711 sampling, LED matrix, RGB LED, buzzer
│   ├── hx711_lite.h         Dependency-free HX711 driver
│   └── sketch.yaml
├── python/
│   ├── main.py              Linux: Bridge + WebUI glue, event loop, persistence
│   ├── drink_monitor.py     Detection logic (pure Python, no Arduino imports)
│   └── notifier.py          ntfy / Discord / generic webhook
├── assets/                  Dashboard (index.html, app.js, style.css, libs/)
├── tests/                   Unit tests for the detection logic
├── tools/simulate.py        Run a simulated drinking session — no board needed
└── data/                    Created at runtime: settings.json
```

## Develop without the board

```bash
python3 -m unittest discover -s tests -v   # 21 tests
python3 tools/simulate.py                  # timeline of a simulated session
```

Sample output:

```
t=    7.5s  setup_done  {'cup_g': 309.9}
t=   12.2s  refilled    {'liquid_g': 400.4}
t=  395.6s  low         {'message': 'Running low — about to need a refill.'}
t=  515.0s  empty       {'message': 'Your drink is empty — time for a refill!'}
t=  635.0s  reminder    {'message': 'Still empty — grab a refill when you can.'}
t=  750.9s  refilled    {'liquid_g': 380.1}
t=  849.7s  cup_removed
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Dashboard says **Sensor problem**, matrix flashes `!` | HX711 isn't answering: check 3.3V/GND, and that DT→D3, SCK→D2 aren't swapped |
| Raw reading never changes when you press the coaster | Load-cell wires to E+/E−/A+/A− are wrong, or the bar isn't actually flexing (check spacers) |
| "Barely any change from the empty reading" during calibration | Same as above, or the known weight wasn't on the coaster |
| Reading drifts by several grams over hours | Normal for cheap load cells; re-tare occasionally, or raise *“Empty” below* to 15–20 g |
| Matrix shows a blinking `?` | Python isn't sending updates — the app is stopped or crashed; check `arduino-app-cli app logs … --follow` |
| Bridge calls fail after editing the app | In the App Lab terminal: `arduino-app-cli system cleanup`, then restart the app |
| No chime in the browser | Browsers only allow sound after you've clicked the page once |
