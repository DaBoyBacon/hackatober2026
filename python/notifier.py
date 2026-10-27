"""Optional push notifications when the drink runs out.

Set "Notify URL" in the web UI to one of:

* an ntfy topic, e.g. https://ntfy.sh/my-drink-7f3k  (install the ntfy app and
  subscribe to the same topic to get phone notifications)
* a Discord webhook URL
* any other URL — it receives a JSON POST: {"title", "message", "event"}

Sending happens on a background thread so a slow network never stalls the
monitor loop.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from urllib.parse import urlparse


def _build_request(url: str, title: str, message: str, event: str) -> urllib.request.Request:
    host = urlparse(url).netloc.lower()
    if "ntfy" in host:
        tags = {"empty": "cup_with_straw,warning", "reminder": "hourglass", "low": "cup_with_straw"}
        return urllib.request.Request(
            url,
            data=message.encode("utf-8"),
            headers={"Title": title, "Tags": tags.get(event, "cup_with_straw"),
                     "Priority": "high" if event == "empty" else "default"},
            method="POST",
        )
    if "discord.com" in host or "discordapp.com" in host:
        body = {"content": f"**{title}**\n{message}"}
    else:
        body = {"title": title, "message": message, "event": event}
    return urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )


def send(url: str, title: str, message: str, event: str = "", on_result=None) -> None:
    """Fire-and-forget. on_result(ok: bool, detail: str) is called from the worker thread."""
    if not url:
        return

    def worker():
        try:
            req = _build_request(url, title, message, event)
            with urllib.request.urlopen(req, timeout=8) as resp:
                ok = 200 <= resp.status < 300
                detail = f"HTTP {resp.status}"
        except Exception as exc:  # network down, bad URL, etc.
            ok, detail = False, str(exc)
        if on_result:
            on_result(ok, detail)

    threading.Thread(target=worker, name="notify", daemon=True).start()
