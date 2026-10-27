"""Unit tests for the refill-detection logic. Run from the project root:

    python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "tools"))

from drink_monitor import (  # noqa: E402
    HX711_SATURATED, CalibrationError, DrinkMonitor, DrinkState, Settings, Status,
)

OFFSET = 50_000
CPG = 400.0
HZ = 10


class Rig:
    """A monitor that is already calibrated, plus a clock."""

    def __init__(self, cup=300.0, **overrides):
        s = Settings(tare_offset=OFFSET, counts_per_gram=CPG, cup_weight_g=cup, **overrides)
        self.mon = DrinkMonitor(s)
        self.t = 1000.0
        self.events = []

    def hold(self, grams, seconds):
        for _ in range(int(seconds * HZ)):
            self.events += self.mon.update(OFFSET + grams * CPG, self.t)
            self.events += self.mon.check_timeouts(self.t)
            self.t += 1 / HZ
        return self.mon.state

    def types(self):
        return [e["type"] for e in self.events if e["type"] != "state"]


class TestCalibration(unittest.TestCase):
    def test_tare_calibrate_and_cup(self):
        mon = DrinkMonitor()
        t = 0.0
        feed = lambda g, n=20: [mon.update(1234 + g * 512.0, t + i / HZ) for i in range(n)]  # noqa: E731

        feed(0)
        self.assertEqual(mon.state, DrinkState.SETUP)
        mon.tare()
        self.assertAlmostEqual(mon.settings.tare_offset, 1234, places=3)

        feed(250)
        mon.calibrate_known_weight(250)
        self.assertAlmostEqual(mon.settings.counts_per_gram, 512.0, places=3)

        t = 10.0
        feed(180)
        self.assertAlmostEqual(mon.set_empty_cup(), 180.0, places=1)
        self.assertTrue(mon.settings.is_cup_calibrated())

    def test_reversed_load_cell_still_works(self):
        mon = DrinkMonitor(Settings(tare_offset=0))
        for i in range(20):
            mon.update(-300 * 100, i / HZ)          # counts go DOWN with weight
        mon.calibrate_known_weight(100)
        self.assertLess(mon.settings.counts_per_gram, 0)

    def test_calibrate_without_tare_fails(self):
        mon = DrinkMonitor()
        for i in range(10):
            mon.update(1000, i / HZ)
        with self.assertRaises(CalibrationError):
            mon.calibrate_known_weight(100)

    def test_calibrate_with_nothing_on_scale_fails(self):
        mon = DrinkMonitor(Settings(tare_offset=1000))
        for i in range(10):
            mon.update(1000, i / HZ)
        with self.assertRaises(CalibrationError):
            mon.calibrate_known_weight(500)

    def test_settings_roundtrip_ignores_unknown_keys(self):
        s = Settings(cup_weight_g=123, low_percent=30)
        d = s.to_dict()
        d["from_a_future_version"] = True
        self.assertEqual(Settings.from_dict(d), s)


class TestDetection(unittest.TestCase):
    def test_full_cup_is_ok(self):
        r = Rig()
        self.assertEqual(r.hold(300 + 400, 3), DrinkState.OK)
        self.assertEqual(r.mon.status().percent, 100)

    def test_drinking_down_goes_low_then_empty(self):
        r = Rig()
        r.hold(700, 3)
        r.hold(300 + 150, 3)
        self.assertEqual(r.mon.state, DrinkState.OK)
        r.hold(300 + 90, 3)                  # 22.5 % of 400
        self.assertEqual(r.mon.state, DrinkState.LOW)
        r.hold(300 + 5, 3)
        self.assertEqual(r.mon.state, DrinkState.EMPTY)
        self.assertEqual(r.types().count("low"), 1)
        self.assertEqual(r.types().count("empty"), 1)

    def test_lifting_the_cup_does_not_alarm(self):
        r = Rig()
        r.hold(700, 3)
        self.assertEqual(r.hold(0, 5), DrinkState.LIFTED)
        r.hold(650, 3)
        self.assertEqual(r.mon.state, DrinkState.OK)
        self.assertNotIn("empty", r.types())
        self.assertNotIn("cup_removed", r.types())

    def test_cup_gone_past_grace_becomes_no_cup(self):
        r = Rig(lift_grace_s=10)
        r.hold(700, 3)
        r.hold(0, 12)
        self.assertEqual(r.mon.state, DrinkState.NO_CUP)
        self.assertIn("cup_removed", r.types())

    def test_grace_expires_even_without_new_samples(self):
        r = Rig(lift_grace_s=3)
        r.hold(700, 3)
        r.hold(0, 1)
        r.mon.check_timeouts(r.t + 3.5)   # < 5 s, so not yet a sensor fault
        self.assertEqual(r.mon.state, DrinkState.NO_CUP)

    def test_refill_on_coaster_resets_full_level(self):
        r = Rig()
        r.hold(700, 3)
        r.hold(310, 3)
        self.assertEqual(r.mon.state, DrinkState.EMPTY)
        r.hold(300 + 250, 3)                  # poured 240 g
        self.assertEqual(r.mon.state, DrinkState.OK)
        self.assertIn("refilled", r.types())
        self.assertEqual(r.mon.status().full_liquid_g, 250)
        self.assertEqual(r.mon.status().percent, 100)

    def test_refill_away_from_coaster_is_detected(self):
        r = Rig()
        r.hold(700, 3)
        r.hold(305, 3)
        r.hold(0, 20)                         # walked to the kitchen
        r.hold(680, 3)
        self.assertEqual(r.mon.state, DrinkState.OK)
        self.assertIn("refilled", r.types())

    def test_empty_cup_placed_does_not_alert(self):
        r = Rig()
        r.hold(0, 2)
        r.hold(302, 3)
        self.assertEqual(r.mon.state, DrinkState.EMPTY)
        self.assertNotIn("empty", r.types())

    def test_noise_near_threshold_does_not_flicker(self):
        r = Rig()
        r.hold(700, 3)
        for i in range(40):                   # hover right on the 25 % line
            r.hold(300 + 100 + (2 if i % 2 else -2), 2)
        changes = [e for e in r.events if e["type"] == "state"]
        self.assertLessEqual(len(changes), 2)  # OK -> LOW once, no bouncing

    def test_wobble_while_pouring_keeps_previous_state(self):
        r = Rig()
        r.hold(700, 3)
        for i in range(20):
            r.mon.update(OFFSET + (500 + (i % 2) * 80) * CPG, r.t)
            r.t += 0.1
        self.assertEqual(r.mon.state, DrinkState.OK)

    def test_reminders_repeat_and_snooze(self):
        r = Rig(reminder_minutes=1)
        r.hold(700, 3)
        r.hold(300, 3)
        r.hold(300, 130)
        self.assertEqual(r.types().count("reminder"), 2)
        r.mon.snooze()
        r.hold(300, 70)
        self.assertEqual(r.types().count("reminder"), 2)
        self.assertTrue(r.mon.status().snoozed)

    def test_saturated_adc_is_a_fault_and_recovers(self):
        r = Rig()
        r.hold(700, 3)
        events = r.mon.update(HX711_SATURATED + 5, r.t)
        self.assertEqual(r.mon.state, DrinkState.FAULT)
        self.assertIn("fault", [e["type"] for e in events])
        r.hold(700, 3)
        self.assertEqual(r.mon.state, DrinkState.OK)
        self.assertIn("sensor_ok", r.types())

    def test_no_samples_for_a_while_is_a_fault(self):
        r = Rig()
        r.hold(700, 3)
        r.mon.check_timeouts(r.t + 6)
        self.assertEqual(r.mon.state, DrinkState.FAULT)

    def test_light_paper_cup_counts_as_present(self):
        r = Rig(cup=12.0)
        r.hold(12 + 300, 3)
        self.assertEqual(r.mon.state, DrinkState.OK)
        r.hold(12 + 2, 3)
        self.assertEqual(r.mon.state, DrinkState.EMPTY)

    def test_status_serialises(self):
        r = Rig()
        r.hold(700, 3)
        d = r.mon.status().to_dict()
        self.assertEqual(d["state"], "ok")
        self.assertIsInstance(r.mon.status(), Status)
        import json
        json.dumps(d)


class TestSimulation(unittest.TestCase):
    def test_full_session_story(self):
        import simulate
        kinds = [ev["type"] for _, ev in simulate.run()]
        self.assertEqual(kinds, ["setup_done", "refilled", "low", "empty", "reminder", "refilled", "cup_removed", "total_drunk"])


if __name__ == "__main__":
    unittest.main()
