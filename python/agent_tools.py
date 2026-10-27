"""The drink monitor's tools, as seen by an LLM agent over MCP.

Each handler runs its body on the app's main loop thread (host.run_on_loop),
the only thread allowed to touch the DrinkMonitor, so agent calls can't race
the sensor processing.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Protocol

from drink_monitor import DrinkState
from intake import local_tz_offset_minutes
from mcp_server import CallContext, Tool, ToolError

NUDGE_COOLDOWN_S = 60.0

INSTRUCTIONS = """\
This server reads a smart coaster: a load cell under the user's cup, attached to an \
Arduino UNO Q. It knows how much drink is left in the cup, when it was last refilled, \
and how much the user has drunk over time.

- Call get_drink_status before saying anything about the user's drink; it is live.
- "percent_left" is relative to the last refill, not the cup's capacity.
- Grams of drink ≈ millilitres for water, tea, coffee and most soft drinks.
- "lifted" means the cup is in the user's hand right now (taking a sip), not empty.
- If "simulated" is true, readings come from the built-in coaster simulator (a demo \
without the load cell). Mention that when it matters.
- nudge_drinker makes a sound and/or notification in the user's space; use it sparingly \
and only when the user asked to be reminded or clearly benefits."""


class Host(Protocol):
    monitor: Any
    intake: Any
    events: Any           # deque of {"t", "type", ...}
    history: Any          # deque of (t, liquid_g|None, state)

    def run_on_loop(self, fn, timeout: float = 3.0): ...
    def save_settings(self) -> None: ...
    def nudge(self, beeps: int, message: str, push_to_phone: bool) -> bool: ...
    def log_event(self, type_: str, **detail) -> None: ...
    def sensor_ok(self) -> bool: ...
    def simulated(self) -> bool: ...


def iso(t: float | None) -> str | None:
    if t is None:
        return None
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def minutes_since(t: float | None, now: float) -> float | None:
    return None if t is None else round((now - t) / 60.0, 1)


# ================================================================ helpers ===
def status_dict(host: Host, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    mon = host.monitor
    st = mon.status(now)
    s = mon.settings
    on_coaster = st.state in (DrinkState.OK, DrinkState.LOW, DrinkState.EMPTY)
    level_known = st.state in (DrinkState.OK, DrinkState.LOW, DrinkState.EMPTY, DrinkState.LIFTED) \
        and st.liquid_g is not None

    rate = host.intake.rate_g_per_min(30, now)
    eta = None
    if level_known and rate and st.state != DrinkState.EMPTY:
        eta = round(st.liquid_g / rate, 0)

    d = {
        "state": st.state.value,
        "summary": _summary(st, mon, now),
        "needs_refill": st.state == DrinkState.EMPTY,
        "cup_on_coaster": on_coaster,
        "percent_left": st.percent if level_known else None,
        "drink_left_g": round(st.liquid_g, 1) if level_known else None,
        "last_fill_g": round(st.full_liquid_g, 1) if level_known and st.full_liquid_g is not None else None,
        "last_refill_at": iso(mon.last_refill_at),
        "minutes_since_refill": minutes_since(mon.last_refill_at, now),
        "empty_for_minutes": minutes_since(mon.empty_since, now),
        "drinking_rate_g_per_min": round(rate, 1) if rate else None,
        "estimated_minutes_until_empty": eta,
        "reminders_snoozed": st.snoozed,
        "setup_complete": s.is_scale_calibrated() and s.is_cup_calibrated(),
        "sensor_ok": host.sensor_ok() and st.state != DrinkState.FAULT,
        "simulated": bool(getattr(host, "simulated", lambda: False)()),
        "as_of": iso(now),
    }
    return d


def _summary(st, mon, now) -> str:
    state = st.state
    if state == DrinkState.SETUP:
        return "The coaster hasn't been calibrated yet: " + st.message
    if state == DrinkState.FAULT:
        return "The load cell isn't responding, so the drink level is unknown."
    if state == DrinkState.NO_CUP:
        return "There is no cup on the coaster."
    left = f"{st.liquid_g:.0f} g" if st.liquid_g is not None else "an unknown amount"
    pct = f" ({st.percent}%)" if st.percent is not None else ""
    since = ""
    if mon.last_refill_at:
        mins = round((now - mon.last_refill_at) / 60)
        since = ", just refilled" if mins < 1 else f", refilled {mins} min ago"
    if state == DrinkState.EMPTY:
        mins = minutes_since(mon.empty_since, now)
        return f"The cup is empty and needs a refill (empty for {mins:.0f} min)." if mins is not None \
            else "The cup is empty and needs a refill."
    if state == DrinkState.LIFTED:
        return f"The user is holding the cup right now; before lifting it had {left}{pct} left."
    if state == DrinkState.LOW:
        return f"Running low: {left}{pct} left{since}."
    return f"Plenty left: {left}{pct}{since}."


def _local_offset(args: dict) -> int:
    off = args.get("utc_offset_minutes")
    return int(off) if off is not None else local_tz_offset_minutes()


# ================================================================== tools ===
def build_tools(host: Host) -> list[Tool]:

    def get_drink_status(args: dict, ctx: CallContext):
        return host.run_on_loop(lambda: status_dict(host))

    def get_drink_history(args: dict, ctx: CallContext):
        minutes = int(args.get("minutes", 30))

        def work():
            now = time.time()
            start = now - minutes * 60
            points = [p for p in host.history if p[0] >= start]
            # Downsample to ≤ 60 points: keep the last reading in each time bucket.
            bucket_s = max(1.0, minutes * 60 / 60)
            buckets: dict[int, tuple] = {}
            for p in points:
                buckets[int((p[0] - start) // bucket_s)] = p
            level = [{"time": iso(t), "drink_g": None if g is None else round(g, 1), "state": s}
                     for t, g, s in (buckets[k] for k in sorted(buckets))]
            events = [{"time": iso(e["t"]), **{k: v for k, v in e.items() if k != "t"}}
                      for e in host.events if e["t"] >= start]
            return {
                "window_minutes": minutes,
                "drunk_in_window_g": host.intake.drunk_since(start, now),
                "events": events,
                "level": level,
                "note": "drink_g is null while the cup was off the coaster.",
            }
        return host.run_on_loop(work)

    def get_intake_summary(args: dict, ctx: CallContext):
        days = int(args.get("days", 7))
        offset = _local_offset(args)

        def work():
            now = time.time()
            totals = host.intake.daily_totals(days, offset, now)
            past = [d["grams"] for d in totals[:-1] if d["grams"] > 0]
            return {
                "utc_offset_minutes": offset,
                "today_g": totals[-1]["grams"],
                "today_by_hour": host.intake.hourly_today(offset, now),
                "daily": totals,
                "average_g_per_day_excluding_today": round(sum(past) / len(past), 1) if past else None,
                "note": "Counts drink that left the cup while it was on the coaster or between "
                        "lifts. Hour buckets are UTC hours, so half-hour time zones are approximate.",
            }
        return host.run_on_loop(work)

    def snooze_refill_reminders(args: dict, ctx: CallContext):
        def work():
            host.monitor.snooze()
            host.log_event("snoozed", by="agent")
            return {"snoozed": True, "until": "the next refill", "status": status_dict(host)}
        return host.run_on_loop(work)

    def update_alert_settings(args: dict, ctx: CallContext):
        if not args:
            raise ToolError("Pass at least one of low_percent, empty_margin_g, reminder_minutes.")

        def work():
            s = host.monitor.settings
            changed = {}
            for k in ("low_percent", "empty_margin_g", "reminder_minutes"):
                if k in args:
                    old = getattr(s, k)
                    setattr(s, k, float(args[k]))
                    changed[k] = {"from": old, "to": float(args[k])}
            host.save_settings()
            host.log_event("settings_changed", by="agent", changes=changed)
            return {"changed": changed,
                    "settings": {k: getattr(s, k) for k in ("low_percent", "empty_margin_g", "reminder_minutes")}}
        return host.run_on_loop(work)

    def nudge_drinker(args: dict, ctx: CallContext):
        beeps = int(args.get("beeps", 2))
        message = (args.get("message") or "Time for a sip!").strip()
        push = bool(args.get("push_to_phone", False))

        def work():
            last = next((e["t"] for e in reversed(host.events) if e["type"] == "nudged"), None)
            if last is not None and time.time() - last < NUDGE_COOLDOWN_S:
                wait = NUDGE_COOLDOWN_S - (time.time() - last)
                raise ToolError(f"The user was nudged {time.time() - last:.0f} s ago. "
                                f"Wait {wait:.0f} s before nudging again.")
            pushed = host.nudge(beeps, message, push)
            host.log_event("nudged", by="agent", beeps=beeps, message=message, pushed=pushed)
            return {"nudged": True, "beeps": beeps, "message_shown": message,
                    "pushed_to_phone": pushed,
                    "note": None if pushed or not push else "No phone notification URL is configured."}
        return host.run_on_loop(work)

    offset_schema = {"type": "integer", "minimum": -720, "maximum": 840,
                     "description": "The user's UTC offset in minutes (e.g. -300 for US Central "
                                    "daylight time). Defaults to the board's clock."}

    return [
        Tool(
            name="get_drink_status",
            title="Get drink status",
            description="Live reading of the user's cup: whether it needs a refill, how much drink "
                        "is left (grams and % of the last fill), when it was refilled, current "
                        "drinking pace and an estimate of when it will run out.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=get_drink_status,
            annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        ),
        Tool(
            name="get_drink_history",
            title="Get recent drink history",
            description="Drink level over the last N minutes (up to 60) plus events such as "
                        "refills, low/empty alerts and when the cup was taken away.",
            input_schema={"type": "object", "additionalProperties": False, "properties": {
                "minutes": {"type": "integer", "minimum": 1, "maximum": 60, "default": 30,
                            "description": "How far back to look."}}},
            handler=get_drink_history,
            annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        ),
        Tool(
            name="get_intake_summary",
            title="Get intake summary",
            description="How much the user has drunk: today's total, today by hour, and daily "
                        "totals for the last N days (up to 30). Useful for hydration goals.",
            input_schema={"type": "object", "additionalProperties": False, "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 30, "default": 7},
                "utc_offset_minutes": offset_schema}},
            handler=get_intake_summary,
            annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
        ),
        Tool(
            name="snooze_refill_reminders",
            title="Snooze refill reminders",
            description="Stop the repeating 'still empty' reminders until the cup is next refilled. "
                        "The empty state itself is still reported.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=snooze_refill_reminders,
            annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                         "openWorldHint": False},
        ),
        Tool(
            name="update_alert_settings",
            title="Update alert settings",
            description="Change when the coaster warns: the 'low' threshold (% of the last fill), "
                        "the 'empty' threshold (grams left), and how often to repeat empty "
                        "reminders (0 turns repeats off). Only pass what should change.",
            input_schema={"type": "object", "additionalProperties": False, "properties": {
                "low_percent": {"type": "number", "minimum": 1, "maximum": 90},
                "empty_margin_g": {"type": "number", "minimum": 0, "maximum": 200},
                "reminder_minutes": {"type": "number", "minimum": 0, "maximum": 240}}},
            handler=update_alert_settings,
            annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                         "openWorldHint": False},
        ),
        Tool(
            name="nudge_drinker",
            title="Nudge the user to drink",
            description="Beep the coaster and show a banner on the dashboard, optionally also "
                        "sending a phone notification. Limited to once a minute.",
            input_schema={"type": "object", "additionalProperties": False, "properties": {
                "beeps": {"type": "integer", "minimum": 0, "maximum": 5, "default": 2},
                "message": {"type": "string", "maxLength": 140,
                            "description": "Short text for the banner/notification."},
                "push_to_phone": {"type": "boolean", "default": False}}},
            handler=nudge_drinker,
            annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                         "openWorldHint": True},
        ),
    ]
