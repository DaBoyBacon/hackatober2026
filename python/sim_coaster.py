"""A virtual coaster: stands in for the load cell + HX711 when there's no hardware.

It models what is physically on the coaster (a cup, the drink in it, a hand
lifting it, a calibration weight) and turns that into the same raw HX711
counts the real sensor would send, ten times a second, with realistic noise
and the clunk when a cup is set down. Everything downstream — detection,
dashboard, LED matrix, alerts, AI-agent tools — runs unchanged on top of it.

Actions like "pour 300" or "sip" play out over real seconds (a pour takes a
few seconds, a sip lifts the cup for a couple of seconds) and are queued, so
you can type several commands in a row and watch them happen.
"""

from __future__ import annotations

import math
import random
import shlex
from collections import deque
from typing import Generator

SIM_OFFSET = 120_000       # raw counts with an empty coaster
SIM_COUNTS_PER_GRAM = 415.0
DEFAULT_CUP_G = 300.0
DEFAULT_FILL_G = 350.0
CAPACITY_G = 600.0         # how much the virtual cup holds
SAMPLE_HZ = 10

Action = Generator[float, None, None]   # yields seconds to wait before continuing


class SimError(ValueError):
    """A command that can't run (bad argument or impossible right now)."""


class SimCoaster:
    def __init__(self, seed: int | None = None):
        self.rng = random.Random(seed)
        self.cup_g = DEFAULT_CUP_G
        self.cup_present = True        # cup on the coaster (or in your hand), vs. gone
        self.lifted = False            # in your hand right now
        self.liquid_g = DEFAULT_FILL_G
        self.extra_g = 0.0             # a calibration weight sitting on the coaster
        self.noise_g = 0.4
        self.fault = False             # pretend the HX711 is unplugged
        self._wobble_until = 0.0
        self._wobble_g = 0.0
        self._queue: deque[tuple[str, Action]] = deque()
        self._current: tuple[str, Action] | None = None
        self._resume_at = 0.0
        self._next_sample = None
        self.log: deque[str] = deque(maxlen=20)  # messages from actions that ran later
        self._now = 0.0
        # Called (on the loop thread) when the virtual world changes in a way the
        # monitor should know about, e.g. {"type": "new_cup", "cup_g": 250}.
        self.on_effect = lambda effect: None

    # ----------------------------------------------------------- physics ---
    def weight_on_coaster(self, now: float) -> float:
        g = self.extra_g
        if self.cup_present and not self.lifted:
            g += self.cup_g + self.liquid_g
            if now < self._wobble_until:
                g += self.rng.uniform(-self._wobble_g, self._wobble_g)
        return g

    def raw_at(self, now: float) -> int:
        grams = self.weight_on_coaster(now) + self.rng.gauss(0, self.noise_g)
        return int(SIM_OFFSET + grams * SIM_COUNTS_PER_GRAM)

    def wobble(self, now: float, seconds: float, grams: float) -> None:
        self._wobble_until = now + seconds
        self._wobble_g = grams

    # -------------------------------------------------------------- clock ---
    def advance(self, now: float) -> list[tuple[float, int]]:
        """Run queued actions up to `now` and return the samples due: [(t, raw), ...]."""
        if self._next_sample is None or now - self._next_sample > 1.0:
            self._next_sample = now            # first call, or the loop stalled: don't flood
        samples = []
        while self._next_sample <= now:
            t = self._next_sample
            self._run_actions(t)
            if not self.fault:
                samples.append((t, self.raw_at(t)))
            self._next_sample += 1.0 / SAMPLE_HZ
        return samples

    def _run_actions(self, now: float) -> None:
        while True:
            if self._current is None:
                if not self._queue:
                    return
                self._current = self._queue.popleft()
                self._resume_at = now
            if now < self._resume_at:
                return
            name, gen = self._current
            self._now = now
            try:
                self._resume_at = now + next(gen)
            except StopIteration:
                self._current = None
            except SimError as exc:
                self.log.append(f"{name}: skipped — {exc}")
                self._current = None

    @property
    def busy(self) -> bool:
        return self._current is not None or bool(self._queue)

    def queued(self) -> list[str]:
        names = [self._current[0]] if self._current else []
        return names + [n for n, _ in self._queue]

    def stop(self) -> None:
        self._queue.clear()
        self._current = None
        if self.lifted and self.cup_present:
            self.lifted = False

    def describe(self) -> dict:
        return {
            "cup": ("in hand" if self.lifted else "on coaster") if self.cup_present else "away",
            "cup_g": self.cup_g,
            "drink_g": round(self.liquid_g, 1),
            "calibration_weight_g": self.extra_g,
            "on_coaster_g": round(self.extra_g + (self.cup_g + self.liquid_g
                                                  if self.cup_present and not self.lifted else 0.0), 1),
            "sensor_fault": self.fault,
            "busy": self.busy,
            "queue": self.queued(),
        }

    # ========================================================== actions ===
    # Each action is a generator: it changes the virtual world, then yields how
    # many seconds to wait. Preconditions are re-checked when the action runs,
    # because earlier queued actions may have changed things.

    def _require_cup(self, on_coaster: bool = False):
        if not self.cup_present:
            raise SimError("there's no cup — run `place` first")
        if on_coaster and self.lifted:
            raise SimError("the cup is in your hand — run `down` first")

    def _a_place(self, cup_g: float | None):
        if cup_g is not None:
            # A different, empty cup. Tell the monitor its weight so the demo
            # doesn't need a trip through the dashboard's "Set empty cup".
            self.cup_g, self.liquid_g = cup_g, 0.0
            self.on_effect({"type": "new_cup", "cup_g": cup_g})
        self.cup_present, self.lifted = True, False
        self.wobble(self._now, 0.6, 15)
        yield 0.6

    def _a_remove(self):
        self._require_cup()
        self.cup_present, self.lifted = False, False
        yield 0.1

    def _a_lift(self):
        self._require_cup()
        self.lifted = True
        yield 0.1

    def _a_down(self):
        self._require_cup()
        self.lifted = False
        self.wobble(self._now, 0.6, 15)
        yield 0.6

    def _a_pour(self, grams: float, seconds: float):
        self._require_cup(on_coaster=True)
        start = self.liquid_g
        target = min(CAPACITY_G, start + grams)
        steps = max(1, int(seconds * SAMPLE_HZ))
        for i in range(1, steps + 1):
            self.liquid_g = start + (target - start) * i / steps
            self.wobble(self._now, 0.2, 10)    # stream hitting the cup
            yield 1.0 / SAMPLE_HZ
        if target < start + grams:
            self.log.append(f"pour: the cup is full at {CAPACITY_G:.0f} g")
        yield 0.3

    def _a_sip(self, grams: float, hold: float):
        self._require_cup()
        self.lifted = True
        yield hold
        self.liquid_g = max(0.0, self.liquid_g - grams)
        self.lifted = False
        self.wobble(self._now, 0.6, 18)        # clunk
        yield 0.6

    def _a_straw(self, grams: float, seconds: float):
        self._require_cup(on_coaster=True)
        start = self.liquid_g
        target = max(0.0, start - grams)
        steps = max(1, int(seconds * SAMPLE_HZ))
        for i in range(1, steps + 1):
            self.liquid_g = start + (target - start) * i / steps
            self.wobble(self._now, 0.2, 2)
            yield 1.0 / SAMPLE_HZ

    def _a_refill(self, grams: float, away: float):
        self._require_cup()
        self.lifted = True                      # carried to the kitchen…
        yield away
        self.liquid_g = min(CAPACITY_G, grams)
        self.lifted = False                     # …and back
        self.wobble(self._now, 0.8, 25)
        yield 0.8

    def _a_set(self, grams: float):
        self._require_cup()
        self.liquid_g = min(CAPACITY_G, grams)
        yield 0.1

    def _a_weight(self, grams: float):
        self.extra_g = grams
        self.wobble(self._now, 0.4, 5)
        yield 0.4

    def _a_fault(self, on: bool):
        self.fault = on
        yield 0.1

    def _a_wait(self, seconds: float):
        yield seconds

    # ======================================================= command line ===
    def command(self, argv: list[str] | str) -> dict:
        """Parse and queue a command. Returns {"message", "seconds"}; raises SimError."""
        if isinstance(argv, str):
            argv = shlex.split(argv)
        if not argv:
            raise SimError("no command given — try `help`")
        name, args = argv[0].lower(), argv[1:]
        name = ALIASES.get(name, name)
        spec = COMMANDS.get(name)
        if spec is None:
            raise SimError(f"unknown command '{name}' — try `help`")
        if len(args) > len(spec.params):
            raise SimError(f"too many arguments. Usage: {spec.usage()}")
        values = []
        for i, (pname, default, lo, hi) in enumerate(spec.params):
            if i < len(args):
                values.append(_parse_param(pname, args[i], lo, hi, spec))
            elif default is REQUIRED:
                raise SimError(f"missing {pname}. Usage: {spec.usage()}")
            else:
                values.append(default)
        return spec.run(self, *values)

    def enqueue(self, name: str, action: Action, seconds: float, message: str) -> dict:
        self._queue.append((name, action))
        ahead = sum(1 for _ in self.queued()) - 1
        if ahead > 0:
            message += f" (queued behind {ahead} action{'s' if ahead > 1 else ''})"
        return {"message": message, "seconds": round(seconds, 1)}

    def check_now(self, need_cup: bool = False, on_coaster: bool = False) -> None:
        """Fail fast at the prompt when nothing is queued that could change the answer."""
        if self.busy:
            return
        if need_cup and not self.cup_present:
            raise SimError("there's no cup on the coaster — run `place` first")
        if on_coaster and self.lifted:
            raise SimError("the cup is in your hand — run `down` first")


# ================================================================ commands ===
REQUIRED = object()


class _Spec:
    def __init__(self, name, params, run, help_text, example=""):
        self.name, self.params, self.run, self.help, self.example = name, params, run, help_text, example

    def usage(self) -> str:
        parts = [self.name]
        for pname, default, *_ in self.params:
            parts.append(f"<{pname}>" if default is REQUIRED else f"[{pname}]")
        return " ".join(parts)


def _parse_param(pname, text, lo, hi, spec):
    if pname in ("on|off",):
        if text.lower() in ("on", "true", "1", "yes"):
            return True
        if text.lower() in ("off", "false", "0", "no"):
            return False
        raise SimError(f"expected on or off. Usage: {spec.usage()}")
    try:
        v = float(text.rstrip("gs"))
    except ValueError:
        raise SimError(f"'{text}' isn't a number. Usage: {spec.usage()}") from None
    if not lo <= v <= hi:
        raise SimError(f"{pname} must be between {lo:g} and {hi:g}")
    return v


def _place(sim, cup_g):
    msg = f"New empty {cup_g:.0f} g cup placed on the coaster" if cup_g is not None \
        else f"Cup put back on the coaster ({sim.liquid_g:.0f} g of drink in it)"
    return sim.enqueue("place", sim._a_place(cup_g), 0.6, msg)


def _remove(sim):
    sim.check_now(need_cup=True)
    return sim.enqueue("remove", sim._a_remove(), 0.1,
                       "Cup taken away (shows 'lifted', then 'no cup' after the grace period)")


def _lift(sim):
    sim.check_now(need_cup=True)
    return sim.enqueue("lift", sim._a_lift(), 0.1, "Cup picked up — it's in your hand")


def _down(sim):
    sim.check_now(need_cup=True)
    return sim.enqueue("down", sim._a_down(), 0.6, "Cup set back down")


def _pour(sim, grams, seconds):
    sim.check_now(need_cup=True, on_coaster=True)
    return sim.enqueue("pour", sim._a_pour(grams, seconds), seconds + 0.3,
                       f"Pouring {grams:.0f} g over {seconds:g} s")


def _sip(sim, grams, hold):
    sim.check_now(need_cup=True)
    return sim.enqueue("sip", sim._a_sip(grams, hold), hold + 0.6,
                       f"Lifting the cup, drinking {grams:.0f} g, putting it back")


def _straw(sim, grams, seconds):
    sim.check_now(need_cup=True, on_coaster=True)
    return sim.enqueue("straw", sim._a_straw(grams, seconds), seconds,
                       f"Drinking {grams:.0f} g through a straw over {seconds:g} s")


def _drain(sim, seconds):
    sim.check_now(need_cup=True, on_coaster=True)
    return sim.enqueue("drain", sim._a_straw(CAPACITY_G, seconds), seconds,
                       f"Finishing the drink through a straw over {seconds:g} s")


def _refill(sim, grams, away):
    sim.check_now(need_cup=True)
    return sim.enqueue("refill", sim._a_refill(grams, away), away + 0.8,
                       f"Carrying the cup away and back with {grams:.0f} g of drink")


def _set(sim, grams):
    sim.check_now(need_cup=True)
    return sim.enqueue("set", sim._a_set(grams), 0.1, f"Drink level set to {grams:.0f} g instantly")


def _weight(sim, grams):
    msg = (f"Calibration weight of {grams:.0f} g placed on the coaster" if grams
           else "Calibration weight removed")
    return sim.enqueue("weight", sim._a_weight(grams), 0.4, msg)


def _fault(sim, on):
    return sim.enqueue("fault", sim._a_fault(on), 0.1,
                       "Simulating an unplugged load cell" if on else "Load cell back to normal")


def _noise(sim, grams):
    sim.noise_g = grams
    return {"message": f"Sensor noise set to ±{grams:g} g", "seconds": 0}


def _wait(sim, seconds):
    return sim.enqueue("wait", sim._a_wait(seconds), seconds, f"Waiting {seconds:g} s")


def _stop(sim):
    n = len(sim.queued())
    sim.stop()
    return {"message": f"Cancelled {n} queued action{'s' if n != 1 else ''}", "seconds": 0}


def _demo(sim, sip_g):
    """A ~1-minute show: fresh pour, sips down through low to empty, then a refill."""
    total = 0.0

    def q(name, action, secs):
        nonlocal total
        sim._queue.append((name, action))
        total += secs

    sim.stop()
    q("place", sim._a_place(DEFAULT_CUP_G), 0.6)
    q("wait", sim._a_wait(2.5), 2.5)
    q("pour", sim._a_pour(DEFAULT_FILL_G, 3.0), 3.3)
    q("wait", sim._a_wait(3.0), 3.0)
    sips = math.ceil(DEFAULT_FILL_G / sip_g)
    for _ in range(sips):
        q("sip", sim._a_sip(sip_g, 2.0), 2.6)
        q("wait", sim._a_wait(2.5), 2.5)
    q("wait", sim._a_wait(6.0), 6.0)
    q("refill", sim._a_refill(DEFAULT_FILL_G, 5.0), 5.8)
    return {"message": f"Demo started: pour, {sips} sips of {sip_g:.0f} g until empty, then a refill",
            "seconds": round(total, 1)}


def _help(sim):
    return {"message": help_text(), "seconds": 0}


_g = lambda name, default, hi=CAPACITY_G: (name, default, 0.0, hi)          # noqa: E731
_s = lambda name, default, hi=600.0: (name, default, 0.1, hi)               # noqa: E731

COMMANDS: dict[str, _Spec] = {s.name: s for s in [
    _Spec("place", [("cup_g", None, 1.0, 2000.0)], _place,
          "Put the cup back on the coaster. With a weight, start over with a new empty cup.", "place 250"),
    _Spec("remove", [], _remove, "Take the cup away entirely."),
    _Spec("lift", [], _lift, "Pick the cup up and hold it."),
    _Spec("down", [], _down, "Set the held cup back down."),
    _Spec("pour", [_g("grams", 300.0), _s("seconds", 3.0)], _pour,
          "Pour drink into the cup while it sits on the coaster.", "pour 250"),
    _Spec("sip", [_g("grams", 30.0), _s("hold_s", 2.0, 120.0)], _sip,
          "Pick up, drink, put back down.", "sip 40"),
    _Spec("straw", [_g("grams", 50.0), _s("seconds", 5.0)], _straw,
          "Drink without lifting the cup (through a straw).", "straw 100 8"),
    _Spec("drain", [_s("seconds", 6.0)], _drain, "Finish the whole drink through a straw."),
    _Spec("refill", [_g("grams", DEFAULT_FILL_G), _s("away_s", 4.0, 600.0)], _refill,
          "Carry the cup away, come back with it filled.", "refill 400"),
    _Spec("set", [_g("grams", REQUIRED)], _set, "Teleport the drink level (no animation).", "set 80"),
    _Spec("weight", [("grams", REQUIRED, 0.0, 5000.0)], _weight,
          "Put a calibration object on the coaster (0 removes it).", "weight 500"),
    _Spec("fault", [("on|off", True, 0, 0)], _fault, "Simulate an unplugged / broken load cell."),
    _Spec("noise", [("grams", REQUIRED, 0.0, 20.0)], _noise, "Sensor noise level (default 0.4 g)."),
    _Spec("wait", [_s("seconds", REQUIRED, 3600.0)], _wait, "Queue a pause between actions."),
    _Spec("demo", [("sip_g", 50.0, 5.0, 300.0)], _demo,
          "Run a ~1 minute show: pour → sips → low → empty → refill."),
    _Spec("stop", [], _stop, "Cancel everything queued."),
    _Spec("help", [], _help, "This list."),
]}

ALIASES = {"put": "down", "putdown": "down", "pickup": "lift", "drink": "sip", "fill": "pour",
           "reset": "stop", "?": "help"}


def help_text() -> str:
    lines = ["Coaster simulator commands:"]
    for s in COMMANDS.values():
        lines.append(f"  {s.usage():<24} {s.help}")
    lines += ["",
              "Board commands:",
              "  status                   Show the coaster and what the monitor thinks.",
              "  mode <sim|real|auto>     Use the simulator, the real load cell, or pick automatically.",
              "",
              "Grams of drink ≈ millilitres. Actions play out in real time and queue up."]
    return "\n".join(lines)

