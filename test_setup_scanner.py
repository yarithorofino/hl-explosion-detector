#!/usr/bin/env python3
"""Setup rules: kPEPE veto, a GRAM-style Donchian catch, and taker fees."""
import unittest

import setup_scanner as setups
from detector import load_cfg


def _asset(coin="GRAM", day=5_000_000):
    return {
        "coin": coin, "dex": "main", "dayNtlVlm": day, "oi_usd": 2_000_000,
        "funding_pct": 0.0, "premium_pct": 0.0, "mark": None,
    }


def _bar(i, price, high=None, low=None, vol=10.0):
    high = price if high is None else high
    low = price if low is None else low
    return {
        "t": i * 3_600_000, "o": price, "h": max(high, price), "l": min(low, price),
        "c": price, "v": vol,
    }


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_cfg()

    def test_kpepe_lower_highs_veto_the_retest(self):
        # Price 0.00521 on a prior swing low. TP1 is the swing high at 0.00545.
        # Highs 0.00560, 0.00550, 0.005351 step down, and 0.005351 is under that TP1.
        cs = []
        for i in range(48):
            bucket = i // 4
            lo = 0.00480 + bucket * 0.00003
            px = min(lo + 0.00002, 0.00521)
            cs.append(_bar(i, px, high=px, low=lo, vol=20))
        # Swing high outside the noise. Neighbours stay under it.
        cs[30] = _bar(30, 0.00510, high=0.00545, low=0.00505, vol=20)
        cs[29] = _bar(29, 0.00508, high=0.00512, low=0.00504, vol=20)
        cs[31] = _bar(31, 0.00508, high=0.00512, low=0.00504, vol=20)
        # Support touch in the last 4h bucket. Its low is still above the previous bucket.
        for i in (42, 43, 45, 46):
            cs[i] = _bar(i, 0.00521, high=0.00521, low=0.00521, vol=20)
        cs[44] = _bar(44, 0.00521, high=0.00521, low=0.005205, vol=20)
        clean = [dict(c) for c in cs]
        clean[47] = _bar(47, 0.00521, high=0.00521, low=0.005208, vol=30)
        allowed = setups.support_retest(clean, 47, _asset("kPEPE"), self.cfg)
        self.assertIsNotNone(allowed)
        self.assertEqual(allowed["setup"], "support_retest")
        self.assertEqual(allowed["tp1"], 0.00545)
        cs[45] = _bar(45, 0.00522, high=0.00560, low=0.005215, vol=30)
        cs[46] = _bar(46, 0.00522, high=0.00550, low=0.005215, vol=30)
        cs[47] = _bar(47, 0.00521, high=0.005351, low=0.005208, vol=30)
        self.assertTrue(setups.lower_high_veto(cs, 47, 0.00545))
        self.assertLess(0.005351, 0.00545)
        self.assertIsNone(setups.support_retest(cs, 47, _asset("kPEPE"), self.cfg))

    def test_gram_trend_breakout_clears_structure_and_pays_two_r(self):
        cs = []
        price = 10.0
        for i in range(70):
            price += 0.08
            cs.append(_bar(i, price, high=price + 0.05, low=price - 0.15, vol=10))
        # A swing high well before the breakout window, so it is the target and not the channel.
        cs[30] = _bar(30, 14.0, high=22.0, low=13.5, vol=10)
        end = 69
        # Push the close through the prior 20 highs. Those highs are about price+0.05.
        prior_high = max(cs[i]["h"] for i in range(end - 20, end))
        cs[end] = _bar(end, prior_high + 0.4, high=prior_high + 0.4, low=cs[end]["l"], vol=40)
        got = setups.trend_breakout(cs, end, _asset("GRAM"), self.cfg)
        self.assertIsNotNone(got)
        self.assertEqual(got["setup"], "trend_breakout")
        self.assertEqual(got["side"], "long")
        self.assertGreaterEqual(got["rr"], 2)
        self.assertLess(got["tp1"], 22.01)
        self.assertIn("Chandelier", got["trail_rule"])
        self.assertAlmostEqual(got["initial_sl"], got["sl"])

    def test_round_trip_taker_fee_comes_off_the_r(self):
        # +1R on a 10 point risk, entry 100. Fees are 0.045% each side.
        net_r, net_pct = setups.net_trade(100, 110, 10, "long")
        self.assertAlmostEqual(net_r, 1 - (0.00045 * 2 * 100) / 10)
        self.assertAlmostEqual(net_pct, 10 - 0.09)
        # A short that goes nowhere still pays both fees.
        flat_r, flat_pct = setups.net_trade(100, 100, 10, "short")
        self.assertAlmostEqual(flat_r, -(0.00045 * 2 * 100) / 10)
        self.assertAlmostEqual(flat_pct, -0.09)


if __name__ == "__main__":
    unittest.main()
