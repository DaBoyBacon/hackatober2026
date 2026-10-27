"""Drink Refill Monitor — Linux side (Qualcomm QRB2210 on the Arduino UNO Q).

Data flow:
    sketch.ino --Bridge.notify("hx711_sample", raw)--> on_sample() --queue--> loop()
    loop() -> DrinkMonitor.update() -> state/events
           -> Bridge.call("set_display", code, percent)   (LED matrix + RGB LED)
           -> Bridge.call("beep", n)                       (optional buzzer)
           -> WebUI "status" / "alert" messages            (dashboard on :7000)
           -> notifier.send(...)                           (optional phone push)

Everything that touches the DrinkMonitor runs on the loop() thread; Bridge and
WebUI callbacks only drop work onto queues, so there are no locking puzzles.
"""

from __future__ import annotations

import json
import os
import queue
import time
from collections import deque
from pathlib import Path

from arduino.app_utils import App, Bridge
from arduino.app_bricks.web_ui import WebUI

import notifier
from drink_monitor import DISPLAY_CODE, CalibrationError, DrinkMonitor, DrinkState, Settings

# ------------------------------------------------------------------ setup ---
APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DRINK_DATA_DIR", APP_DIR / "data"))
SETTINGS_FILE = DATA_DIR / "settings.json"

STATUS_PUSH_INTERVAL = 0.25   # s, max rate of dashboard updates
DISPLAY_HEARTBEAT = 5.0       # s, re-send display state so the MCU knows we're alive
HISTORY_SECONDS = 15 * 60     # sparkline length on the dashboard
LOOP_SLEEP = 0.05


def log(msg: str) -> None:
    print(f"[drink-monitor] {msg}", flush=True)


def load_settings() -> Settings:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            settings = Settings.from_dict(json.load(f))
        log(f"Loaded settings from {SETTINGS_FILE}")
        return settings
    except FileNotFoundError:
        log("No saved settings yet — starting setup.")
    except Exception as exc:
        log(f"Could not read {SETTINGS_FILE} ({exc}); using defaults.")
    return Settings()


def save_settings(settings: Settings) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings.to_dict(), f, indent=2)
    tmp.replace(SETTINGS_FILE)


monitor = DrinkMonitor(load_settings())
samples: "queue.SimpleQueue[tuple[str, float]]" = queue.SimpleQueue()
commands: "queue.SimpleQueue[tuple[str, str | None, object]]" = queue.SimpleQueue()
history: deque[tuple[float, float | None, str]] = deque(maxlen=HISTORY_SECONDS)

_last_push = 0.0
_last_display: tuple[int, int] | None = None
_last_display_sent = 0.0
_last_history_t = 0.0
_bridge_warned = False


# --------------------------------------------------------- Bridge (MCU) ---
def on_sample(raw: int) -> None:
    samples.put(("sample", float(raw)))


def on_fault(ms_without_data: int) -> None:
    samples.put(("fault", float(ms_without_data)))


Bridge.provide("hx711_sample", on_sample)
Bridge.provide("hx711_fault", on_fault)


def mcu_call(method: str, *args) -> None:
    global _bridge_warned
    try:
        Bridge.call(method, *args, timeout=1.5)
        _bridge_warned = False
    except Exception as exc:
        if not _bridge_warned:
            log(f"MCU call '{method}' failed ({exc}). Is the sketch running?")
            _bridge_warned = True


# ------------------------------------------------------------- Web UI ---
ui = WebUI()


def _queue(name):
    return lambda client, data=None: commands.put((name, client, data))


for _name in ("get_initial_state", "tare", "calibrate", "set_cup", "snooze",
              "save_settings", "test_alert", "reset_calibration"):
    ui.on_message(_name, _queue(_name))


def toast(text: str, kind: str = "info", client: str | None = None) -> None:
    ui.send_message("toast", {"kind": kind, "text": text}, client)


def settings_payload() -> dict:
    s = monitor.settings
    d = s.to_dict()
    d["scale_calibrated"] = s.is_scale_calibrated()
    d["cup_calibrated"] = s.is_cup_calibrated()
    return d


def history_payload() -> list:
    return [[round(t, 1), None if g is None else round(g, 1), st] for t, g, st in history]


def handle_command(name: str, client, data) -> None:
    s = monitor.settings
    try:
        if name == "get_initial_state":
            ui.send_message("settings", settings_payload(), client)
            ui.send_message("history", history_payload(), client)
            ui.send_message("status", monitor.status().to_dict(), client)
            return

        if name == "tare":
            monitor.tare()
            toast("Zeroed. Next: calibrate with a known weight.", "ok", client)
        elif name == "calibrate":
            grams = float((data or {}).get("grams", 0))
            monitor.calibrate_known_weight(grams)
            toast(f"Scale calibrated with {grams:g} g. Next: set the empty cup.", "ok", client)
        elif name == "set_cup":
            g = monitor.set_empty_cup()
            toast(f"Empty cup saved ({g:.0f} g). Fill it up!", "ok", client)
        elif name == "reset_calibration":
            monitor.reset_calibration()
            toast("Calibration cleared.", "info", client)
        elif name == "snooze":
            monitor.snooze()
            toast("Reminders snoozed until the next refill.", "info", client)
        elif name == "save_settings":
            incoming = dict(data or {})
            for key in ("low_percent", "empty_margin_g", "presence_g", "refill_jump_g",
                        "lift_grace_s", "reminder_minutes"):
                if key in incoming and incoming[key] not in (None, ""):
                    setattr(s, key, max(0.0, float(incoming[key])))
            if "notify_url" in incoming:
                url = str(incoming["notify_url"]).strip()
                if url and not url.startswith(("http://", "https://")):
                    raise CalibrationError("Notify URL must start with http:// or https://")
                s.notify_url = url
            toast("Settings saved.", "ok", client)
        elif name == "test_alert":
            fire_alert({"type": "empty", "message": "Test alert — this is what an empty cup looks like."},
                       test=True)
            if not s.notify_url:
                toast("Dashboard alert sent. Add a Notify URL to also get phone alerts.", "info", client)
            return
        else:
            return
    except CalibrationError as exc:
        toast(str(exc), "error", client)
        return
    except (TypeError, ValueError) as exc:
        toast(f"Couldn't do that: {exc}", "error", client)
        return

    save_settings(s)
    ui.send_message("settings", settings_payload())
    ui.send_message("status", monitor.status().to_dict())


# ------------------------------------------------------------- events ---
def fire_alert(event: dict, test: bool = False) -> None:
    kind = event["type"]
    msg = event.get("message", "")
    title = {"empty": "Refill needed", "reminder": "Still empty", "low": "Running low",
             "fault": "Sensor problem"}.get(kind, "Drink monitor")
    log(f"ALERT {kind}: {msg}")
    ui.send_message("alert", {"type": kind, "title": title, "message": msg, "test": test})

    if kind in ("empty", "reminder"):
        mcu_call("beep", 3 if kind == "empty" else 1)

    if kind in ("empty", "reminder", "fault") and monitor.settings.notify_url:
        def result(ok: bool, detail: str):
            if not ok:
                log(f"Notification failed: {detail}")
                ui.send_message("toast", {"kind": "error", "text": f"Phone notification failed: {detail}"})
        notifier.send(monitor.settings.notify_url, title, msg, kind, result)


def handle_events(events: list[dict]) -> None:
    for ev in events:
        t = ev["type"]
        if t in ("empty", "reminder", "low", "fault"):
            fire_alert(ev)
        elif t == "refilled":
            log(f"Refilled: {ev['liquid_g']} g")
            ui.send_message("alert", {"type": "refilled", "title": "Refilled",
                                      "message": f"{ev['liquid_g']:.0f} g of drink. Cheers!"})
        elif t == "state":
            log(f"State {ev['from']} -> {ev['to']}")
        elif t == "sensor_ok":
            log("Load cell is responding again.")


# --------------------------------------------------------------- loop ---
def publish(now: float) -> None:
    global _last_push, _last_display, _last_display_sent, _last_history_t
    status = monitor.status(now)

    if now - _last_history_t >= 1.0:
        liquid = status.liquid_g if status.state in (DrinkState.OK, DrinkState.LOW, DrinkState.EMPTY) else None
        history.append((now, liquid, status.state.value))
        _last_history_t = now

    if now - _last_push >= STATUS_PUSH_INTERVAL:
        ui.send_message("status", status.to_dict())
        _last_push = now

    display = (int(DISPLAY_CODE[status.state]), status.percent or 0)
    if display != _last_display or now - _last_display_sent >= DISPLAY_HEARTBEAT:
        mcu_call("set_display", *display)
        _last_display = display
        _last_display_sent = now


def loop() -> None:
    events: list[dict] = []

    # Drain everything the MCU sent since last time (normally 0-2 samples).
    while True:
        try:
            kind, value = samples.get_nowait()
        except queue.Empty:
            break
        events += monitor.update(value) if kind == "sample" else monitor.report_fault()

    while True:
        try:
            name, client, data = commands.get_nowait()
        except queue.Empty:
            break
        handle_command(name, client, data)

    events += monitor.check_timeouts()
    handle_events(events)
    publish(time.time())
    time.sleep(LOOP_SLEEP)


log("Starting. Open http://<board-ip>:7000 for the dashboard.")
App.run(user_loop=loop)
