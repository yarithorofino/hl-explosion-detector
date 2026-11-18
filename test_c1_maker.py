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
        out.append({
            "t": start + i * HOUR,
            "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": 1.0,
        })
    return out


def signal_candles(n=48, close=140.0, prev_close=145.0, min_close=100.0, peak=160.0):
    """48+ contiguous hours. Bar n-1 is the signal bar when the inputs qualify."""
    rows = []
    signal_at = n - 1
    for i in range(n):
        c, h = close, close + 1.0
        if i == 0:
            c, h = min_close, min_close + 1.0
        elif i == signal_at - 1:
            c, h = prev_close, max(prev_close, peak)
        elif i == signal_at:
            c, h = close, max(close + 1.0, peak)
        low = min(c, h) - 1.0
        rows.append((c, h, low, c))
    return bars_at(0, rows)


def resting(limit=100.0, atr=4.0, signal_t=0, coin="AAA", cfg=None):
    cfg = cfg or c1_maker.variant()
    rules = c1_maker._rules(cfg)
    sl, tp = c1_maker.planned_levels(limit, atr, cfg)
    return {
        "coin": coin,
        "side": "short",
        "variant": cfg["name"],
        "signal_t": signal_t,
        "expires_t": signal_t + 3 * HOUR,
        "window_open": True,
        "limit": limit,
        "atr": atr,
        "sl": sl,
        "tp": tp,
        "up48": 0.5,
        "dd24": -0.08,
        "ret1h": -0.04,
        "ta_extreme": False,
        "day_ntl_vlm": 250000.0,
        "rules": rules,
        "position": None,
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


def run_step(signals, mapping, start, end, active=None, meta=None, cfg=None, funding=None, last_signal_t=None):
    book = {"signals": signals, "last_signal_t": dict(last_signal_t or {})}
    return c1_maker.step(
        book, series_of(mapping),
        process_from=start, latest_closed=end,
        meta=meta or {}, active=active or [], cfg=cfg, funding=funding,
    )


class SignalTests(unittest.TestCase):
    def test_close_pump_fade_and_close_to_close_drop(self):
        candles = signal_candles()
        found = c1_maker.detect(candles, 47, atr=8)
        self.assertIsNotNone(found)
        self.assertEqual(found["side"], "short")
        self.assertAlmostEqual(found["up48"], 0.40)
        self.assertAlmostEqual(found["ret1h"], 140.0 / 145.0 - 1.0)
        self.assertLessEqual(found["dd24"], -0.06)
        self.assertAlmostEqual(found["limit"], 140.0 + 0.25 * 8)
        self.assertFalse(found["ta_extreme"])
        # The open equals the close, so the old close/open rule would not fire.
        self.assertEqual(candles[-1]["o"], candles[-1]["c"])
        prepared = c1_maker.prepare(candles)
        self.assertGreater(prepared["atr"][47], 0)
        self.assertIsNotNone(c1_maker.detect(candles, 47, prepared["atr"][47]))

    def test_boundaries_and_the_drawdown_has_no_upper_band(self):
        self.assertIsNotNone(c1_maker.detect(signal_candles(peak=160), 47, atr=4))
        # up48 just under 40%.
        self.assertIsNone(c1_maker.detect(signal_candles(min_close=100.1), 47, atr=4))
        # Drawdown of about 4%: variant A needs 6%, and there is no 3-6% band.
        shallow = signal_candles(peak=145.9)
        self.assertIsNone(c1_maker.detect(shallow, 47, atr=4, cfg=c1_maker.variant("A")))
        self.assertIsNotNone(c1_maker.detect(shallow, 47, atr=4, cfg=c1_maker.variant("C")))
        # A much deeper fade still signals.
        self.assertIsNotNone(c1_maker.detect(signal_candles(peak=220), 47, atr=4))
        # ret1h warmer than -3%.
        self.assertIsNone(c1_maker.detect(signal_candles(prev_close=143.0), 47, atr=4))
        exact_prev = 140.0 / 0.97
        self.assertIsNotNone(c1_maker.detect(signal_candles(prev_close=exact_prev), 47, atr=4))

    def test_hundred_percent_up48_sets_the_flag(self):
        hot = c1_maker.detect(signal_candles(close=200, prev_close=210, min_close=100, peak=230), 47, atr=4)
        mild = c1_maker.detect(signal_candles(close=199, prev_close=210, min_close=100, peak=230), 47, atr=4)
        self.assertTrue(hot["ta_extreme"])
        self.assertGreaterEqual(hot["up48"], 1.0)
        self.assertFalse(mild["ta_extreme"])

    def test_a_gap_kills_the_window(self):
        candles = signal_candles()
        candles[20]["t"] += HOUR
        self.assertIsNone(c1_maker.detect(candles, 47, atr=4))
        self.assertIsNone(c1_maker.detect(signal_candles(), 10, atr=4))

    def test_variants_only_change_the_listed_knobs(self):
        a, b, c = c1_maker.variant("A"), c1_maker.variant("B"), c1_maker.variant("C")
        self.assertEqual(c1_maker.LIVE_VARIANT, "A")
        self.assertEqual((a["sl_atr"], a["tp_r"], a["max_hold"], a["dd24_max"]), (1.5, 1.0, 12, -0.06))
        self.assertEqual((b["sl_atr"], b["tp_r"], b["max_hold"], b["dd24_max"]), (2.0, 1.0, 24, -0.06))
        self.assertEqual((c["sl_atr"], c["tp_r"], c["max_hold"], c["dd24_max"]), (2.0, 1.5, 36, -0.03))
        sl, tp = c1_maker.planned_levels(100, 4, c)
        self.assertAlmostEqual(sl, 108)
        self.assertAlmostEqual(tp, 100 - 1.5 * 8)


class FillModelTests(unittest.TestCase):
    def test_fill_needs_a_strict_trade_through(self):
        limit = 100.0
        need = limit * (1.0 + c1_maker.TRADE_THROUGH)
        self.assertFalse(c1_maker.traded_through_above(limit, limit))
        self.assertFalse(c1_maker.traded_through_above(need, limit))
        self.assertTrue(c1_maker.traded_through_above(need + 1e-6, limit))
        target = 94.0
        floor = target * (1.0 - c1_maker.TRADE_THROUGH)
        self.assertFalse(c1_maker.traded_through_below(target, target))
        self.assertFalse(c1_maker.traded_through_below(floor, target))
        self.assertTrue(c1_maker.traded_through_below(floor - 1e-6, target))

    def test_three_bars_then_the_limit_is_dead(self):
        need = 100.0 * 1.0005
        candles = bars_at(HOUR, [
            (99, need, 98, 99),
            (99, need + 0.01, 98, 99),
            (99, 99, 98, 99),
            (99, 150, 98, 140),
        ])
        _book, rows, events = run_step([resting()], {"AAA": candles}, HOUR, 3 * HOUR)
        fills = [event for event in events if event["type"] == "c1_maker_fill"]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["entry"], 100)
        self.assertEqual(fills[0]["book"], "c1_maker")
        self.assertEqual(rows, [])
        self.assertEqual(_book["signals"][0]["position"]["entry_t"], 2 * HOUR)
        quiet = bars_at(HOUR, [(99, need, 98, 99)] * 3 + [(99, 150, 98, 140)])
        _book, rows, events = run_step([resting()], {"AAA": quiet}, HOUR, 4 * HOUR)
        self.assertTrue(all(event["type"] != "c1_maker_fill" for event in events))
        self.assertEqual(rows[0]["status"], "unfilled")
        self.assertEqual(rows[0]["filled"], "0")


class ExitAndFeeTests(unittest.TestCase):
    def test_stop_on_the_fill_bar_adds_taker_fee_and_slippage(self):
        candles = bars_at(HOUR, [
            (99, 110, 98, 105),
            (99, 99, 98, 99),
            (99, 99, 98, 99),
        ])
        _book, rows, events = run_step([resting()], {"AAA": candles}, HOUR, 3 * HOUR)
        self.assertEqual(rows[0]["status"], "closed")
        self.assertEqual(rows[0]["exit_reason"], "sl")
        slipped = 106.0 * (1.0 + c1_maker.SLIPPAGE)
        self.assertAlmostEqual(float(rows[0]["exit"]), slipped)
        size = 2.5 / 6.0
        expect = c1_maker.settle(100, slipped, size, "maker", "taker", points=[])
        self.assertAlmostEqual(float(rows[0]["r"]), expect["r"])
        self.assertAlmostEqual(float(rows[0]["fees"]), expect["fees"])
        self.assertAlmostEqual(expect["fees"], 0.00015 * size * 100 + 0.00045 * size * slipped)
        self.assertLess(expect["r"], -1)
        self.assertEqual(
            [event["exit_reason"] for event in events if event["type"] == "c1_maker_exit"],
            ["sl"],
        )

    def test_target_needs_a_later_trade_through_and_pays_maker(self):
        need = 100.0 * 1.0005 + 0.01
        # The fill bar spikes through the target. That low is not a fill.
        early = bars_at(HOUR, [
            (99, need, 80, 99),
            (99, 99, 94, 99),
            (99, 99, 98, 99),
        ])
        book, rows, _events = run_step([resting()], {"AAA": early}, HOUR, 3 * HOUR)
        self.assertEqual(rows, [])
        self.assertIsNone(book["signals"][0]["position"]["exit_reason"])
        later = bars_at(HOUR, [
            (99, need, 98, 99),
            (99, 99, 93.9, 96),
            (99, 99, 98, 99),
        ])
        _book, rows, _events = run_step([resting()], {"AAA": later}, HOUR, 3 * HOUR)
        self.assertEqual(rows[0]["exit_reason"], "tp")
        self.assertAlmostEqual(float(rows[0]["exit"]), 94)
        size = 2.5 / 6.0
        expect = c1_maker.settle(100, 94, size, "maker", "maker", points=[])
        self.assertAlmostEqual(float(rows[0]["r"]), expect["r"])
        self.assertAlmostEqual(expect["fees"], 0.00015 * size * (100 + 94))

    def test_a_bar_that_reaches_both_stops_out(self):
        need = 100.0 * 1.0005 + 0.01
        candles = bars_at(HOUR, [
            (99, need, 98, 99),
            (100, 110, 90, 100),
            (99, 99, 98, 99),
        ])
        _book, rows, _events = run_step([resting()], {"AAA": candles}, HOUR, 3 * HOUR)
        self.assertEqual(rows[0]["exit_reason"], "sl")
        self.assertAlmostEqual(float(rows[0]["exit"]), 106.0 * (1.0 + c1_maker.SLIPPAGE))

    def test_a_gap_through_the_stop_slips_the_open(self):
        need = 100.0 * 1.0005 + 0.01
        candles = bars_at(HOUR, [
            (99, need, 98, 99),
            (110, 112, 109, 111),
            (99, 99, 98, 99),
        ])
        _book, rows, _events = run_step([resting()], {"AAA": candles}, HOUR, 3 * HOUR)
        self.assertEqual(rows[0]["exit_reason"], "sl")
        self.assertAlmostEqual(float(rows[0]["exit"]), 110.0 * (1.0 + c1_maker.SLIPPAGE))

    def test_time_exit_on_bar_12_and_a_shorter_variant_hold(self):
        need = 100.0 * 1.0005 + 0.01
        candles = bars_at(HOUR, [(99.8, need, 99.5, 99.7)] + [(99.8, 100.0, 99.5, 99.7)] * 11)
        _book, rows, events = run_step([resting()], {"AAA": candles}, HOUR, 12 * HOUR)
        self.assertEqual(rows[0]["exit_reason"], "time")
        self.assertAlmostEqual(float(rows[0]["exit"]), 99.7 * (1.0 + c1_maker.SLIPPAGE))
        self.assertEqual(rows[0]["variant"], "A")
        self.assertIn("time", [event.get("exit_reason") for event in events])
        short = c1_maker.variant("A", max_hold=2)
        _book, rows, _events = run_step(
            [resting(cfg=short)], {"AAA": candles}, HOUR, 3 * HOUR, cfg=short,
        )
        self.assertEqual(rows[0]["exit_reason"], "time")
        self.assertEqual(rows[0]["exit_time"], paper.iso(2 * HOUR + HOUR))

    def test_funding_is_received_by_the_short(self):
        need = 100.0 * 1.0005 + 0.01
        candles = bars_at(HOUR, [
            (99, need, 98, 99),
            (99, 99, 93.9, 96),
            (99, 99, 98, 99),
        ])
        # The print at the end of the fill bar is inside the hold.
        points = [(2 * HOUR, 0.0001)]
        _book, rows, _events = run_step(
            [resting()], {"AAA": candles}, HOUR, 3 * HOUR, funding={"AAA": points},
        )
        size = 2.5 / 6.0
        fund = paper.funding_pnl("short", 100, size, HOUR, 2 * HOUR + HOUR, points)
        self.assertAlmostEqual(fund, 0.0001 * size * 100)
        self.assertAlmostEqual(float(rows[0]["funding_pnl"]), fund)
        self.assertEqual(rows[0]["funding_ok"], "1")
        gross = (100 - 94) * size
        fees = 0.00015 * size * (100 + 94)
        self.assertAlmostEqual(float(rows[0]["r"]), (gross - fees + fund) / 2.5)

    def test_unfilled_limit_logs_the_taker_path(self):
        # High stays under the trade-through. Bar 2 runs the taker's stop,
        # which sits under the maker limit because the entry is 90.
        candles = bars_at(HOUR, [
            (90, 96, 89, 95),
            (95, 98, 94, 97),
            (96, 97, 95, 96),
        ])
        _book, rows, events = run_step([resting()], {"AAA": candles}, HOUR, 3 * HOUR)
        self.assertEqual(rows[0]["status"], "unfilled")
        self.assertEqual(rows[0]["exit_reason"], "expired-unfilled")
        entry = 90.0 * (1.0 - c1_maker.SLIPPAGE)
        self.assertAlmostEqual(float(rows[0]["hypothetical_entry"]), entry)
        self.assertEqual(rows[0]["hypothetical_exit_reason"], "sl")
        sl = entry + 1.5 * 4
        slipped = sl * (1.0 + c1_maker.SLIPPAGE)
        self.assertAlmostEqual(float(rows[0]["hypothetical_exit"]), slipped)
        size = c1_maker.size_coins(entry, sl)
        expect = c1_maker.settle(entry, slipped, size, "taker", "taker", points=[])
        self.assertAlmostEqual(float(rows[0]["hypothetical_r"]), expect["r"])
        expired = next(event for event in events if event.get("exit_reason") == "expired-unfilled")
        self.assertAlmostEqual(expired["r"], expect["r"])
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

    def test_twelve_bar_gap_and_no_overlap(self):
        candles = signal_candles(n=48)
        last = candles[-1]["t"]
        prepared = c1_maker.prepare(candles)
        too_soon = last - 11 * HOUR
        _book, _rows, events = c1_maker.step(
            {"signals": [], "last_signal_t": {"AAA": too_soon}},
            {"AAA": prepared},
            process_from=last, latest_closed=last,
            meta={}, active=["AAA"],
        )
        self.assertEqual(events, [])
        allowed = last - 12 * HOUR
        _book, _rows, events = c1_maker.step(
            {"signals": [], "last_signal_t": {"AAA": allowed}},
            {"AAA": prepared},
            process_from=last, latest_closed=last,
            meta={}, active=["AAA"],
        )
        self.assertEqual([event["type"] for event in events], ["c1_maker_order"])
        occupied = resting(coin="AAA", signal_t=last, limit=10_000)
        occupied["checked_t"] = last
        _book, rows, events = c1_maker.step(
            {"signals": [occupied], "last_signal_t": {}},
            {"AAA": prepared, "BBB": prepared},
            process_from=last, latest_closed=last,
            meta={"BBB": {"dayNtlVlm": 20}}, active=["AAA", "BBB"],
        )
        self.assertEqual([event["coin"] for event in events if event["type"] == "c1_maker_order"], ["BBB"])
        self.assertEqual(rows, [])

    def test_ten_slots_and_the_extreme_flag_on_the_order(self):
        candles = signal_candles(close=200, prev_close=210, min_close=100, peak=230)
        last = candles[-1]["t"]
        prepared = c1_maker.prepare(candles)
        names = [f"C{i:02d}" for i in range(11)]
        _book, rows, events = c1_maker.step(
            {"signals": [], "last_signal_t": {}},
            {coin: prepared for coin in names},
            process_from=last, latest_closed=last,
            meta={coin: {"dayNtlVlm": 5} for coin in names}, active=names,
        )
        orders = [event for event in events if event["type"] == "c1_maker_order"]
        self.assertEqual(len(orders), 10)
        self.assertTrue(orders[0]["ta_extreme"])
        self.assertEqual(orders[0]["book"], "c1_maker")
        self.assertEqual(orders[0]["side"], "short")
        self.assertEqual(orders[0]["variant"], "A")
        self.assertIsNone(orders[0]["entry"])
        self.assertLess(orders[0]["tp"], orders[0]["limit"])
        self.assertGreater(orders[0]["sl"], orders[0]["limit"])
        self.assertEqual(rows[0]["status"], "skipped_cap")
        self.assertEqual(rows[0]["coin"], "C10")

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
        self.assertEqual(c1_maker.SLIPPAGE, 0.0001)
        self.assertEqual(paper.empty_book("c1_maker")["equity_usdc"], 1000.0)
        self.assertEqual(paper.empty_book("trend4h")["equity_usdc"], 10_000.0)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def book_dir(strategy):
                return root / strategy

            with patch.object(paper, "book_dir", book_dir):
                c1_maker.save({
                    "signals": [], "positions": [], "pending_entries": [],
                    "pending_exits": [], "last_signal_t": {"BTC": 5},
                })
                loaded = paper.load_book("c1_maker")
                self.assertEqual(loaded["equity_usdc"], 1000.0)
                self.assertEqual(loaded["fee_per_side"], 0.00015)
                self.assertEqual(loaded["variant"], "A")
                self.assertEqual(loaded["last_signal_t"]["BTC"], 5)

    def test_summary_reports_the_research_columns(self):
        rows = [
            {"status": "closed", "filled": "1", "r": "0.4"},
            {"status": "closed", "filled": "1", "r": "-0.2"},
            {"status": "unfilled", "filled": "0", "hypothetical_r": "0.5"},
            {"status": "skipped_cap", "filled": "0"},
        ]
        stats = c1_maker.summarize_rows(rows, days=10)
        self.assertEqual(stats["signals"], 4)
        self.assertAlmostEqual(stats["signals_per_day"], 0.4)
        self.assertAlmostEqual(stats["fill_rate"], 2 / 3)
        self.assertAlmostEqual(stats["avg_r"], 0.1)
        self.assertAlmostEqual(stats["pf"], 2)
        self.assertAlmostEqual(stats["avg_taker_r_unfilled"], 0.5)

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
