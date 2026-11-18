#!/usr/bin/env python3
"""c1_maker signal, fill, exit and fee tests. No network and no webhook."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import c1_maker
import paper
import paper_run

HOUR = paper.HOUR_MS


def bars_at(start, rows):
    out = []
    for i, (o, h, l, c) in enumerate(rows):
        out.append({"t": start + i * HOUR, "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": 1.0})
    return out


def pump_rows(high, low, close, open_, n=48, spike_at=10, quiet=130.0):
    rows = []
    for i in range(n):
        if i == spike_at:
            rows.append((low, high, low, min(high, low * 1.1)))
        elif i == n - 1:
            top = max(open_, close)
            rows.append((open_, top, min(open_, close), close))
        else:
            rows.append((quiet, quiet + 1, quiet - 1, quiet))
    return rows


def pump_candles(**kwargs):
    return bars_at(0, pump_rows(**kwargs))


def resting(limit=100.0, atr=4.0, signal_t=0, coin="AAA", ta_extreme=False):
    sl, tp = c1_maker.planned_levels(limit, atr)
    return {
        "coin": coin,
        "side": "short",
        "signal_t": signal_t,
        "expires_t": signal_t + 3 * HOUR,
        "window_open": True,
        "limit": limit,
        "atr": atr,
        "sl": sl,
        "tp": tp,
        "pump_high": 140.0,
        "pump_low": 100.0,
        "pump_ratio": 1.4,
        "close_off_high": 0.05,
        "bar_return": -0.04,
        "ta_extreme": ta_extreme,
        "day_ntl_vlm": 250000.0,
        "naive": None,
        "conservative": None,
        "shadow": None,
        "l2_snaps": [],
        "best_bid": None,
        "best_ask": None,
        "ask_depth_ntl": None,
        "ask_depth_complete": None,
        "limit_vs_spread": None,
        "checked_t": signal_t,
    }


def series_of(mapping):
    return {coin: paper.index_series(candles, [None] * len(candles)) for coin, candles in mapping.items()}


def run_step(signals, mapping, start, end, active=None, meta=None):
    book = {"signals": signals}
    return c1_maker.step(
        book, series_of(mapping),
        process_from=start, latest_closed=end,
        meta=meta or {}, active=active or [],
    )


class SignalTests(unittest.TestCase):
    def test_pump_fade_bar_is_a_short(self):
        candles = pump_candles(high=140, low=100, close=132, open_=140)
        found = c1_maker.detect(candles, 47, atr=8)
        self.assertIsNotNone(found)
        self.assertEqual(found["side"], "short")
        self.assertAlmostEqual(found["pump_ratio"], 1.4)
        self.assertAlmostEqual(found["limit"], 132 + 0.25 * 8)
        self.assertFalse(found["ta_extreme"])
        prepared = c1_maker.prepare(candles)
        self.assertGreater(prepared["atr"][47], 0)
        self.assertIsNotNone(c1_maker.detect(candles, 47, prepared["atr"][47]))

    def test_boundaries_are_inclusive_and_the_misses_are_not(self):
        def last(high, low, close, open_):
            return c1_maker.detect(pump_candles(high=high, low=low, close=close, open_=open_), 47, atr=4)

        high = 140.0
        self.assertIsNotNone(last(high, 100, high * 0.97, high))
        self.assertIsNotNone(last(high, 100, high * 0.94, high))
        self.assertIsNone(last(high, 100, high * 0.971, high))
        self.assertIsNone(last(high, 100, high * 0.939, high))
        self.assertIsNone(last(139.0, 100, 132, 137))
        # Return warmer than -3% with the close still 3-6% under the high.
        self.assertIsNone(last(high, 100, 132, 132 / 0.971))
        self.assertIsNotNone(last(high, 100, 132, 132 / 0.97))

    def test_hundred_percent_pump_sets_the_alert_only_flag(self):
        mild = c1_maker.detect(pump_candles(high=199, low=100, close=189, open_=199), 47, atr=4)
        hot = c1_maker.detect(pump_candles(high=200, low=100, close=190, open_=200), 47, atr=4)
        self.assertIsNotNone(mild)
        self.assertFalse(mild["ta_extreme"])
        self.assertTrue(hot["ta_extreme"])
        self.assertGreaterEqual(hot["pump_ratio"], 2.0)

    def test_a_gap_or_a_flat_atr_kills_the_signal(self):
        candles = pump_candles(high=140, low=100, close=132, open_=140)
        candles[20]["t"] += HOUR
        self.assertIsNone(c1_maker.detect(candles, 47, atr=4))
        intact = pump_candles(high=140, low=100, close=132, open_=140)
        self.assertIsNone(c1_maker.detect(intact, 47, atr=0))
        self.assertIsNone(c1_maker.detect(intact, 47, atr=None))
        self.assertIsNone(c1_maker.detect(intact, 10, atr=4))


class FillModelTests(unittest.TestCase):
    def test_naive_touches_and_conservative_needs_half_a_tenth_of_a_percent(self):
        self.assertFalse(c1_maker.naive_hit(99.99, 100))
        self.assertTrue(c1_maker.naive_hit(100, 100))
        self.assertTrue(c1_maker.naive_hit(100.04, 100))
        self.assertFalse(c1_maker.conservative_hit(100, 100))
        self.assertFalse(c1_maker.conservative_hit(100.04, 100))
        self.assertTrue(c1_maker.conservative_hit(100.05, 100))
        self.assertTrue(c1_maker.conservative_hit(100 * 1.0005, 100))

    def test_limit_lives_three_bars_and_records_both_fills(self):
        sig = resting()
        candles = bars_at(HOUR, [
            (99, 100.04, 98, 99.5),   # naive only
            (99, 100.2, 98, 99.5),    # conservative too; 100.2 clears 0.05%
            (99, 99.5, 98, 99),
        ])
        _book, rows, events = run_step([sig], {"AAA": candles}, HOUR, 3 * HOUR)
        self.assertEqual(rows, [])
        kinds = [event["type"] for event in events]
        self.assertEqual(kinds, ["c1_maker_fill", "c1_maker_fill"])
        self.assertEqual([event["fill_model"] for event in events], ["naive", "conservative"])
        self.assertEqual(events[0]["book"], "c1_maker")
        self.assertEqual(events[0]["side"], "short")
        self.assertEqual(events[0]["entry"], 100)
        self.assertFalse(events[0]["ta_extreme"])
        open_sig = _book["signals"][0]
        self.assertFalse(open_sig["window_open"])
        self.assertAlmostEqual(open_sig["naive"]["entry"], 100)
        self.assertAlmostEqual(open_sig["conservative"]["entry"], 100)
        self.assertEqual(open_sig["naive"]["entry_t"], HOUR)
        self.assertEqual(open_sig["conservative"]["entry_t"], 2 * HOUR)

    def test_bar_four_cannot_fill_an_expired_limit(self):
        candles = bars_at(HOUR, [
            (99, 99.5, 98, 99),
            (99, 99.5, 98, 99),
            (99, 99.5, 98, 99),
            (99, 150, 98, 140),
        ])
        _book, rows, events = run_step([resting()], {"AAA": candles}, HOUR, 4 * HOUR)
        self.assertTrue(all(event["type"] != "c1_maker_fill" for event in events))
        self.assertEqual(rows[0]["status"], "unfilled")
        self.assertEqual(rows[0]["naive_filled"], "0")
        self.assertEqual(rows[0]["conservative_filled"], "0")
        expired = [event for event in events if event.get("exit_reason") == "expired-unfilled"]
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["book"], "c1_maker")
        self.assertIsNone(expired[0]["entry"])


class ExitAndFeeTests(unittest.TestCase):
    def test_settle_uses_maker_entry_and_the_exit_schedule(self):
        size = c1_maker.size_coins(100, 107)
        self.assertAlmostEqual(size, 2.5 / 7)
        target = c1_maker.settle(100, 93, size, "maker", "maker")
        stopped = c1_maker.settle(100, 107, size, "maker", "taker")
        self.assertAlmostEqual(target["gross_pnl"], 2.5)
        self.assertAlmostEqual(stopped["gross_pnl"], -2.5)
        self.assertAlmostEqual(target["fees"], 0.00015 * size * (100 + 93))
        self.assertAlmostEqual(stopped["fees"], 0.00015 * size * 100 + 0.00045 * size * 107)
        self.assertAlmostEqual(target["r"], target["net_pnl"] / 2.5)
        self.assertLess(stopped["r"], -1)

    def test_stop_on_the_fill_bar_and_target_is_not_taken_there(self):
        # The low prints through the target, but the short is only filled on the way up.
        early = bars_at(HOUR, [
            (99, 100.02, 90, 99),
            (99, 99.5, 98, 99),
            (99, 99.5, 98, 99),
        ])
        book, rows, _events = run_step([resting()], {"AAA": early}, HOUR, 3 * HOUR)
        self.assertEqual(rows, [])
        self.assertIsNone(book["signals"][0]["naive"]["exit_reason"])
        stopped = bars_at(HOUR, [
            (99, 108, 98, 100),
            (99, 99, 98, 99),
            (99, 99, 98, 99),
        ])
        _book, rows, events = run_step([resting()], {"AAA": stopped}, HOUR, 3 * HOUR)
        self.assertEqual(rows[0]["status"], "closed")
        self.assertEqual(rows[0]["exit_reason"], "sl")
        self.assertAlmostEqual(float(rows[0]["exit"]), 107)
        size = 2.5 / 7
        expect = c1_maker.settle(100, 107, size, "maker", "taker")
        self.assertAlmostEqual(float(rows[0]["r"]), expect["r"])
        self.assertAlmostEqual(float(rows[0]["fees"]), expect["fees"])
        # 108 also clears the conservative buffer, so both shorts stop on that bar.
        exits = [event for event in events if event["type"] == "c1_maker_exit"]
        self.assertEqual([event["exit_reason"] for event in exits], ["sl", "sl"])
        self.assertEqual([event["fill_model"] for event in exits], ["naive", "conservative"])
        self.assertEqual(rows[0]["conservative_filled"], "1")
        self.assertAlmostEqual(float(rows[0]["conservative_r"]), expect["r"])

    def test_later_bar_target_is_maker_and_a_double_touch_stops(self):
        candles = bars_at(HOUR, [
            (99, 100.02, 98, 99.5),
            (99, 99.5, 90, 95),
            (99, 99, 98, 99),
        ])
        _book, rows, _events = run_step([resting()], {"AAA": candles}, HOUR, 3 * HOUR)
        self.assertEqual(rows[0]["exit_reason"], "tp")
        self.assertAlmostEqual(float(rows[0]["exit"]), 93)
        size = 2.5 / 7
        expect = c1_maker.settle(100, 93, size, "maker", "maker")
        self.assertAlmostEqual(float(rows[0]["r"]), expect["r"])
        self.assertAlmostEqual(float(rows[0]["risk_usd"]), 2.5)
        both = bars_at(HOUR, [
            (99, 100.02, 98, 99),
            (100, 110, 90, 100),
            (99, 99, 98, 99),
        ])
        _book, rows, _events = run_step([resting()], {"AAA": both}, HOUR, 3 * HOUR)
        self.assertEqual(rows[0]["exit_reason"], "sl")
        self.assertAlmostEqual(float(rows[0]["exit"]), 107)

    def test_a_gap_through_the_stop_fills_at_the_open(self):
        candles = bars_at(HOUR, [
            (99, 100.02, 98, 99),
            (110, 112, 109, 111),
            (99, 99, 98, 99),
        ])
        _book, rows, _events = run_step([resting()], {"AAA": candles}, HOUR, 3 * HOUR)
        self.assertEqual(rows[0]["exit_reason"], "sl")
        self.assertAlmostEqual(float(rows[0]["exit"]), 110)
        size = 2.5 / 7
        expect = c1_maker.settle(100, 110, size, "maker", "taker")
        self.assertAlmostEqual(float(rows[0]["r"]), expect["r"])

    def test_time_exit_after_48_bars_at_the_close_pays_taker(self):
        rows_px = []
        for i in range(48):
            rows_px.append((99.8, 100.02, 99.5, 99.7))
        candles = bars_at(HOUR, rows_px)
        _book, rows, events = run_step([resting()], {"AAA": candles}, HOUR, 48 * HOUR)
        self.assertEqual(rows[0]["status"], "closed")
        self.assertEqual(rows[0]["exit_reason"], "time")
        self.assertAlmostEqual(float(rows[0]["exit"]), 99.7)
        self.assertEqual(rows[0]["naive_filled"], "1")
        self.assertEqual(rows[0]["conservative_filled"], "0")
        size = 2.5 / 7
        expect = c1_maker.settle(100, 99.7, size, "maker", "taker")
        self.assertAlmostEqual(float(rows[0]["r"]), expect["r"])
        reasons = [event.get("exit_reason") for event in events]
        self.assertIn("time", reasons)
        self.assertNotIn("expired-unfilled", reasons)

    def test_unfilled_limit_logs_the_taker_path_at_the_next_open(self):
        # Next open is 90, so the taker stop sits at 97, under the limit at 100.
        # Bar 2 trades 98 and stops the counterfactual without ever trading the limit.
        candles = bars_at(HOUR, [
            (90, 96, 89, 95),
            (95, 98, 94, 97),
            (96, 97, 95, 96),
        ])
        _book, rows, events = run_step([resting()], {"AAA": candles}, HOUR, 3 * HOUR)
        self.assertEqual(rows[0]["status"], "unfilled")
        self.assertEqual(rows[0]["exit_reason"], "expired-unfilled")
        self.assertAlmostEqual(float(rows[0]["hypothetical_entry"]), 90)
        self.assertEqual(rows[0]["hypothetical_exit_reason"], "sl")
        sl = 90 + 1.75 * 4
        self.assertAlmostEqual(float(rows[0]["hypothetical_exit"]), sl)
        size = c1_maker.size_coins(90, sl)
        expect = c1_maker.settle(90, sl, size, "taker", "taker")
        self.assertAlmostEqual(float(rows[0]["hypothetical_r"]), expect["r"])
        expired = next(event for event in events if event.get("exit_reason") == "expired-unfilled")
        self.assertAlmostEqual(expired["r"], expect["r"])
        self.assertAlmostEqual(expired["hypothetical_entry"], 90)
        self.assertTrue(expired["paper"])


class BookAndCapacityTests(unittest.TestCase):
    def test_limit_location_and_ask_depth(self):
        self.assertEqual(c1_maker.limit_vs_spread(100.5, 100, 101), "inside")
        self.assertEqual(c1_maker.limit_vs_spread(101, 100, 101), "inside")
        self.assertEqual(c1_maker.limit_vs_spread(102, 100, 101), "beyond")
        self.assertEqual(c1_maker.limit_vs_spread(100, 100, 101), "through")
        self.assertEqual(c1_maker.limit_vs_spread(99, None, 101), "unknown")
        asks = [{"px": 101, "sz": 2}, {"px": 102, "sz": 3}, {"px": 103, "sz": 1}]
        depth, complete = c1_maker.ask_depth_within(asks, 101.5)
        self.assertAlmostEqual(depth, 202)
        self.assertTrue(complete)
        inside = c1_maker.book_snapshot(100.5, {"bids": [{"px": 100, "sz": 1}], "asks": asks}, 2 * HOUR + 5)
        self.assertEqual(inside["limit_vs_spread"], "inside")
        self.assertEqual(inside["best_ask"], 101)
        snap = c1_maker.book_snapshot(101.5, {"bids": [{"px": 100, "sz": 1}], "asks": asks}, 2 * HOUR + 5)
        self.assertEqual(snap["limit_vs_spread"], "beyond")
        self.assertEqual(snap["best_ask"], 101)
        self.assertEqual(snap["ts"], paper.iso(2 * HOUR))
        full = [{"px": 100 + i, "sz": 1} for i in range(20)]
        _depth, cut = c1_maker.ask_depth_within(full, 10_000)
        self.assertFalse(cut)

    def test_hourly_book_snap_is_kept_once_per_hour(self):
        sig = resting()
        book = {"signals": [sig]}

        def fetch(_coin):
            return {"bids": [{"px": 99, "sz": 1}], "asks": [{"px": 101, "sz": 4}, {"px": 104, "sz": 1}]}

        self.assertTrue(c1_maker.record_l2(book, fetch, HOUR + 10))
        self.assertFalse(c1_maker.record_l2(book, fetch, HOUR + 20))
        self.assertEqual(len(sig["l2_snaps"]), 1)
        self.assertEqual(sig["limit_vs_spread"], "inside")
        self.assertAlmostEqual(sig["ask_depth_ntl"], 0)
        self.assertTrue(c1_maker.record_l2(book, fetch, 2 * HOUR))
        self.assertEqual(len(sig["l2_snaps"]), 2)

        def broken(_coin):
            raise RuntimeError("l2 caido")

        self.assertTrue(c1_maker.record_l2(book, broken, 3 * HOUR))
        self.assertEqual(sig["l2_snaps"][-1]["limit_vs_spread"], "unknown")

    def test_one_coin_and_ten_slots(self):
        candles = pump_candles(high=140, low=100, close=132, open_=140)
        last = candles[-1]["t"]
        occupied = resting(coin="AAA", signal_t=last, limit=10_000, atr=4)
        occupied["checked_t"] = last
        book, rows, events = c1_maker.step(
            {"signals": [occupied]},
            {
                "AAA": c1_maker.prepare(candles),
                "BBB": c1_maker.prepare(candles),
            },
            process_from=last, latest_closed=last,
            meta={"BBB": {"dayNtlVlm": 20}}, active=["AAA", "BBB"],
        )
        orders = [event for event in events if event["type"] == "c1_maker_order"]
        self.assertEqual([event["coin"] for event in orders], ["BBB"])
        self.assertEqual(rows, [])
        names = []
        mapping = {}
        prepared = c1_maker.prepare(candles)
        for i in range(11):
            coin = f"C{i:02d}"
            names.append(coin)
            mapping[coin] = prepared
        _book, rows, events = c1_maker.step(
            {"signals": []}, mapping,
            process_from=last, latest_closed=last,
            meta={coin: {"dayNtlVlm": 5} for coin in names}, active=names,
        )
        orders = [event for event in events if event["type"] == "c1_maker_order"]
        self.assertEqual(len(orders), 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "skipped_cap")
        self.assertEqual(rows[0]["coin"], "C10")
        self.assertTrue(all(event["type"] != "c1_maker_order" or event["coin"] != "C10" for event in events))
        self.assertEqual(book["signals"][0]["coin"], "AAA")

    def test_extreme_flag_is_on_the_order_payload(self):
        candles = pump_candles(high=200, low=100, close=190, open_=200)
        last = candles[-1]["t"]
        _book, _rows, events = c1_maker.step(
            {"signals": []}, {"HOT": c1_maker.prepare(candles)},
            process_from=last, latest_closed=last,
            meta={"HOT": {"dayNtlVlm": 80}}, active=["HOT"],
        )
        order = events[0]
        self.assertEqual(order["type"], "c1_maker_order")
        self.assertTrue(order["ta_extreme"])
        self.assertEqual(order["book"], "c1_maker")
        self.assertEqual(order["side"], "short")
        self.assertIsNone(order["entry"])
        self.assertIsNotNone(order["limit"])
        self.assertIsNotNone(order["sl"])
        self.assertIsNotNone(order["tp"])
        self.assertGreater(order["tp"], 0)
        self.assertLess(order["tp"], order["limit"])
        self.assertGreater(order["sl"], order["limit"])

    def test_universe_keeps_illiquid_main_names(self):
        names, _meta = c1_maker.main_universe([
            {"dex": "main", "coin": "TINY", "dayNtlVlm": 10},
            {"dex": "main", "coin": "BTC", "dayNtlVlm": 9e9},
            {"dex": "xyz", "coin": "BIG", "dayNtlVlm": 9e9},
            {"dex": "main", "coin": "OLD", "isDelisted": True, "dayNtlVlm": 9e9},
        ])
        self.assertEqual(names, ["BTC", "TINY"])

    def test_book_file_keeps_the_thousand_usdc_account(self):
        self.assertEqual(c1_maker.EQUITY_USDC, 1000.0)
        self.assertEqual(c1_maker.RISK_USD, 2.5)
        self.assertEqual(c1_maker.FEE_MAKER, 0.00015)
        self.assertEqual(c1_maker.FEE_TAKER, 0.00045)
        self.assertEqual(paper.empty_book("c1_maker")["equity_usdc"], 1000.0)
        self.assertEqual(paper.empty_book("trend4h")["equity_usdc"], 10_000.0)
        self.assertIn("c1_maker", paper.BOOKS)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def book_dir(strategy):
                return root / strategy

            with patch.object(paper, "book_dir", book_dir):
                c1_maker.save({"signals": [], "positions": [], "pending_entries": [], "pending_exits": []})
                loaded = paper.load_book("c1_maker")
                self.assertEqual(loaded["equity_usdc"], 1000.0)
                self.assertEqual(loaded["risk_pct"], 0.0025)
                self.assertEqual(loaded["fee_per_side"], 0.00015)
                self.assertEqual(loaded["fee_taker"], c1_maker.FEE_TAKER)
                self.assertIsInstance(loaded["positions"], list)

    def test_summary_fill_rate_average_r_and_profit_factor(self):
        rows = [
            {"status": "closed", "naive_filled": "1", "conservative_filled": "1", "naive_r": "1", "conservative_r": "1"},
            {"status": "closed", "naive_filled": "1", "conservative_filled": "0", "naive_r": "-0.5", "conservative_r": ""},
            {"status": "unfilled", "naive_filled": "0", "conservative_filled": "0", "hypothetical_r": "0.2"},
            {"status": "skipped_cap", "naive_filled": "0", "conservative_filled": "0"},
        ]
        stats = c1_maker.summarize_rows(rows)
        self.assertEqual(stats["trades"], 2)
        self.assertEqual(stats["naive_fills"], 2)
        self.assertEqual(stats["unfilled"], 1)
        self.assertEqual(stats["skipped_cap"], 1)
        self.assertAlmostEqual(stats["fill_rate_naive"], 2 / 3)
        self.assertAlmostEqual(stats["avg_r_naive"], 0.25)
        self.assertAlmostEqual(stats["pf_naive"], 2)
        self.assertAlmostEqual(stats["avg_r_conservative"], 1)
        self.assertIsNone(stats["pf_conservative"])
        self.assertAlmostEqual(stats["avg_r_hypothetical_unfilled"], 0.2)

    def test_a_failed_book_does_not_escape(self):
        def boom():
            raise RuntimeError("libro roto")

        events, mutated, line = paper_run.guard_book("c1_maker", boom)
        self.assertEqual(events, [])
        self.assertFalse(mutated)
        self.assertIn("fallo", line)
        self.assertNotIn("libro roto", line)


if __name__ == "__main__":
    unittest.main()
