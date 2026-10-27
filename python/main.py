"""Drink Refill Monitor — Linux side (Qualcomm QRB2210 on the Arduino UNO Q).

Data flow:
    sketch.ino --Bridge.notify("hx711_sample", raw)--> on_sample() --queue--> loop()
    or SimCoaster.advance() (no hardware)  ----------------------------------> loop()
    loop() -> DrinkMonitor.update() -> state/events
           -> Bridge.call("set_display", code, percent)   (LED matrix + RGB LED)
           -> Bridge.call("beep", n)                       (optional buzzer)
           -> WebUI "status" / "alert" messages            (dashboard on :7000)
           -> notifier.send(...)                           (optional phone push)
    LLM agents  --POST /mcp (MCP over HTTP)--> McpEndpoint -> agent_tools -> loop()
    tools/coaster.py --POST /api/sim--> sim_command() -> SimCoaster   (demo control)

Sensor source: "real" (HX711 via the sketch) or "sim" (the virtual coaster).
With DRINK_SENSOR=auto (the default) the app uses the real load cell if it
hears from it within a few seconds of starting, and the simulator otherwise.
The simulator keeps its own calibration and intake log, so demo data never
mixes with real data.

Everything that touches the DrinkMonitor runs on the loop() thread; Bridge,
WebUI, MCP and simulator requests only drop work onto a queue (and wait for
the result when they need one), so there are no locking puzzles.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import queue
import secrets
import threading
import time
from collections import deque
from pathlib import Path

from arduino.app_utils import App, Bridge
from arduino.app_bricks.web_ui import WebUI
from fastapi import Request, Response

import notifier
from agent_tools import INSTRUCTIONS, build_tools, iso, status_dict
from drink_monitor import (DISPLAY_CODE, HX711_SATURATED, CalibrationError, DrinkMonitor, DrinkState,
                           Settings)
from intake import IntakeLog, local_tz_offset_minutes
from mcp_server import CallContext, McpEndpoint, ToolError
from sim_coaster import (DEFAULT_CUP_G, SIM_COUNTS_PER_GRAM, SIM_OFFSET, SimCoaster, SimError,
                         help_text)

APP_VERSION = "1.2.0"

# ------------------------------------------------------------------ setup ---
APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DRINK_DATA_DIR", APP_DIR / "data"))
SETTINGS_FILE = DATA_DIR / "settings.json"
SIM_SETTINGS_FILE = DATA_DIR / "settings.sim.json"
SENSOR_MODE_FILE = DATA_DIR / "sensor_mode.txt"
TOKEN_FILE = DATA_DIR / "mcp_token.txt"

STATUS_PUSH_INTERVAL = 0.25   # s, max rate of dashboard updates
DISPLAY_HEARTBEAT = 5.0       # s, re-send display state so the MCU knows we're alive
HISTORY_SECONDS = 60 * 60     # kept for agents; the dashboard shows the last 15 min
DASHBOARD_HISTORY_S = 15 * 60
LOOP_SLEEP = 0.05
AUTO_SIM_AFTER_S = 6.0        # auto mode: no load cell heard by then -> simulator
REAL_SAMPLES_TO_SWITCH = 20   # auto mode: this many good real samples in a row -> real sensor
LOGGED_EVENTS = ("cup_placed", "refilled", "low", "empty", "reminder", "cup_removed", "fault", "sensor_ok")
SENSOR_MODES = ("auto", "real", "sim")


def log(msg: str) -> None:
    print(f"[drink-monitor] {msg}", flush=True)


def load_settings(path: Path, default: Settings) -> Settings:
    try:
        with open(path, encoding="utf-8") as f:
            settings = Settings.from_dict(json.load(f))
        log(f"Loaded settings from {path}")
        return settings
    except FileNotFoundError:
        pass
    except Exception as exc:
        log(f"Could not read {path} ({exc}); using defaults.")
    return default


def sim_default_settings() -> Settings:
    """The virtual coaster comes pre-calibrated, so a demo works straight away."""
    return Settings(tare_offset=SIM_OFFSET, counts_per_gram=SIM_COUNTS_PER_GRAM, cup_weight_g=DEFAULT_CUP_G)


def save_settings(settings: Settings) -> None:
    path = SIM_SETTINGS_FILE if settings is settings_sim else SETTINGS_FILE
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings.to_dict(), f, indent=2)
    tmp.replace(path)


def load_sensor_mode() -> str:
    env = os.environ.get("DRINK_SENSOR", "").strip().lower()
    if env in SENSOR_MODES:
        return env
    try:
        saved = SENSOR_MODE_FILE.read_text().strip().lower()
        if saved in SENSOR_MODES:
            return saved
    except FileNotFoundError:
        pass
    return "auto"


def load_or_create_token(regenerate: bool = False) -> str:
    """Bearer token agents must send. DRINK_MCP_TOKEN overrides; otherwise kept in data/."""
    env = os.environ.get("DRINK_MCP_TOKEN")
    if env and not regenerate:
        return env
    if not regenerate:
        try:
            tok = TOKEN_FILE.read_text().strip()
            if tok:
                return tok
        except FileNotFoundError:
            pass
    tok = secrets.token_urlsafe(24)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(tok + "\n")
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    return tok


settings_real = load_settings(SETTINGS_FILE, Settings())
settings_sim = load_settings(SIM_SETTINGS_FILE, sim_default_settings())
intake_real = IntakeLog(DATA_DIR / "intake.json")
intake_sim = IntakeLog(None)            # demo sips are kept in memory only
sim = SimCoaster()

sensor_mode = load_sensor_mode()        # what the user asked for: auto / real / sim
source = "sim" if sensor_mode == "sim" else "real"   # what's feeding the monitor right now
monitor = DrinkMonitor(settings_sim if source == "sim" else settings_real)
intake = intake_sim if source == "sim" else intake_real
sim.on_effect = lambda e: monitor.new_cup(e["cup_g"]) if e.get("type") == "new_cup" else None
if settings_sim.cup_weight_g:
    sim.cup_g = settings_sim.cup_weight_g   # keep the virtual cup and its calibration in step

samples: "queue.SimpleQueue[tuple[str, float]]" = queue.SimpleQueue()
commands: "queue.SimpleQueue[tuple[str, object, object]]" = queue.SimpleQueue()
history: deque[tuple[float, float | None, str]] = deque(maxlen=HISTORY_SECONDS)
event_log: deque[dict] = deque(maxlen=300)
agent_calls: deque[dict] = deque(maxlen=20)
mcp_token = load_or_create_token()

_last_push = 0.0
_last_display: tuple[int, int] | None = None
_last_display_sent = 0.0
_last_history_t = 0.0
_bridge_warned = False
_loop_thread: int | None = None
_started_at: float | None = None
_real_seen = False                      # has the real load cell ever answered since boot?
_real_streak = 0
_last_real_sample = 0.0
_last_sim_fault = 0.0


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


for _name in ("get_initial_state", "tare", "calibrate", "set_cup", "snooze", "save_settings",
              "test_alert", "reset_calibration", "get_agent_info", "regenerate_token"):
    ui.on_message(_name, _queue(_name))


def toast(text: str, kind: str = "info", client: str | None = None) -> None:
    ui.send_message("toast", {"kind": kind, "text": text}, client)


def settings_payload() -> dict:
    s = monitor.settings
    d = s.to_dict()
    d["scale_calibrated"] = s.is_scale_calibrated()
    d["cup_calibrated"] = s.is_cup_calibrated()
    return d


def source_payload() -> dict:
    return {"source": source, "mode": sensor_mode}


def history_payload() -> list:
    cutoff = time.time() - DASHBOARD_HISTORY_S
    return [[round(t, 1), None if g is None else round(g, 1), st] for t, g, st in history if t >= cutoff]


def agent_info_payload() -> dict:
    return {"token": mcp_token, "path": "/mcp", "calls": list(agent_calls)[::-1][:8],
            "today_g": intake.daily_totals(1, local_tz_offset_minutes())[-1]["grams"]}


def handle_command(name: str, client, data) -> None:
    global mcp_token
    if name == "__call__":                # work submitted by another thread (see run_on_loop)
        fut, fn = client, data
        if fut.set_running_or_notify_cancel():
            try:
                fut.set_result(fn())
            except BaseException as exc:
                fut.set_exception(exc)
        return

    s = monitor.settings
    try:
        if name == "get_initial_state":
            ui.send_message("source", source_payload(), client)
            ui.send_message("settings", settings_payload(), client)
            ui.send_message("history", history_payload(), client)
            ui.send_message("status", monitor.status().to_dict(), client)
            ui.send_message("agent_info", agent_info_payload(), client)
            return
        if name == "get_agent_info":
            ui.send_message("agent_info", agent_info_payload(), client)
            return
        if name == "regenerate_token":
            mcp_token = load_or_create_token(regenerate=True)
            log("MCP token regenerated; agents must be reconfigured.")
            ui.send_message("agent_info", agent_info_payload())
            toast("New agent token created. Update your agent's config.", "ok", client)
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
            log_event("snoozed", by="dashboard")
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
def log_event(type_: str, **detail) -> None:
    event_log.append({"t": time.time(), "type": type_, **detail})


def push_to_phone(title: str, msg: str, kind: str) -> bool:
    url = monitor.settings.notify_url
    if not url:
        return False
    if source == "sim":
        title = f"[demo] {title}"

    def result(ok: bool, detail: str):
        if not ok:
            log(f"Notification failed: {detail}")
            ui.send_message("toast", {"kind": "error", "text": f"Phone notification failed: {detail}"})
    notifier.send(url, title, msg, kind, result)
    return True


def fire_alert(event: dict, test: bool = False) -> None:
    kind = event["type"]
    msg = event.get("message", "")
    title = {"empty": "Refill needed", "reminder": "Still empty", "low": "Running low",
             "fault": "Sensor problem"}.get(kind, "Drink monitor")
    log(f"ALERT {kind}: {msg}")
    ui.send_message("alert", {"type": kind, "title": title, "message": msg, "test": test})
    if kind in ("empty", "reminder"):
        mcu_call("beep", 3 if kind == "empty" else 1)
    if kind in ("empty", "reminder", "fault"):
        push_to_phone(title, msg, kind)


def handle_events(events: list[dict]) -> None:
    for ev in events:
        t = ev["type"]
        if t in LOGGED_EVENTS:
            log_event(t, **{k: v for k, v in ev.items() if k not in ("type", "message")})
        if t in ("empty", "reminder", "low", "fault"):
            fire_alert(ev)
        elif t == "refilled":
            log(f"Refilled: {ev['liquid_g']} g")
            ui.send_message("alert", {"type": "refilled", "title": "Refilled",
                                      "message": f"{ev['liquid_g']:.0f} g of drink. Cheers!"})
        elif t == "drank":
            intake.add(ev["grams"])
        elif t == "state":
            log(f"State {ev['from']} -> {ev['to']}")
        elif t == "sensor_ok":
            log("Load cell is responding again.")


# ------------------------------------------------------ sensor source ---
def switch_source(new: str, reason: str) -> None:
    """Point the monitor at the real load cell or the simulator (loop thread only)."""
    global source, intake
    if new == source:
        return
    source = new
    monitor.swap_settings(settings_sim if new == "sim" else settings_real)
    intake = intake_sim if new == "sim" else intake_real
    history.clear()
    msg = f"{reason} — using the {'simulated' if new == 'sim' else 'real'} coaster."
    log(msg)
    log_event("sensor_source", source=new, reason=reason)
    ui.send_message("source", source_payload())
    ui.send_message("settings", settings_payload())
    ui.send_message("history", [])
    toast(msg, "info")


def set_sensor_mode(mode: str) -> str:
    global sensor_mode
    sensor_mode = mode
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SENSOR_MODE_FILE.write_text(mode + "\n")
    if mode == "sim":
        switch_source("sim", "Simulator selected")
    elif mode == "real":
        switch_source("real", "Real load cell selected")
    else:
        live = time.time() - _last_real_sample < 2.0
        switch_source("real" if live else "sim",
                      "Auto mode: load cell is answering" if live else "Auto mode: no load cell found")
    ui.send_message("source", source_payload())
    return (f"Sensor mode is now '{mode}'; readings come from the "
            f"{'simulated' if source == 'sim' else 'real'} coaster.")


def handle_real_sample(raw: float) -> list[dict]:
    global _real_seen, _real_streak, _last_real_sample
    valid = abs(raw) < HX711_SATURATED
    _real_streak = _real_streak + 1 if valid else 0
    if valid:
        _real_seen = True
        _last_real_sample = time.time()
    if source == "real":
        return monitor.update(raw)
    if sensor_mode == "auto" and _real_streak >= REAL_SAMPLES_TO_SWITCH:
        switch_source("real", "Load cell detected")
    return []


def check_auto_switch(now: float) -> None:
    # Only ever falls back to the simulator at start-up: if a real coaster stops
    # answering mid-demo, that's a fault worth showing, not something to paper over.
    if (sensor_mode == "auto" and source == "real" and not _real_seen
            and _started_at is not None and now - _started_at >= AUTO_SIM_AFTER_S):
        switch_source("sim", "No load cell found")


def tick_simulator(now: float) -> list[dict]:
    global _last_sim_fault
    if source != "sim":
        return []
    events: list[dict] = []
    for t, raw in sim.advance(now):
        events += monitor.update(raw, t)
    if sim.fault and now - _last_sim_fault >= 2.0:   # like the sketch's hx711_fault reports
        events += monitor.report_fault(now)
        _last_sim_fault = now
    return events


def sim_command(argv: list[str]) -> dict:
    """Run one coaster-CLI command (loop thread only)."""
    if not argv or argv[0] in ("status", "st"):
        return sim_status("")
    cmd = argv[0].lower()
    if cmd == "mode":
        if len(argv) != 2 or argv[1].lower() not in SENSOR_MODES:
            raise SimError("Usage: mode <sim|real|auto>")
        return sim_status(set_sensor_mode(argv[1].lower()))
    if cmd in ("help", "?"):
        return sim_status(help_text())
    if source != "sim":
        raise SimError("The board is reading the real load cell. Run `mode sim` to use the simulator.")
    result = sim.command(argv)
    out = sim_status(result["message"])
    out["seconds"] = result.get("seconds", 0)
    log(f"Simulator: {' '.join(argv)}")
    return out


def sim_status(message: str) -> dict:
    notes = list(sim.log)
    sim.log.clear()
    return {
        "ok": True,
        "message": message,
        "notes": notes,
        "source": source,
        "mode": sensor_mode,
        "coaster": sim.describe() if source == "sim" else None,
        "monitor": status_dict(AppHost()),
    }


# ------------------------------------------------------------ MCP host ---
class AppHost:
    """What agent_tools needs from the running app."""
    events = event_log
    history = history

    @property
    def monitor(self):
        return monitor

    @property
    def intake(self):
        return intake

    def run_on_loop(self, fn, timeout: float = 3.0):
        return run_on_loop(fn, timeout)

    def save_settings(self) -> None:
        save_settings(monitor.settings)
        ui.send_message("settings", settings_payload())

    def nudge(self, beeps: int, message: str, push: bool) -> bool:
        if beeps:
            mcu_call("beep", beeps)
        ui.send_message("alert", {"type": "nudge", "title": "From your assistant", "message": message})
        return push_to_phone("From your assistant", message, "nudge") if push else False

    def log_event(self, type_: str, **detail) -> None:
        log_event(type_, **detail)

    def sensor_ok(self) -> bool:
        return monitor.state != DrinkState.FAULT

    def simulated(self) -> bool:
        return source == "sim"


def run_on_loop(fn, timeout: float = 3.0):
    if threading.get_ident() == _loop_thread:
        return fn()
    fut: concurrent.futures.Future = concurrent.futures.Future()
    commands.put(("__call__", fut, fn))
    try:
        return fut.result(timeout)
    except concurrent.futures.TimeoutError:
        fut.cancel()
        raise ToolError("The board didn't answer in time. Is the app still running? Try again.")


def on_agent_call(tool: str, args: dict, ctx: CallContext, ok: bool) -> None:
    agent_calls.append({"time": iso(time.time()), "tool": tool, "client": ctx.client_name, "ok": ok})
    commands.put(("get_agent_info", None, None))   # refresh the dashboard's activity list


endpoint = McpEndpoint(
    name="drink-refill-monitor",
    title="Drink Refill Monitor",
    version=APP_VERSION,
    tools=build_tools(AppHost()),
    instructions=INSTRUCTIONS,
    token_provider=lambda: mcp_token,
    on_call=on_agent_call,
)


async def mcp_route(request: Request) -> Response:
    body = await request.body()
    status, headers, out = await asyncio.to_thread(
        endpoint.handle_http, request.method, dict(request.headers), body)
    return Response(content=out, status_code=status, headers=headers)


for _m in ("POST", "GET", "DELETE"):
    ui.expose_api(_m, "/mcp", mcp_route)


def _sim_http(method: str, headers: dict, body: bytes) -> tuple[int, dict]:
    h = {k.lower(): v for k, v in headers.items()}
    # Same browser-origin rule as /mcp, and JSON-only POSTs, so a random web page
    # can't drive the simulator from someone's browser.
    if not McpEndpoint._origin_ok(h.get("origin"), h.get("host")):
        return 403, {"ok": False, "error": "Origin not allowed."}
    argv: list[str] = []
    if method == "POST":
        if "application/json" not in h.get("content-type", ""):
            return 415, {"ok": False, "error": "Send JSON: {\"argv\": [\"pour\", \"300\"]}"}
        try:
            payload = json.loads(body or b"{}")
            argv = [str(a) for a in payload.get("argv", [])]
        except (ValueError, AttributeError, TypeError):
            return 400, {"ok": False, "error": "Body must be JSON like {\"argv\": [\"pour\", \"300\"]}"}
    try:
        return 200, run_on_loop(lambda: sim_command(argv))
    except SimError as exc:
        return 400, {"ok": False, "error": str(exc), "source": source, "mode": sensor_mode}
    except ToolError as exc:
        return 503, {"ok": False, "error": str(exc)}


async def sim_route(request: Request) -> Response:
    body = await request.body()
    status, payload = await asyncio.to_thread(_sim_http, request.method, dict(request.headers), body)
    return Response(content=json.dumps(payload), status_code=status, media_type="application/json")


for _m in ("GET", "POST"):
    ui.expose_api(_m, "/api/sim", sim_route)


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
    global _loop_thread, _started_at
    _loop_thread = threading.get_ident()
    if _started_at is None:
        _started_at = time.time()
    events: list[dict] = []

    # Drain everything the MCU sent since last time (normally 0-2 samples).
    while True:
        try:
            kind, value = samples.get_nowait()
        except queue.Empty:
            break
        if kind == "sample":
            events += handle_real_sample(value)
        elif source == "real":
            events += monitor.report_fault()

    while True:
        try:
            name, client, data = commands.get_nowait()
        except queue.Empty:
            break
        handle_command(name, client, data)

    now = time.time()
    check_auto_switch(now)
    events += tick_simulator(now)
    events += monitor.check_timeouts()
    handle_events(events)
    publish(now)
    intake_real.maybe_save(now)
    time.sleep(LOOP_SLEEP)


log(f"Starting (sensor mode: {sensor_mode}). Dashboard: http://<board-ip>:7000   "
    "Agents (MCP): http://<board-ip>:7000/mcp   Simulator: tools/coaster.py")
try:
    App.run(user_loop=loop)
finally:
    intake_real.save()
