"""Tests for the virtual coaster, run through the real detection logic.

    python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from drink_monitor import DrinkMonitor, DrinkState, Settings  # noqa: E402
from sim_coaster import (DEFAULT_CUP_G, SIM_COUNTS_PER_GRAM, SIM_OFFSET, SimCoaster,  # noqa: E402
                         SimError)


class Rig:
    def __init__(self, settings=None):
        self.sim = SimCoaster(seed=42)
        self.mon = DrinkMonitor(settings or Settings(
            tare_offset=SIM_OFFSET, counts_per_gram=SIM_COUNTS_PER_GRAM,
            cup_weight_g=DEFAULT_CUP_G, reminder_minutes=0, lift_grace_s=20))
        self.sim.on_effect = lambda e: self.mon.new_cup(e["cup_g"])
        self.t = 1000.0
        self.events = []
        self.samples = 0

    def run(self, seconds):
        end = self.t + seconds
        while self.t < end:
            self.t += 0.05
            for ts, raw in self.sim.advance(self.t):
                self.samples += 1
                self.events += self.mon.update(raw, ts)
            self.events += self.mon.check_timeouts(self.t)
        return self.mon.state

    def do(self, cmd, extra=2.5):
        r = self.sim.command(cmd)
        self.run(r.get("seconds", 0) + extra)
        return self.mon.state

    def kinds(self):
        return [e["type"] for e in self.events if e["type"] not in ("state", "drank")]


class TestSimCoaster(unittest.TestCase):
    def test_starts_with_a_full_cup(self):
        r = Rig()
        self.assertEqual(r.run(3), DrinkState.OK)
        self.assertAlmostEqual(r.mon.status().liquid_g, 350, delta=2)
        self.assertGreaterEqual(r.samples, 28)          # ~10 Hz

    def test_demo_story(self):
        r = Rig()
        r.run(3)
        r.do("demo", extra=5)
        self.assertEqual(r.kinds(), ["cup_placed", "refilled", "low", "empty", "refilled"])
        self.assertEqual(r.mon.state, DrinkState.OK)
        self.assertFalse(r.sim.busy)

    def test_sip_lift_and_put_down(self):
        r = Rig()
        r.run(3)
        self.assertEqual(r.do("sip 100"), DrinkState.OK)
        self.assertAlmostEqual(r.mon.status().liquid_g, 250, delta=2)
        r.sim.command("lift")
        self.assertEqual(r.run(1), DrinkState.LIFTED)
        self.assertEqual(r.do("down"), DrinkState.OK)

    def test_straw_drain_and_refill(self):
        r = Rig()
        r.run(3)
        self.assertEqual(r.do("straw 280 4"), DrinkState.LOW)
        self.assertEqual(r.do("drain 3"), DrinkState.EMPTY)
        self.assertEqual(r.do("refill 400 2"), DrinkState.OK)
        self.assertEqual(r.kinds()[-3:], ["low", "empty", "refilled"])

    def test_remove_becomes_no_cup(self):
        r = Rig()
        r.run(3)
        r.do("remove")
        self.assertEqual(r.run(25), DrinkState.NO_CUP)
        self.assertEqual(r.do("place"), DrinkState.OK)

    def test_new_cup_tells_the_monitor(self):
        r = Rig()
        r.run(3)
        r.do("remove")
        r.do("place 180")
        self.assertEqual(r.mon.settings.cup_weight_g, 180)
        self.assertEqual(r.mon.state, DrinkState.EMPTY)
        self.assertNotIn("empty", r.kinds())          # a fresh empty cup isn't an alert
        self.assertEqual(r.do("pour 250 2"), DrinkState.OK)
        self.assertAlmostEqual(r.mon.status().liquid_g, 250, delta=2)

    def test_fault(self):
        r = Rig()
        r.run(3)
        r.sim.command("fault on")
        r.run(7)
        self.assertEqual(r.mon.state, DrinkState.FAULT)
        r.sim.command("fault off")
        self.assertEqual(r.run(4), DrinkState.OK)

    def test_dashboard_calibration_works_on_the_simulator(self):
        r = Rig(Settings())                           # uncalibrated, like a fresh board
        r.do("remove")
        r.mon.tare()
        r.do("weight 500")
        r.mon.calibrate_known_weight(500)
        self.assertAlmostEqual(r.mon.settings.counts_per_gram, SIM_COUNTS_PER_GRAM, delta=3)
        r.do("weight 0")
        r.do("place 220")
        r.mon.set_empty_cup()
        self.assertAlmostEqual(r.mon.settings.cup_weight_g, 220, delta=1)
        self.assertEqual(r.do("pour 300 2"), DrinkState.OK)

    def test_queueing_and_stop(self):
        r = Rig()
        r.sim.command("sip")
        r2 = r.sim.command("sip")
        self.assertIn("queued behind 1", r2["message"])
        self.assertEqual(len(r.sim.queued()), 2)
        r.sim.command("stop")
        self.assertFalse(r.sim.busy)

    def test_bad_commands(self):
        sim = SimCoaster()
        for cmd, needle in [("", "no command"), ("fly", "unknown command"), ("pour lots", "isn't a number"),
                            ("sip 5000", "between"), ("set", "missing grams"), ("fault maybe", "on or off"),
                            ("pour 1 2 3", "too many")]:
            with self.subTest(cmd=cmd):
                with self.assertRaises(SimError) as cm:
                    sim.command(cmd)
                self.assertIn(needle, str(cm.exception))

    def test_impossible_now(self):
        sim = SimCoaster()
        sim.command("remove")
        sim.advance(0)
        sim.advance(1)
        with self.assertRaises(SimError):
            sim.command("pour")          # no cup and nothing queued that would bring one
        sim.command("place")
        sim.command("pour")              # fine: queued behind `place`

    def test_help_lists_everything(self):
        text = SimCoaster().command("help")["message"]
        for word in ("pour", "sip", "refill", "demo", "mode", "status"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
