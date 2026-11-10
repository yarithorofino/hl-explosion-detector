#!/usr/bin/env python3
"""Early-signal components and the phase-2 watch. No network."""
import json
import unittest

import early_signals as early
import watch as watchmod
from detector import load_cfg


def flat(n, price=100.0, vol=10.0):
    cs = []
    for i in range(n):
        cs.append({"t": i * 3_600_000, "o": price, "h": price + 0.2, "l": price - 0.2, "c": price, "v": vol})
    return cs


class EarlyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_cfg()

    def test_relative_volume_needs_a_small_price_move(self):
        cs = flat(30)
        cs[-1] = {"t": cs[-1]["t"], "o": 100, "h": 101, "l": 99.5, "c": 100.8, "v": 80}
        got = early.vol_accel(cs, len(cs) - 1, self.cfg, 1000)
        self.assertTrue(got["hit"])
        self.assertLess(got["pct"], 3)
        cs[-1]["c"] = 110
        cs[-1]["h"] = 110
        self.assertFalse(early.vol_accel(cs, len(cs) - 1, self.cfg, 1000)["hit"])

    def test_ttm_squeeze_releases_with_volume(self):
        cs = flat(40, vol=10)
        # A held squeeze (flat prices: bands collapse inside the channel) then a close outside.
        cs[-1] = {"t": cs[-1]["t"], "o": 100, "h": 104, "l": 100, "c": 103.5, "v": 40}
        got = early.squeeze_breakout(cs, len(cs) - 1, self.cfg)
        self.assertTrue(got["hit"])
        self.assertEqual(got["side"], "long")

    def test_open_interest_buildup_is_flat_price_only(self):
        self.assertTrue(early.oi_buildup(110, 100, 100, 100.5, self.cfg)["hit"])
        self.assertFalse(early.oi_buildup(110, 100, 110, 100, self.cfg)["hit"])

    def test_cvd_absorption_and_divergence(self):
        stats = {"buy": 80_000, "sell": 20_000, "cvd": 60_000, "imbalance": 0.6, "notional": 100_000}
        got = early.cvd_absorption(stats, price_pct=0.4, cfg=self.cfg)
        self.assertTrue(got["hit"])
        self.assertEqual(got["side"], "long")
        self.assertFalse(early.cvd_absorption(stats, price_pct=4.0, cfg=self.cfg)["hit"])
        down = {"buy": 20_000, "sell": 80_000, "cvd": -60_000, "imbalance": -0.6, "notional": 100_000}
        self.assertTrue(early.cvd_divergence(down, price_pct=2.0, cfg=self.cfg))

    def _absorption_series(self, spikes=1):
        """48 quiet hours, then `spikes` narrow 8× bars separated by quiet hours."""
        cs = flat(48, price=100.0, vol=10.0)
        for n in range(spikes):
            cs.append({
                "t": (len(cs)) * 3_600_000,
                "o": 100.0, "h": 100.5, "l": 99.9, "c": 100.4, "v": 200.0,
            })
            if n + 1 < spikes:
                for _ in range(6):
                    cs.extend(flat(1, price=100.0, vol=10.0))
                    cs[-1]["t"] = len(cs) * 3_600_000
        return cs

    def test_absorption_needs_a_repeat_and_a_small_range(self):
        one = self._absorption_series(1)
        got = early.absorption(one, len(one) - 1, self.cfg)
        self.assertTrue(got["hit"])
        self.assertFalse(got["repeated"])
        self.assertEqual(got["side"], "long")
        self.assertGreaterEqual(got["rvol"], 8)
        alone = early.confluence({"absorption": True}, self.cfg, side="long")
        self.assertFalse(alone["hit"])
        two = self._absorption_series(2)
        got = early.absorption(two, len(two) - 1, self.cfg)
        self.assertTrue(got["repeated"])
        self.assertGreaterEqual(got["repeats"], 2)
        alert = early.confluence({"absorption": True, "absorption_repeat": True}, self.cfg, side=got["side"])
        self.assertTrue(alert["hit"])
        self.assertIn("absorption", alert["triggers"])
        self.assertNotIn("vol_accel", alert["triggers"])
        wide = self._absorption_series(1)
        wide[-1]["c"] = 108
        wide[-1]["h"] = 108
        self.assertFalse(early.absorption(wide, len(wide) - 1, self.cfg)["hit"])
        # 8× the median of a lumpy day can be an ordinary busy hour. The bar
        # also has to clear 8× the average, which is what relative volume uses.
        lumpy = flat(40, vol=1.0) + flat(8, vol=100.0)
        for i, bar in enumerate(lumpy):
            bar["t"] = i * 3_600_000
        lumpy.append({"t": len(lumpy) * 3_600_000, "o": 100.0, "h": 100.4, "l": 99.8, "c": 100.3, "v": 12.0})
        self.assertFalse(early.absorption(lumpy, len(lumpy) - 1, self.cfg)["hit"])

    def test_delta_overrides_close_location(self):
        cs = self._absorption_series(1)
        got = early.absorption(cs, len(cs) - 1, self.cfg, delta_side="short")
        self.assertEqual(got["side"], "short")

    def test_one_component_does_not_alert(self):
        parts = {"vol_accel": True}
        out = early.confluence(parts, self.cfg, side="long")
        self.assertFalse(out["hit"])
        parts["squeeze"] = True
        out = early.confluence(parts, self.cfg, side="long")
        self.assertTrue(out["hit"])
        self.assertGreaterEqual(out["score"], self.cfg["early_score_min"])
        self.assertIn("vol_accel", out["triggers"])
        self.assertIn("squeeze_breakout", out["triggers"])

    def test_taker_sign_matches_hyperliquid_sides(self):
        stats = early.cvd_from_trades([
            {"side": "B", "px": "10", "sz": "3"},
            {"side": "A", "px": "10", "sz": "1"},
        ])
        self.assertEqual(stats["cvd"], 20)
        self.assertGreater(stats["imbalance"], 0)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_cfg()

    def _rally(self):
        # 100 -> 110 impulse. Zone is the 38.2–50% pullback.
        return [
            {"o": 100, "h": 104, "l": 99, "c": 103, "v": 10},
            {"o": 103, "h": 108, "l": 102, "c": 107, "v": 10},
            {"o": 107, "h": 112, "l": 106, "c": 111, "v": 10},
        ]

    def test_retest_with_enough_payoff_becomes_entry_ready_once(self):
        w = watchmod.build_watch("INIT", "main", "long", "1h_legacy", self._rally(), now=1_000, cfg=self.cfg)
        self.assertIsNotNone(w)
        span = w["impulse_high"] - w["impulse_low"]
        self.assertAlmostEqual(w["tp1"], w["impulse_high"] + span)
        # A pullback into the zone, stop untouched.
        mid = (w["entry_low"] + w["entry_high"]) / 2
        w, event = watchmod.advance_watch(w, high=mid + 0.2, low=mid - 0.2, close=mid, now=2_000, cfg=self.cfg)
        self.assertEqual(event, "entry_ready")
        self.assertGreaterEqual(w["rr"], 2)
        payload_side = w["side"]
        w, again = watchmod.advance_watch(w, high=mid, low=mid, close=mid, now=3_000, cfg=self.cfg)
        self.assertIsNone(again)
        self.assertEqual(payload_side, "long")

    def test_stop_invalidates_and_expiry_closes(self):
        w = watchmod.build_watch("INIT", "main", "long", "3h_escalera", self._rally(), now=1_000, cfg=self.cfg)
        w, event = watchmod.advance_watch(
            w, high=w["entry_high"], low=w["sl"] - 0.1, close=w["sl"] - 0.1, now=2_000, cfg=self.cfg,
        )
        self.assertEqual(event, "invalidated")
        w = watchmod.build_watch("VVV", "main", "short", "1h_legacy", [
            {"o": 110, "h": 111, "l": 104, "c": 105, "v": 1},
            {"o": 105, "h": 106, "l": 100, "c": 101, "v": 1},
            {"o": 101, "h": 102, "l": 96, "c": 97, "v": 1},
        ], now=1_000, cfg=self.cfg)
        w, event = watchmod.advance_watch(w, high=w["entry_high"], low=w["entry_low"], close=w["entry_low"], now=w["expires_at"] + 1, cfg=self.cfg)
        self.assertEqual(event, "expired")

    def test_quiet_hours_do_not_consume_the_entry(self):
        w = watchmod.build_watch("SOPH", "main", "long", "vol_accel+squeeze_breakout", self._rally(), now=1_000, cfg=self.cfg)
        mid = (w["entry_low"] + w["entry_high"]) / 2
        w, event = watchmod.advance_watch(
            w, high=mid, low=mid, close=mid, now=2_000, cfg=self.cfg, allow_entry=False,
        )
        self.assertEqual(event, "quiet")
        self.assertEqual(w["status"], "open")

    def test_absorption_watch_waits_for_the_breakout(self):
        w = watchmod.build_absorption_watch(
            "SOPH", "main", "long", "absorption", range_low=99.9, range_high=100.5, now=1_000, cfg=self.cfg,
        )
        self.assertEqual(w["urgency"], "baja")
        self.assertAlmostEqual(w["tp1"], 100.5 + 2 * (100.5 - 99.9))
        # Still inside the range: no entry.
        w, event = watchmod.advance_watch(w, high=100.4, low=100.0, close=100.2, now=2_000, cfg=self.cfg)
        self.assertIsNone(event)
        # Close through the high, and the bar traded back to the level so the fill is the edge.
        w, event = watchmod.advance_watch(w, high=100.8, low=100.4, close=100.7, now=3_000, cfg=self.cfg)
        self.assertEqual(event, "entry_ready")
        self.assertGreaterEqual(w["rr"], 2)
        self.assertAlmostEqual(w["fill"], 100.5)
        w, again = watchmod.advance_watch(w, high=101, low=100.6, close=100.9, now=4_000, cfg=self.cfg)
        self.assertIsNone(again)

    def test_absorption_watch_does_not_chase_a_gap_or_a_stop(self):
        w = watchmod.build_absorption_watch(
            "SOPH", "main", "long", "absorption", range_low=99.9, range_high=100.5, now=1_000, cfg=self.cfg,
        )
        # Gap well past the edge: R:R at the near side of the bar is under 2, watch stays open.
        w, event = watchmod.advance_watch(w, high=103.0, low=102.0, close=102.5, now=2_000, cfg=self.cfg)
        self.assertIsNone(event)
        self.assertEqual(w["status"], "open")
        # A wick through the far side that closes back inside is a spring, not a stop.
        w, event = watchmod.advance_watch(
            w, high=100.3, low=w["sl"] - 0.2, close=100.1, now=2_500, cfg=self.cfg,
        )
        self.assertIsNone(event)
        self.assertEqual(w["status"], "open")
        w, event = watchmod.advance_watch(
            w, high=100.6, low=w["sl"] - 0.01, close=99.8, now=3_000, cfg=self.cfg,
        )
        self.assertEqual(event, "invalidated")

    def test_forward_outcome_prefers_the_stop_on_a_wide_bar(self):
        w = watchmod.build_watch("INIT", "main", "long", "1h_legacy", self._rally(), now=1_000, cfg=self.cfg)
        w["status"] = "entry_sent"
        w["fill"] = w["entry_low"]
        hit = watchmod.outcome_after_entry(w, [{"h": w["tp1"] + 1, "l": w["sl"] - 1, "c": w["sl"]}])
        self.assertEqual(hit, "sl")


class ShadowReportTests(unittest.TestCase):
    def test_lead_time_and_false_alarm(self):
        import shadow_report

        hit = {"ts": 1_000_000, "coin": "SOPH", "side": "long", "trigger": "absorption+vol_accel", "price": 100.0}
        paid = {"SOPH": [(1_000_000 + 3 * 3600, 105.0)]}
        per = shadow_report.summarise([hit], paid)
        self.assertEqual(len(per["absorption"]["leads"]), 1)
        self.assertEqual(len(per["vol_accel"]["leads"]), 1)
        self.assertAlmostEqual(per["absorption"]["leads"][0], 3.0)
        failed = {"SOPH": [(1_000_000 + 3600, 95.0)]}
        per = shadow_report.summarise([hit], failed)
        self.assertEqual(per["absorption"]["false"], 1)
        per = shadow_report.summarise([hit], {})
        self.assertEqual(per["absorption"]["no_path"], 1)


if __name__ == "__main__":
    unittest.main()
