"""Unit tests for the pure signal logic in market_alerts.py.

No network or credentials required: market_alerts imports cleanly (yfinance is loaded lazily,
only inside fetch functions). Run with:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import market_alerts as m  # noqa: E402

BANDS = m.resolve_bands({"buy": [-0.10, -0.18, -0.28], "sell": [0.12, 0.18],
                         "buy_rearm": -0.07, "sell_rearm": 0.08})
ROW_FIELDS = "glabel is_us snap name m side tier fire dmv crit eod nudge"


def row(name, mdict, **kw):
    base = dict(glabel="g", is_us=False, snap=True, name=name, m=mdict, side=None,
                tier=0, fire=None, dmv=False, crit=False, eod=False, nudge="")
    base.update(kw)
    return m.Row(**base)


class TestClassify(unittest.TestCase):
    def test_buy_tiers(self):
        self.assertEqual(m.classify(None, -0.11, BANDS), ("buy", 1))
        self.assertEqual(m.classify(None, -0.20, BANDS), ("buy", 2))
        self.assertEqual(m.classify(None, -0.40, BANDS), ("buy", 3))

    def test_sell_tiers(self):
        self.assertEqual(m.classify(0.13, None, BANDS), ("sell", 1))
        self.assertEqual(m.classify(0.25, None, BANDS), ("sell", 2))

    def test_neutral_and_buy_priority(self):
        self.assertEqual(m.classify(0.05, -0.02, BANDS), (None, 0))
        # a drawdown deep enough to buy wins even if the DMA distance is also stretched
        self.assertEqual(m.classify(0.25, -0.30, BANDS), ("buy", 3))


class TestResolveBands(unittest.TestCase):
    def test_wide_loosens_only_sell(self):
        narrow = m.resolve_bands({})
        wide = m.resolve_bands({"wide": True})
        self.assertEqual(narrow["buy"], wide["buy"])          # buy side never loosened
        self.assertGreater(wide["sell"][0], narrow["sell"][0])  # sell froth band wider

    def test_per_instrument_override(self):
        b = m.resolve_bands({"buy": [-0.05], "sell": [0.5]})
        self.assertEqual(b["buy"], [-0.05])
        self.assertEqual(b["sell"], [0.5])


class TestStateMachine(unittest.TestCase):
    def test_enter_escalate_hold_recover(self):
        st = {}
        self.assertEqual(m.step_state("X", "buy", 1, None, -0.11, BANDS, st), "enter")
        # same tier -> no re-fire
        self.assertIsNone(m.step_state("X", "buy", 1, None, -0.12, BANDS, st))
        # deeper tier -> escalate
        self.assertEqual(m.step_state("X", "buy", 2, None, -0.20, BANDS, st), "escalate")
        # still in zone, not past re-arm -> hold (no recover)
        self.assertIsNone(m.step_state("X", None, 0, None, -0.09, BANDS, st))
        # recovered past buy_rearm (-0.07) -> recover, state resets
        self.assertEqual(m.step_state("X", None, 0, None, -0.05, BANDS, st), "recover")
        self.assertEqual(st["X"], {"side": None, "tier": 0})

    def test_flip_sides(self):
        st = {}
        m.step_state("Y", "sell", 1, 0.13, None, BANDS, st)
        self.assertEqual(m.step_state("Y", "buy", 1, None, -0.11, BANDS, st), "flip")


class TestDailyAndInfo(unittest.TestCase):
    def test_daily_move_once_per_side(self):
        ds = {}
        self.assertTrue(m.daily_move_alert("X", -0.03, 0.02, None, 0, ds, "2026-06-09"))
        self.assertFalse(m.daily_move_alert("X", -0.03, 0.02, None, 0, ds, "2026-06-09"))
        # below threshold
        self.assertFalse(m.daily_move_alert("Z", -0.01, 0.02, None, 0, {}, "2026-06-09"))
        # suppressed when already deep in a buy tier and falling further
        self.assertFalse(m.daily_move_alert("X", -0.03, 0.02, "buy", 2, {}, "2026-06-09"))

    def test_info_asymmetric_once_per_side(self):
        ds = {}
        self.assertTrue(m.info_move_alert("X", -m.INFO_DOWN, ds, "2026-06-09"))
        self.assertFalse(m.info_move_alert("X", -m.INFO_DOWN, ds, "2026-06-09"))  # repeat same side
        # asymmetry: the up band is stricter than the down band
        self.assertGreater(m.INFO_UP, m.INFO_DOWN)
        self.assertFalse(m.info_move_alert("Y", 0.022, {}, "2026-06-09"))  # +2.2% < INFO_UP
        self.assertTrue(m.info_move_alert("Y", m.INFO_UP, {}, "2026-06-09"))


class TestOvernight(unittest.TestCase):
    def test_late_big_threshold(self):
        self.assertTrue(m._late_big(-m.LATE_DOWN))
        self.assertTrue(m._late_big(m.LATE_UP))
        self.assertFalse(m._late_big(-0.01))
        # asymmetric: a smaller down move trips before an up move of the same size
        self.assertLess(m.LATE_DOWN, m.LATE_UP)

    def test_big_move_fires_once_per_day(self):
        st = {}
        self.assertTrue(m.big_move("QQQ", -0.05, st, "2026-06-09"))
        self.assertFalse(m.big_move("QQQ", -0.05, st, "2026-06-09"))
        self.assertTrue(m.big_move("QQQ", -0.05, st, "2026-06-10"))  # new day re-arms


class TestFillBars(unittest.TestCase):
    def test_mood_bar(self):
        self.assertEqual(len(m._mood_bar(50)), 10)
        self.assertEqual(m._mood_bar(0), "▱" * 10)   # extreme fear -> empty
        self.assertEqual(m._mood_bar(100), "▰" * 10)  # extreme greed -> full

    def test_mood_trend_arrows(self):
        self.assertIn("↗", m._mood_trend(70, 60))   # rising
        self.assertIn("↘", m._mood_trend(50, 60))   # easing
        self.assertEqual(m._mood_trend(50, None), "")

    def test_range_line_bar(self):
        out = m.range_line({"peak": 120, "trough": 80, "px": 100,
                            "drawdown": -0.1667, "runup": 0.25})
        self.assertIn("▰", out)            # has a fill bar
        self.assertIn("below high", out)


class TestCrossAndWeekly(unittest.TestCase):
    def test_golden_then_death_fire_once(self):
        name = next(iter(m.CROSS_NAMES))
        st = {}
        # first observation just records the side (no note)
        self.assertEqual(m.cross_events([row(name, {"d50": 90, "d200": 100})], st), [])
        # 50DMA crosses above 200DMA -> golden cross
        notes = m.cross_events([row(name, {"d50": 110, "d200": 100})], st)
        self.assertTrue(any("Golden cross" in n for n in notes))
        # holding above -> no repeat
        self.assertEqual(m.cross_events([row(name, {"d50": 112, "d200": 100})], st), [])
        # crosses back below -> death cross
        notes = m.cross_events([row(name, {"d50": 95, "d200": 100})], st)
        self.assertTrue(any("Death cross" in n for n in notes))

    def test_weekly_movers_threshold(self):
        r_big = row("Idx", {"wk": -0.10})
        r_small = row("Idx2", {"wk": -0.005})
        movers = m.weekly_movers([r_big, r_small])
        names = [r.name for r, _ in movers]
        self.assertIn("Idx", names)
        self.assertNotIn("Idx2", names)


class TestPollAndHelpers(unittest.TestCase):
    def test_poll_triggers(self):
        for w in ("status", "/status", "digest", "ping"):
            self.assertIn(w, m.POLL_TRIGGERS)
        self.assertIn("help", m.POLL_HELP)

    def test_finnhub_noop_without_token(self):
        # no token configured in the test env -> graceful (None, None), never raises
        if not m.FINNHUB_TOKEN:
            self.assertEqual(m.finnhub_quote("QQQ"), (None, None))

    def test_pct_and_short(self):
        self.assertEqual(m.pct(None), "n/a")
        self.assertEqual(m.pct(0.123), "+12.3%")
        self.assertEqual(m.short("Nasdaq100 (QQQ)"), "Nasdaq100")


if __name__ == "__main__":
    unittest.main(verbosity=2)
