"""Drink Refill Monitor — Linux side (Qualcomm QRB2210 on the Arduino UNO Q).

Data flow:
    sketch.ino --Bridge.notify("hx711_sample", raw)--> on_sample() --queue--> loop()
    loop() -> DrinkMonitor.update() -> state/events
           -> Bridge.call("set_display", code, percent)   (LED matrix + RGB LED)
           -> Bridge.call("beep", n)                       (optional buzzer)
           -> WebUI "status" / "alert" messages            (dashboard on :7000)
           -> notifier.send(...)                           (optional phone push)
    LLM agents  --POST /mcp (MCP over HTTP)--> McpEndpoint -> agent_tools -> loop()

Everything that touches the DrinkMonitor runs on the loop() thread; Bridge,
WebUI and MCP callbacks only drop work onto a queue (MCP calls then wait for
the result), so there are no locking puzzles.
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
from agent_tools import INSTRUCTIONS, build_tools, iso
from drink_monitor import DISPLAY_CODE, CalibrationError, DrinkMonitor, DrinkState, Settings
from intake import IntakeLog
from mcp_server import CallContext, McpEndpoint, ToolError

APP_VERSION = "1.1.0"

# ------------------------------------------------------------------ setup ---
APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DRINK_DATA_DIR", APP_DIR / "data"))
SETTINGS_FILE = DATA_DIR / "settings.json"
TOKEN_FILE = DATA_DIR / "mcp_token.txt"

STATUS_PUSH_INTERVAL = 0.25   # s, max rate of dashboard updates
DISPLAY_HEARTBEAT = 5.0       # s, re-send display state so the MCU knows we're alive
HISTORY_SECONDS = 60 * 60     # kept for agents; the dashboard shows the last 15 min
DASHBOARD_HISTORY_S = 15 * 60
LOOP_SLEEP = 0.05
LOGGED_EVENTS = ("cup_placed", "refilled", "low", "empty", "reminder", "cup_removed", "fault", "sensor_ok")


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


monitor = DrinkMonitor(load_settings())
intake = IntakeLog(DATA_DIR / "intake.json")
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


def history_payload() -> list:
    cutoff = time.time() - DASHBOARD_HISTORY_S
    return [[round(t, 1), None if g is None else round(g, 1), st] for t, g, st in history if t >= cutoff]


def agent_info_payload() -> dict:
    return {"token": mcp_token, "path": "/mcp", "calls": list(agent_calls)[::-1][:8],
            "today_g": intake.daily_totals(1, _local_offset())[-1]["grams"]}


def _local_offset() -> int:
    from intake import local_tz_offset_minutes
    return local_tz_offset_minutes()


def handle_command(name: str, client, data) -> None:
    global mcp_token
    if name == "__call__":                # work submitted by an MCP tool (see run_on_loop)
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


# ------------------------------------------------------------ MCP host ---
class AppHost:
    """What agent_tools needs from the running app."""
    monitor = monitor
    intake = intake
    events = event_log
    history = history

    def run_on_loop(self, fn, timeout: float = 3.0):
        if threading.get_ident() == _loop_thread:
            return fn()
        fut: concurrent.futures.Future = concurrent.futures.Future()
        commands.put(("__call__", fut, fn))
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise ToolError("The board didn't answer in time. Is the app still running? Try again.")

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
    global _loop_thread
    _loop_thread = threading.get_ident()
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
    now = time.time()
    publish(now)
    intake.maybe_save(now)
    time.sleep(LOOP_SLEEP)


log("Starting. Dashboard: http://<board-ip>:7000   Agents (MCP): http://<board-ip>:7000/mcp")
try:
    App.run(user_loop=loop)
finally:
    intake.save()
