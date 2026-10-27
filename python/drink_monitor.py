"""Refill detection logic for the Drink Refill Monitor.

This module has no Arduino dependencies, so it can be unit-tested and
simulated on any computer (see tests/ and tools/simulate.py).

How it works
------------
A load cell under a coaster reports raw HX711 counts. We turn those into
grams with a tare offset and a scale factor, then:

* wait for the reading to be *stable* (cup set down, nobody touching it),
* subtract the weight of the empty cup to get grams of liquid left,
* compare that against the amount there was right after the last refill.

That gives four everyday states — no cup, OK, low, empty — plus "lifted"
(the cup was picked up to take a sip; we don't alarm while it's in your hand).
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from enum import Enum, IntEnum
from typing import Any

HX711_SATURATED = 8_388_600  # |raw| at or above this means the ADC is pinned (bad wiring)


class DrinkState(str, Enum):
    SETUP = "setup"        # scale or cup not calibrated yet
    NO_CUP = "no_cup"      # nothing on the coaster
    OK = "ok"
    LOW = "low"
    EMPTY = "empty"
    LIFTED = "lifted"      # cup picked up recently; waiting for it to come back
    FAULT = "fault"        # load cell not responding / reading garbage


class DisplayState(IntEnum):
    """Numeric codes understood by sketch/sketch.ino."""
    SETUP = 0
    NO_CUP = 1
    OK = 2
    LOW = 3
    EMPTY = 4
    LIFTED = 5
    FAULT = 6


DISPLAY_CODE = {
    DrinkState.SETUP: DisplayState.SETUP,
    DrinkState.NO_CUP: DisplayState.NO_CUP,
    DrinkState.OK: DisplayState.OK,
    DrinkState.LOW: DisplayState.LOW,
    DrinkState.EMPTY: DisplayState.EMPTY,
    DrinkState.LIFTED: DisplayState.LIFTED,
    DrinkState.FAULT: DisplayState.FAULT,
}


@dataclass
class Settings:
    # --- calibration (set from the web UI) ---
    tare_offset: float | None = None      # raw counts with an empty coaster
    counts_per_gram: float | None = None  # from the known-weight calibration
    cup_weight_g: float | None = None     # weight of the empty cup

    # --- detection tuning ---
    low_percent: float = 25.0        # at or below this % of the last fill -> LOW
    empty_margin_g: float = 12.0     # this little liquid (or less) counts as EMPTY
    presence_g: float = 25.0         # less than this on the coaster -> no cup
    refill_jump_g: float = 30.0      # a stable rise this big = you refilled
    hysteresis_g: float = 4.0        # stops flicker between states
    stable_window_s: float = 1.5     # reading must hold steady this long
    stable_tolerance_g: float = 3.0  # ...within this many grams
    lift_grace_s: float = 90.0       # how long a lifted cup stays "lifted" before "no cup"
    reminder_minutes: float = 10.0   # repeat the empty alert this often (0 = never)

    # --- notifications ---
    notify_url: str = ""             # e.g. https://ntfy.sh/my-secret-topic (optional)

    def is_scale_calibrated(self) -> bool:
        return self.tare_offset is not None and bool(self.counts_per_gram)

    def is_cup_calibrated(self) -> bool:
        return self.cup_weight_g is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Status:
    state: DrinkState
    grams_on_scale: float | None   # filtered weight on the coaster (incl. cup)
    liquid_g: float | None         # grams of drink left
    full_liquid_g: float | None    # grams of drink right after the last refill
    percent: int | None            # liquid_g / full_liquid_g, 0..100
    stable: bool
    raw: float | None
    snoozed: bool
    message: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        for k in ("grams_on_scale", "liquid_g", "full_liquid_g", "raw"):
            if d[k] is not None:
                d[k] = round(d[k], 1)
        return d


class CalibrationError(ValueError):
    pass


class DrinkMonitor:
    """Feed it raw HX711 samples with update(); read back Status and events."""

    RAW_HISTORY = 15   # raw samples kept for tare/calibration averaging
    SPIKE_WINDOW = 5   # median filter length for single-sample glitches
    SIP_MIN_G = 2.0    # smaller stable drops are treated as noise, not drinking

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self._raw: deque[float] = deque(maxlen=self.RAW_HISTORY)
        self._grams: deque[tuple[float, float]] = deque()   # (t, grams) within the stability window
        self._last_sample_t: float | None = None
        self._fault_since: float | None = None

        self._state = DrinkState.SETUP
        self._level_state = DrinkState.OK    # OK/LOW/EMPTY remembered while lifted
        self._liquid_g: float | None = None
        self._full_liquid_g: float | None = None
        self._last_stable_g: float | None = None
        self._lifted_at: float | None = None
        self._empty_since: float | None = None
        self._last_reminder: float | None = None
        self._snoozed = False
        self._last_status: Status | None = None
        self._last_refill_at: float | None = None
        self._counted_liquid_g: float | None = None

    # ------------------------------------------------------------ inputs ---
    def report_fault(self, now: float | None = None) -> list[dict]:
        """The MCU says the HX711 isn't producing data."""
        now = time.time() if now is None else now
        if self._fault_since is None:
            self._fault_since = now
        return self._set_state(DrinkState.FAULT, now)

    def update(self, raw: float, now: float | None = None) -> list[dict]:
        """Process one raw sample. Returns a list of events (possibly empty)."""
        now = time.time() if now is None else now
        self._last_sample_t = now

        if abs(raw) >= HX711_SATURATED:
            return self.report_fault(now)
        events: list[dict] = []
        if self._fault_since is not None:
            self._fault_since = None
            events.append({"type": "sensor_ok"})

        self._raw.append(float(raw))
        s = self.settings
        if not s.is_scale_calibrated():
            return events + self._set_state(DrinkState.SETUP, now)

        recent = list(self._raw)[-self.SPIKE_WINDOW:]
        grams = (statistics.median(recent) - s.tare_offset) / s.counts_per_gram
        self._grams.append((now, grams))
        while self._grams and now - self._grams[0][0] > s.stable_window_s:
            self._grams.popleft()

        if not s.is_cup_calibrated():
            return events + self._set_state(DrinkState.SETUP, now)

        present = grams >= self._presence_threshold()
        stable, stable_g = self._stability(now)

        if not present:
            # Cup is off the coaster. Start (or continue) the lift timer.
            if self._state in (DrinkState.OK, DrinkState.LOW, DrinkState.EMPTY):
                events += self._set_state(DrinkState.LIFTED, now)
            elif self._state == DrinkState.LIFTED:
                if now - (self._lifted_at or now) >= s.lift_grace_s:
                    events += self._set_state(DrinkState.NO_CUP, now)
            elif self._state != DrinkState.NO_CUP:
                events += self._set_state(DrinkState.NO_CUP, now)
            return events

        if not stable:
            # Cup is being set down / poured into / bumped. Keep the last verdict
            # until the reading settles.
            return events + self._emit_status(now)

        events += self._on_stable_weight(stable_g, now)
        events += self._maybe_remind(now)
        return events

    def check_timeouts(self, now: float | None = None) -> list[dict]:
        """Call periodically: handles lift grace expiry and empty reminders with no new samples."""
        now = time.time() if now is None else now
        events: list[dict] = []
        if (self._state == DrinkState.LIFTED and self._lifted_at is not None
                and now - self._lifted_at >= self.settings.lift_grace_s):
            events += self._set_state(DrinkState.NO_CUP, now)
        if self._last_sample_t is not None and now - self._last_sample_t > 5 and self._state != DrinkState.FAULT:
            events += self.report_fault(now)
        events += self._maybe_remind(now)
        return events

    # ------------------------------------------------------- calibration ---
    def average_raw(self) -> float:
        if len(self._raw) < 5:
            raise CalibrationError("Not enough readings yet — wait a second and try again.")
        values = list(self._raw)
        spread = max(values) - min(values)
        if self.settings.counts_per_gram and abs(spread / self.settings.counts_per_gram) > 10:
            raise CalibrationError("The reading is still moving — keep the coaster still and retry.")
        return statistics.median(values)

    def tare(self) -> float:
        """Coaster must be empty. Records the zero point."""
        self.settings.tare_offset = self.average_raw()
        self._reset_tracking()
        return self.settings.tare_offset

    def calibrate_known_weight(self, grams: float) -> float:
        """Coaster holds an object of known weight (e.g. a full 500 ml water bottle ≈ 520 g)."""
        if self.settings.tare_offset is None:
            raise CalibrationError("Tare the empty coaster first.")
        if grams <= 0:
            raise CalibrationError("Known weight must be more than 0 g.")
        delta = self.average_raw() - self.settings.tare_offset
        cpg = delta / grams
        if abs(cpg) < 1.0:
            raise CalibrationError(
                "Barely any change from the empty reading. Is the weight on the coaster "
                "and the load cell wired to the HX711?")
        self.settings.counts_per_gram = cpg
        self._grams.clear()
        self._reset_tracking()
        return cpg

    def set_empty_cup(self) -> float:
        """Coaster holds the empty cup. Records its weight."""
        if not self.settings.is_scale_calibrated():
            raise CalibrationError("Calibrate the scale first.")
        stable, grams = self._stability(time.time() if not self._grams else self._grams[-1][0])
        if not stable or grams is None:
            raise CalibrationError("Hold on — the reading isn't steady yet.")
        if grams < 5:
            raise CalibrationError("The coaster looks empty. Put the empty cup on it first.")
        self.settings.cup_weight_g = grams
        self._reset_tracking()
        return grams

    def reset_calibration(self) -> None:
        self.settings.tare_offset = None
        self.settings.counts_per_gram = None
        self.settings.cup_weight_g = None
        self._grams.clear()
        self._reset_tracking()

    def new_cup(self, cup_weight_g: float) -> None:
        """A different, empty cup is now in use (simulator shortcut for "Set empty cup")."""
        self.settings.cup_weight_g = cup_weight_g
        self._grams.clear()
        self._reset_tracking()

    def swap_settings(self, settings: "Settings") -> None:
        """Switch to another calibration (real sensor vs. simulator) and start fresh."""
        self.settings = settings
        self._raw.clear()
        self._grams.clear()
        self._last_sample_t = None
        self._fault_since = None
        self._last_refill_at = None
        self._reset_tracking()

    def snooze(self) -> None:
        """Silence repeat reminders until the next refill."""
        self._snoozed = True

    # ------------------------------------------------------------ output ---
    @property
    def state(self) -> DrinkState:
        return self._state

    @property
    def last_refill_at(self) -> float | None:
        """When the cup was last filled (or first placed with drink in it)."""
        return self._last_refill_at

    @property
    def empty_since(self) -> float | None:
        return self._empty_since if self._state in (DrinkState.EMPTY, DrinkState.LIFTED) else None

    def status(self, now: float | None = None) -> Status:
        """Snapshot of the current state (recomputed, so it reflects snooze/calibration changes)."""
        if now is None:
            now = self._grams[-1][0] if self._grams else time.time()
        self._emit_status(now)
        return self._last_status  # type: ignore[return-value]

    # ---------------------------------------------------------- internals ---
    def _presence_threshold(self) -> float:
        cup = self.settings.cup_weight_g or 0.0
        # A lightweight paper cup could weigh less than presence_g; scale down if needed.
        return min(self.settings.presence_g, cup * 0.6) if cup > 0 else self.settings.presence_g

    def _stability(self, now: float) -> tuple[bool, float | None]:
        s = self.settings
        if not self._grams:
            return False, None
        values = [g for _, g in self._grams]
        span_t = now - self._grams[0][0]
        steady = (max(values) - min(values)) <= s.stable_tolerance_g
        enough = span_t >= s.stable_window_s * 0.8 and len(values) >= 3
        return steady and enough, statistics.median(values)

    def _on_stable_weight(self, grams: float, now: float) -> list[dict]:
        s = self.settings
        events: list[dict] = []
        liquid = max(0.0, grams - (s.cup_weight_g or 0.0))
        prev_stable = self._last_stable_g
        self._last_stable_g = grams
        self._liquid_g = liquid

        refilled = False
        if self._full_liquid_g is None:
            # First reading after placing a cup (or after recalibrating).
            self._full_liquid_g = liquid
            if liquid > s.empty_margin_g + s.hysteresis_g:
                self._last_refill_at = now
                events.append({"type": "cup_placed", "liquid_g": round(liquid, 1)})
        elif prev_stable is not None and grams - prev_stable >= s.refill_jump_g:
            # Poured in while on the coaster, or picked up, refilled and put back.
            # (_last_stable_g survives a short lift, so both cases land here.)
            self._full_liquid_g = liquid
            self._snoozed = False
            self._last_refill_at = now
            refilled = True
            events.append({"type": "refilled", "liquid_g": round(liquid, 1)})
        elif liquid > self._full_liquid_g:
            self._full_liquid_g = liquid   # small top-up or settling

        # Intake accounting: every stable drop of a couple of grams is a sip.
        if (self._counted_liquid_g is None or refilled
                or liquid > self._counted_liquid_g + self.SIP_MIN_G):
            self._counted_liquid_g = liquid
        elif self._counted_liquid_g - liquid >= self.SIP_MIN_G:
            events.append({"type": "drank", "grams": round(self._counted_liquid_g - liquid, 1)})
            self._counted_liquid_g = liquid

        events += self._set_state(self._classify(liquid), now)
        return events

    def _classify(self, liquid: float) -> DrinkState:
        s = self.settings
        full = self._full_liquid_g or 0.0
        prev = self._level_state
        h = s.hysteresis_g

        empty_line = s.empty_margin_g + (h if prev == DrinkState.EMPTY else 0.0)
        if liquid <= empty_line:
            return DrinkState.EMPTY

        if full <= s.empty_margin_g:
            return DrinkState.OK
        pct = 100.0 * liquid / full
        low_line = s.low_percent + (100.0 * h / full if prev == DrinkState.LOW else 0.0)
        return DrinkState.LOW if pct <= low_line else DrinkState.OK

    def _set_state(self, new: DrinkState, now: float) -> list[dict]:
        events: list[dict] = []
        old = self._state
        if new != old:
            prev_level = self._level_state
            self._state = new
            if new in (DrinkState.OK, DrinkState.LOW, DrinkState.EMPTY):
                self._level_state = new
            if new == DrinkState.LIFTED:
                self._lifted_at = now

            # Only alert if the cup actually had a drink in it since it was put
            # down — an empty cup sitting on the coaster isn't news.
            had_drink = (self._full_liquid_g or 0.0) > self.settings.empty_margin_g + self.settings.hysteresis_g

            if new == DrinkState.EMPTY and self._empty_since is None:
                self._empty_since = now
                self._last_reminder = now
                if had_drink:
                    events.append({"type": "empty", "message": "Your drink is empty — time for a refill!"})
            elif new == DrinkState.LOW and prev_level == DrinkState.OK:
                events.append({"type": "low", "message": "Running low — about to need a refill."})
            elif new == DrinkState.NO_CUP:
                self._reset_tracking(keep_state=True)
                events.append({"type": "cup_removed"})
            elif new == DrinkState.FAULT:
                events.append({"type": "fault", "message": "Load cell isn't responding. Check the HX711 wiring."})
            if new in (DrinkState.OK, DrinkState.LOW):
                self._empty_since = None
            events.append({"type": "state", "from": old.value, "to": new.value})
        return events + self._emit_status(now)

    def _maybe_remind(self, now: float) -> list[dict]:
        mins = self.settings.reminder_minutes
        if (self._state == DrinkState.EMPTY and mins > 0 and not self._snoozed
                and self._last_reminder is not None and now - self._last_reminder >= mins * 60):
            self._last_reminder = now
            return [{"type": "reminder", "message": "Still empty — grab a refill when you can."}]
        return []

    def _emit_status(self, now: float) -> list[dict]:
        full = self._full_liquid_g
        liquid = self._liquid_g
        pct = None
        if liquid is not None and full is not None:
            # A cup that never held more than a splash counts as 0 %, not 100 %.
            pct = 0 if full <= self.settings.empty_margin_g else \
                int(max(0, min(100, round(100 * liquid / full))))
        stable, stable_g = self._stability(now) if self._grams else (False, None)
        grams_now = self._grams[-1][1] if self._grams else None
        self._last_status = Status(
            state=self._state,
            grams_on_scale=stable_g if stable else grams_now,
            liquid_g=liquid,
            full_liquid_g=full,
            percent=pct,
            stable=stable,
            raw=self._raw[-1] if self._raw else None,
            snoozed=self._snoozed,
            message=self._message(),
            timestamp=now,
        )
        return []

    def _message(self) -> str:
        s = self.settings
        st = self._state
        if st == DrinkState.SETUP:
            if s.tare_offset is None:
                return "Step 1: clear the coaster and press Tare."
            if not s.counts_per_gram:
                return "Step 2: put something of known weight on the coaster and calibrate."
            if s.cup_weight_g is None:
                return "Step 3: put your empty cup on the coaster and press Set empty cup."
            return "Waiting for a steady reading…"
        return {
            DrinkState.NO_CUP: "No cup on the coaster.",
            DrinkState.OK: "Plenty left.",
            DrinkState.LOW: "Running low.",
            DrinkState.EMPTY: "Empty — needs a refill!",
            DrinkState.LIFTED: "Cup lifted — enjoy your sip.",
            DrinkState.FAULT: "Load cell not responding. Check the HX711 wiring.",
        }[st]

    def _reset_tracking(self, keep_state: bool = False) -> None:
        self._full_liquid_g = None
        self._liquid_g = None
        self._last_stable_g = None
        self._counted_liquid_g = None
        self._lifted_at = None
        self._empty_since = None
        self._snoozed = False
        self._level_state = DrinkState.OK
        if not keep_state:
            self._state = DrinkState.SETUP
