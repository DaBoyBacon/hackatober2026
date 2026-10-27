#!/usr/bin/env python3
"""Run the refill-detection logic against a simulated coaster — no board needed.

    python3 tools/simulate.py            # prints the timeline of events
    python3 tools/simulate.py --verbose  # also prints a status line every few seconds

Useful for tuning thresholds in python/drink_monitor.py before touching hardware.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from drink_monitor import DrinkMonitor, Settings  # noqa: E402

SAMPLE_HZ = 10              # HX711 with RATE pin low
OFFSET = 84_210             # raw counts with an empty coaster (any value works)
COUNTS_PER_GRAM = 431.7     # typical-ish for a 1 kg bar load cell at gain 128
NOISE_G = 0.6               # sensor noise, grams (1 sigma)


class Coaster:
    """Generates raw HX711 samples for whatever is sitting on the coaster."""

    def __init__(self, seed: int = 7):
        self.t = 0.0
        self.rng = random.Random(seed)

    def raw(self, grams: float) -> float:
        return OFFSET + (grams + self.rng.gauss(0, NOISE_G)) * COUNTS_PER_GRAM


def run(verbose: bool = False) -> list[tuple[float, dict]]:
    settings = Settings(reminder_minutes=2)  # short reminder so the demo shows one
    mon = DrinkMonitor(settings)
    coaster = Coaster()
    timeline: list[tuple[float, dict]] = []
    drank = [0.0]
    dt = 1.0 / SAMPLE_HZ

    def hold(grams: float, seconds: float, wobble: float = 0.0):
        for _ in range(int(seconds * SAMPLE_HZ)):
            g = grams + (coaster.rng.uniform(-wobble, wobble) if wobble else 0.0)
            for ev in mon.update(coaster.raw(g), coaster.t) + mon.check_timeouts(coaster.t):
                if ev["type"] == "drank":
                    drank[0] += ev["grams"]
                if ev["type"] not in ("state", "drank") or verbose:
                    timeline.append((coaster.t, ev))
            if verbose and int(coaster.t * SAMPLE_HZ) % (5 * SAMPLE_HZ) == 0:
                st = mon.status()
                print(f"  t={coaster.t:7.1f}s  {st.state.value:7s}  liquid={st.liquid_g}  pct={st.percent}")
            coaster.t += dt

    def ramp(g0: float, g1: float, seconds: float):
        n = int(seconds * SAMPLE_HZ)
        for i in range(n):
            hold(g0 + (g1 - g0) * i / n + coaster.rng.uniform(-15, 15), dt)

    CUP = 310.0
    # --- setup, as a person would do it in the web UI ---
    hold(0, 2)
    mon.tare()
    hold(500, 2)
    mon.calibrate_known_weight(500)
    hold(0, 1)
    hold(CUP, 2.5)
    mon.set_empty_cup()
    timeline.append((coaster.t, {"type": "setup_done", "cup_g": round(settings.cup_weight_g, 1)}))

    # --- pour 400 g of iced tea while the cup sits on the coaster ---
    ramp(CUP, CUP + 400, 3)
    hold(CUP + 400, 20)

    # --- a sip every ~minute: lift, drink 45 g, set it back down ---
    liquid = 400.0
    while liquid > 0:
        hold(0, 4)                                 # cup in hand
        liquid = max(0.0, liquid - 45)
        hold(CUP + liquid, 0.8, wobble=20)         # clunk
        hold(CUP + liquid, 55)

    hold(CUP, 150)                                 # sits empty -> reminder

    # --- refill at the kitchen: lift for 30 s, come back full ---
    hold(0, 30)
    hold(CUP + 380, 0.8, wobble=25)
    hold(CUP + 380, 10)

    # --- take the cup away for good ---
    hold(0, settings.lift_grace_s + 5)
    timeline.append((coaster.t, {"type": "total_drunk", "grams": round(drank[0], 1)}))
    return timeline


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    for t, ev in run(args.verbose):
        extra = {k: v for k, v in ev.items() if k != "type"}
        print(f"t={t:7.1f}s  {ev['type']:<11} {extra if extra else ''}")


if __name__ == "__main__":
    main()
