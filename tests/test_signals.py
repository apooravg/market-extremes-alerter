"""Unit + smoke tests for the signal engine. No network or credentials required —
the module imports cleanly (yfinance is imported lazily, only at fetch time)."""
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "market_alerts", os.path.join(_HERE, "..", "market_alerts.py"))
ma = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ma)


def _row(name, **m):
    base = dict(px=100, d50=98, d100=97, d200=95, peak=110, trough=80,
                dist50=0.0, dist100=0.0, dist200=0.0, drawdown=-0.05, runup=0.2,
                daily=0.0, wk=None, sref="d50", blabel=None)
    base.update(m)
    return ma.Row("g", False, True, name, base, "", 0, None, False, False, False, "")


class TestConfig(unittest.TestCase):
    def test_instruments_present(self):
        self.assertIn("SENSEX", ma.INDIA_IDX)
        self.assertIn("Nasdaq100 (QQQ)", ma.US_IDX)


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.b = ma.resolve_bands(ma.INDIA_IDX["SENSEX"])  # buy [-15/-22/-30], sell [12/18]

    def test_deep_dip_is_buy(self):
        self.assertEqual(ma.classify(0.0, -0.16, self.b), ("buy", 1))
        self.assertEqual(ma.classify(0.0, -0.31, self.b), ("buy", 3))

    def test_froth_is_sell(self):
        self.assertEqual(ma.classify(0.30, 0.0, self.b), ("sell", 2))

    def test_quiet_is_none(self):
        self.assertEqual(ma.classify(0.05, -0.05, self.b), (None, 0))


class TestStateMachine(unittest.TestCase):
    def test_escalate_once_then_recover(self):
        b = ma.resolve_bands(ma.INDIA_IDX["Nifty Smallcap250"])  # sell [8/11/14], rearm 5
        st = {}
        self.assertEqual(ma.step_state("X", "sell", 1, 0.09, -0.05, b, st), "enter")
        self.assertIsNone(ma.step_state("X", "sell", 1, 0.09, -0.05, b, st))   # no repeat at tier
        self.assertEqual(ma.step_state("X", "sell", 2, 0.12, -0.05, b, st), "escalate")
        # falls back below the re-arm level -> resets with a "recover"
        self.assertEqual(ma.step_state("X", None, 0, 0.03, -0.05, b, st), "recover")


class TestRender(unittest.TestCase):
    def test_mood_bar_endpoints(self):
        self.assertEqual(ma._mood_bar(0), "▱" * 10)
        self.assertEqual(ma._mood_bar(100), "▰" * 10)
        self.assertEqual(ma._mood_bar(50).count("▰"), 5)

    def test_range_line_position(self):
        m = dict(px=104, peak=110, trough=80, drawdown=104 / 110 - 1, runup=104 / 80 - 1)
        out = ma.range_line(m)
        self.assertEqual(out.count("▰"), 8)   # 80% of the 52-week range
        self.assertIn("above low", out)
        self.assertIn("below high", out)


class TestCross(unittest.TestCase):
    def test_golden_then_death_fire_once(self):
        st = {}
        self.assertEqual(ma.cross_events([_row("SENSEX", d50=95, d200=100)], st), [])  # baseline
        self.assertTrue(any("Golden" in n for n in
                            ma.cross_events([_row("SENSEX", d50=101, d200=100)], st)))
        self.assertEqual(ma.cross_events([_row("SENSEX", d50=102, d200=100)], st), [])  # no repeat
        self.assertTrue(any("Death" in n for n in
                            ma.cross_events([_row("SENSEX", d50=99, d200=100)], st)))


class TestWeekly(unittest.TestCase):
    def test_slow_bleed_fires(self):
        self.assertTrue(ma.weekly_movers([_row("Nifty Smallcap250", wk=-0.034)]))
        self.assertFalse(ma.weekly_movers([_row("Nifty Smallcap250", wk=-0.020)]))


if __name__ == "__main__":
    unittest.main()
