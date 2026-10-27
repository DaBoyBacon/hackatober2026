"""How much has been drunk, and when.

Sips are stored in UTC hour buckets ({epoch_hour: grams}) so the file stays
small (30 days ≈ 720 entries) and days can be regrouped into whatever time
zone the person — or the agent asking — is in.
"""

from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

KEEP_DAYS = 30


class IntakeLog:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.hours: dict[int, float] = {}
        self.recent: deque[tuple[float, float]] = deque()   # (t, grams) for the last 2 h
        self._dirty = False
        self._last_save = 0.0
        if path:
            self._load()

    # ------------------------------------------------------------ writes ---
    def add(self, grams: float, now: float | None = None) -> None:
        if grams <= 0:
            return
        now = time.time() if now is None else now
        hour = int(now // 3600)
        self.hours[hour] = round(self.hours.get(hour, 0.0) + grams, 1)
        self.recent.append((now, grams))
        self._trim(now)
        self._dirty = True

    def maybe_save(self, now: float | None = None, every_s: float = 30.0) -> None:
        now = time.time() if now is None else now
        if self._dirty and self.path and now - self._last_save >= every_s:
            self.save()
            self._last_save = now

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "hours": {str(k): v for k, v in sorted(self.hours.items())}}))
        tmp.replace(self.path)
        self._dirty = False

    # ------------------------------------------------------------- reads ---
    def drunk_since(self, since: float, now: float | None = None) -> float:
        """Grams drunk since a timestamp within the last 2 hours (exact, from sip events)."""
        return round(sum(g for t, g in self.recent if t >= since), 1)

    def rate_g_per_min(self, window_min: float = 30.0, now: float | None = None) -> float | None:
        """Recent drinking pace, or None if nothing was drunk in the window."""
        now = time.time() if now is None else now
        sips = [(t, g) for t, g in self.recent if t >= now - window_min * 60]
        if not sips:
            return None
        # Measure from the first sip (or the window start, whichever is later) so a
        # single sip a minute ago doesn't look like a frantic pace.
        span_min = max((now - sips[0][0]) / 60.0, 5.0)
        return sum(g for _, g in sips) / span_min

    def daily_totals(self, days: int, tz_offset_minutes: int, now: float | None = None) -> list[dict]:
        """[{date, grams}] for the last `days` local days, oldest first, today last."""
        now = time.time() if now is None else now
        tz = timezone(timedelta(minutes=tz_offset_minutes))
        today = datetime.fromtimestamp(now, tz).date()
        totals = {today - timedelta(days=i): 0.0 for i in range(days)}
        for hour, grams in self.hours.items():
            # Attribute each hour bucket to the local date of its midpoint.
            d = datetime.fromtimestamp(hour * 3600 + 1800, tz).date()
            if d in totals:
                totals[d] += grams
        return [{"date": d.isoformat(), "grams": round(totals[d], 1)} for d in sorted(totals)]

    def hourly_today(self, tz_offset_minutes: int, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        tz = timezone(timedelta(minutes=tz_offset_minutes))
        today = datetime.fromtimestamp(now, tz).date()
        out = []
        for hour, grams in sorted(self.hours.items()):
            local = datetime.fromtimestamp(hour * 3600, tz)
            if local.date() == today and grams > 0:
                out.append({"hour": local.strftime("%H:00"), "grams": round(grams, 1)})
        return out

    # --------------------------------------------------------- internals ---
    def _trim(self, now: float) -> None:
        while self.recent and self.recent[0][0] < now - 2 * 3600:
            self.recent.popleft()
        oldest = int(now // 3600) - KEEP_DAYS * 24
        for h in [h for h in self.hours if h < oldest]:
            del self.hours[h]

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            self.hours = {int(k): float(v) for k, v in data.get("hours", {}).items()}
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"[drink-monitor] Ignoring unreadable {self.path}: {exc}", flush=True)


def local_tz_offset_minutes(now: float | None = None) -> int:
    now = time.time() if now is None else now
    return int(datetime.fromtimestamp(now).astimezone().utcoffset().total_seconds() // 60)
