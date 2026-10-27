#!/usr/bin/env python3
"""Drive the Drink Refill Monitor's coaster simulator from a terminal.

Run it on your laptop (pointing at the board) or on the board itself.
Standard library only.

    python3 coaster.py --board 192.168.1.50 demo          # one-shot command
    python3 coaster.py --board 192.168.1.50 pour 300 --wait
    python3 coaster.py --board 192.168.1.50 watch         # live status line
    python3 coaster.py --board 192.168.1.50               # interactive shell

Set DRINK_BOARD=192.168.1.50 to skip --board. Run `help` for every command.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.request

STATE_ICON = {"ok": "🟢", "low": "🟠", "empty": "🔴", "lifted": "✋", "no_cup": "⚪",
              "setup": "⚙️ ", "fault": "⚠️ "}


NOT_ACTIONS = {"status", "st", "help", "?", "mode", "noise", "stop", "reset"}


class Board:
    def __init__(self, board: str, timeout: float = 8.0):
        if "://" not in board:
            board = "http://" + board
        if board.count(":") < 2:                      # no port given
            board += ":7000"
        self.url = board.rstrip("/") + "/api/sim"
        self.timeout = timeout

    def _request(self, method: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.url, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read() or b"{}")
            except ValueError:
                body = {}
            if e.code == 404:
                body.setdefault("error", "This board's app has no simulator endpoint. "
                                         "Is the latest version of the app running?")
            body.setdefault("error", f"HTTP {e.code}")
            body["ok"] = False
            return body
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            return {"ok": False, "error": f"Can't reach {self.url} ({reason}). Is the app running, "
                                          "and are you on the same network as the board?"}

    def status(self) -> dict:
        return self._request("GET")

    def run(self, argv: list[str]) -> dict:
        return self._request("POST", {"argv": argv})


# ---------------------------------------------------------------- output ---
def status_line(r: dict, show_queue: bool = True) -> str:
    m = r.get("monitor") or {}
    state = m.get("state", "?")
    parts = [f"{STATE_ICON.get(state, '•')} {state.upper():<7}"]
    if m.get("percent_left") is not None:
        parts.append(f"{m['percent_left']:>3}%")
    if m.get("drink_left_g") is not None:
        last = f" of {m['last_fill_g']:.0f} g" if m.get("last_fill_g") else ""
        parts.append(f"{m['drink_left_g']:.0f} g left{last}")
    if m.get("reminders_snoozed"):
        parts.append("reminders snoozed")
    c = r.get("coaster")
    if c:
        parts.append(f"cup {c['cup']}")
        if show_queue and c.get("queue"):
            parts.append("next: " + ", ".join(c["queue"][:4]) + ("…" if len(c["queue"]) > 4 else ""))
    elif r.get("source") == "real":
        parts.append("(real load cell)")
    return " · ".join(parts)


def print_result(r: dict, as_json: bool) -> bool:
    if as_json:
        print(json.dumps(r, indent=2))
        return r.get("ok", False)
    if not r.get("ok"):
        print(f"✗ {r.get('error', 'failed')}", file=sys.stderr)
        return False
    if r.get("message"):
        print(r["message"])
    for note in r.get("notes", []):
        print(f"  note: {note}")
    if r.get("seconds", 0) >= 1:
        print(f"  (takes about {r['seconds']:.0f} s)")
    print(status_line(r))
    return True


def change_key(r: dict) -> tuple:
    """What counts as 'something happened' when following along: the monitor's verdict."""
    m = r.get("monitor") or {}
    return m.get("state"), m.get("percent_left"), m.get("reminders_snoozed")


def wait_until_idle(board: Board, settle_s: float = 2.5, max_s: float = 600) -> dict:
    """Follow along until queued actions finish and the reading settles."""
    last_key, idle_since, start = None, None, time.time()
    r = board.status()
    while time.time() - start < max_s:
        r = board.status()
        if not r.get("ok"):
            return r
        key = change_key(r)
        if key != last_key:
            print(time.strftime("  %H:%M:%S ") + status_line(r, show_queue=False))
            last_key = key
        busy = (r.get("coaster") or {}).get("busy", False)
        if busy:
            idle_since = None
        elif idle_since is None:
            idle_since = time.time()
        elif time.time() - idle_since >= settle_s:
            break
        time.sleep(0.5)
    return r


def watch(board: Board) -> None:
    print("Watching (Ctrl-C to stop)…")
    last = None
    try:
        while True:
            r = board.status()
            key = change_key(r) if r.get("ok") else r.get("error")
            if key != last:
                line = status_line(r, show_queue=False) if r.get("ok") else f"✗ {r.get('error')}"
                print(time.strftime("%H:%M:%S ") + line)
                last = key
            time.sleep(1)
    except KeyboardInterrupt:
        print()


def run_one(board: Board, argv: list[str], wait: bool, as_json: bool) -> bool:
    if argv and argv[0] == "watch":
        watch(board)
        return True
    r = board.status() if not argv or argv[0] in ("status", "st") else board.run(argv)
    ok = print_result(r, as_json)
    is_action = bool(argv) and argv[0].lower() not in NOT_ACTIONS
    if ok and wait and is_action and r.get("coaster"):
        final = wait_until_idle(board)
        if not final.get("ok"):
            print(f"✗ {final.get('error')}", file=sys.stderr)
            return False
        if as_json:
            print(json.dumps(final, indent=2))
        elif final.get("monitor", {}).get("summary"):
            print("→ " + final["monitor"]["summary"])
    return ok


def shell(board: Board, as_json: bool) -> None:
    try:
        import readline  # noqa: F401  (arrow-key history where available)
    except ImportError:
        pass
    print(f"Coaster simulator at {board.url}")
    print("Type `help` for commands, `watch` for a live view, `quit` to leave.")
    print("Add `&` to a command to return immediately instead of following it (e.g. `demo &`).\n")
    first = board.status()
    print(status_line(first) if first.get("ok") else f"✗ {first.get('error')}")
    while True:
        try:
            line = input("coaster> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in ("quit", "exit", "q"):
            return
        background = line.endswith("&")
        try:
            argv = shlex.split(line.rstrip("&"))
        except ValueError as e:
            print(f"✗ {e}")
            continue
        try:
            run_one(board, argv, wait=not background, as_json=as_json)
        except KeyboardInterrupt:
            print("\n(stopped following — the actions keep running on the board; `stop` cancels them)")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Control the Drink Refill Monitor's coaster simulator.",
        epilog="Commands: place, remove, lift, down, pour, sip, straw, drain, refill, set, weight, "
               "fault, noise, wait, demo, stop, status, mode, watch, help.")
    ap.add_argument("--board", default=os.environ.get("DRINK_BOARD", "localhost:7000"),
                    help="board address, e.g. 192.168.1.50 or uno-q.local:7000 (default: $DRINK_BOARD "
                         "or localhost:7000)")
    ap.add_argument("--wait", "-w", action="store_true", help="follow the action until it finishes")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    ap.add_argument("command", nargs=argparse.REMAINDER, help="e.g. pour 300   (none = interactive shell)")
    args = ap.parse_args()
    # Allow flags after the command too: `coaster.py pour 300 --wait`
    for flag, attr in (("--wait", "wait"), ("-w", "wait"), ("--json", "json")):
        while flag in args.command:
            args.command.remove(flag)
            setattr(args, attr, True)

    board = Board(args.board)
    if not args.command:
        shell(board, args.json)
        return
    sys.exit(0 if run_one(board, args.command, args.wait, args.json) else 1)


if __name__ == "__main__":
    main()
