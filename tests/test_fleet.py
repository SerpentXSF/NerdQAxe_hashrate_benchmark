"""Unit tests for fleet.py's hardware-free helpers (NerdQAxe health model)."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet


def nq(**kw):
    """A NerdQAxe++ /api/system/info payload (4x BM1370 -> 8160 cores). At 800MHz
    the theoretical hashrate is 800 * 8.16 = 6528 GH/s."""
    base = {"hostname": "NerdQAxe", "frequency": 800, "coreVoltage": 1180,
            "smallCoreCount": 2040, "asicCount": 4, "hashRate": 6360,
            "temp": 65, "vrTemp": 58, "fanspeed": 80, "fanspeed2": 80,
            "fanrpm": 2300, "power": 99, "uptimeSeconds": 3600,
            "duplicateHWNonces": 0}
    base.update(kw)
    return base


class TestEfficiency(unittest.TestCase):
    def test_jth(self):
        self.assertAlmostEqual(fleet.efficiency_jth({"hashRate": 1200, "power": 18}), 15.0)

    def test_zero_hashrate_is_none(self):
        self.assertIsNone(fleet.efficiency_jth({"hashRate": 0, "power": 18}))

    def test_missing_fields_is_none(self):
        self.assertIsNone(fleet.efficiency_jth({}))


class TestHashrateHealth(unittest.TestCase):
    def test_ratio(self):
        self.assertAlmostEqual(fleet.hashrate_health(nq()), 6360 / 6528)

    def test_none_without_cores(self):
        self.assertIsNone(fleet.hashrate_health({"frequency": 800, "hashRate": 6000}))

    def test_fan_pct_uses_busier_fan(self):
        self.assertEqual(fleet.fan_pct(nq(fanspeed=70, fanspeed2=99)), 99)


class TestStatusRow(unittest.TestCase):
    def test_unreachable(self):
        self.assertIn("UNREACHABLE", fleet.status_row("10.0.0.1", None))

    def test_reachable_includes_key_values(self):
        row = fleet.status_row("10.0.0.2", nq())
        self.assertIn("NerdQAxe", row)
        self.assertIn("800/1180", row)
        self.assertIn("80%/2300", row)
        self.assertIn("97%", row)         # hashrate health
        self.assertNotIn("[!", row)       # healthy, nothing flagged

    def test_missing_cores_health_na(self):
        # Without core counts, health can't be computed -> n/a, nothing flagged.
        info = {"hostname": "X", "frequency": 800, "coreVoltage": 1180,
                "hashRate": 6000, "temp": 55, "power": 99, "uptimeSeconds": 600}
        row = fleet.status_row("10.0.0.3", info)
        self.assertIn("n/a", row)
        self.assertNotIn("[!", row)


class TestRowFlags(unittest.TestCase):
    def test_healthy_no_flags(self):
        self.assertEqual(fleet.row_flags(nq()), [])

    def test_pegged_fans_but_cool_not_flagged(self):
        # A PID board can hold both fans high while cool; cool + pegged is normal.
        self.assertEqual(fleet.row_flags(nq(temp=46, fanspeed=100, fanspeed2=100)), [])

    def test_droop_flagged(self):
        # 5800 / 6528 = 0.89 < 0.95 health floor.
        self.assertIn("droop", fleet.row_flags(nq(hashRate=5800)))

    def test_dup_flagged(self):
        self.assertIn("dup", fleet.row_flags(nq(duplicateHWNonces=7)))

    def test_droop_hot_and_cooling(self):
        flags = fleet.row_flags(nq(hashRate=5800, temp=71, fanspeed=100, fanspeed2=100))
        self.assertEqual(flags, ["droop", "temp", "cooling"])


class TestLoadTargets(unittest.TestCase):
    def test_positional_dedupe(self):
        args = types.SimpleNamespace(ips=["a", "b", "a", "c"], file=None)
        self.assertEqual(fleet.load_targets(args), [("a", []), ("b", []), ("c", [])])


class TestDelta(unittest.TestCase):
    def test_before_after(self):
        before = nq(hashRate=6020, frequency=820, coreVoltage=1170)  # 6020/6691 = 90%
        after = nq(hashRate=6360, frequency=800, coreVoltage=1180)   # 6360/6528 = 97%
        d = fleet._delta(before, after)
        self.assertIn("90% -> 97%", d)
        self.assertIn("820/1170 -> 800/1180", d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
