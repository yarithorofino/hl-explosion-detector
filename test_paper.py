#!/usr/bin/env python3
"""Paper strategies. No network and no webhook."""
import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import setup_scanner
import paper
import squeeze4h
import trapped_shorts1h
import trend4h

STEP = 10_000


def bars(start, rows):
    """rows are (open, high, low, close, volume)."""
    out = []
    for i, (o, h, l, c, v) in enumerate(rows):
        out.append({"t": start + i * STEP, "o": o, "h": h, "l": l, "c": c, "v": v})
    return out


def flat_then_up():
    closes = [10.0] * 12
    for k in range(1, 16):
        closes.append(10.0 + k)
    out = []
    prev = closes[0]
    for i, cl in enumerate(closes):
        o = prev
        out.append({
            "t": i * STEP, "o": o, "h": max(o, cl) + 0.2,
            "l": min(o, cl) - 0.2, "c": cl, "v": 10.0,
        })
        prev = cl
    return out


def cfg(**over):
    base = {
        "book": "trend4h",
        "interval": STEP,
        "max_hold": 180,
        "max_positions": 10,
        "k": 3.0,
        "t_mult": None,
        "risk_usd": 25.0,
    }
    base.update(over)
    return base


def run(series, signal, universe, book, config, start, end, forming_t, opens, marks=None, funding=None):
    return paper.simulate(
        series, signal, book, config,
        process_from=start, latest_closed=end, forming_t=forming_t,
        forming_opens=opens, marks=marks, universe=universe,
        funding={} if funding is None else funding,
    )


class IndicatorTests(unittest.TestCase):
    def test_ema_seeds_with_sma_and_matches_the_scanner(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        got = paper.ema_series(values, 3)
        self.assertEqual(got[:2], [None, None])
        self.assertAlmostEqual(got[2], 2.0)
        self.assertAlmostEqual(got[3], 3.0)
        self.assertAlmostEqual(got[4], 4.0)
        self.assertEqual(got, setup_scanner.ema_series(values, 3))
        self.assertIsNone(paper.ema_series([1.0, 2.0], 3))

    def test_wilder_atr_and_scanner_agree(self):
        candles = [
            {"h": 10, "l": 8, "c": 9},
            {"h": 11, "l": 9, "c": 10},
            {"h": 12, "l": 10, "c": 11},
            {"h": 14, "l": 11, "c": 13},
            {"h": 13, "l": 10, "c": 11},
        ]
        got = paper.wilder_atr(candles, 3)
        self.assertEqual(got[:3], [None, None, None])
        self.assertAlmostEqual(got[3], 7.0 / 3.0)
        self.assertAlmostEqual(got[4], (7.0 / 3.0 * 2 + 3) / 3.0)
        self.assertEqual(got, setup_scanner.wilder_atr(candles, 3))

    def test_cross_up_down_and_a_touch_is_not_a_cross(self):
        self.assertEqual(paper.cross_side([1, 2], [2, 1], 1), "long")
        self.assertEqual(paper.cross_side([2, 1], [1, 2], 1), "short")
        self.assertIsNone(paper.cross_side([1, 1], [1, 1], 1))
        self.assertIsNone(paper.cross_side([None, 2], [1, 1], 1))

    def test_warmup_blocks_an_early_cross(self):
        series = {"AAA": {"ema_fast": [None, 1.0, 2.0], "ema_slow": [None, 2.0, 1.0]}}
        self.assertIsNone(trend4h.make_signal(series, warmup=100)("AAA", 2))
        self.assertEqual(trend4h.make_signal(series, warmup=3)("AAA", 2)["side"], "long")

    def test_bandwidth_uses_population_stdev(self):
        bw = paper.bandwidth_series([1.0, 2.0, 3.0], period=3, nstd=2)
        var = 2.0 / 3.0
        self.assertAlmostEqual(bw[2], 4.0 * (var ** 0.5) / 2.0)
        self.assertIsNone(bw[1])

    def test_percentile_rank_is_the_share_strictly_below(self):
        ranks = paper.percentile_rank_series([5.0, 1.0, 1.0, 1.0], 4)
        self.assertAlmostEqual(ranks[-1], 0.0)
        ranks = paper.percentile_rank_series([1.0, 2.0, 3.0, 4.0], 4)
        self.assertAlmostEqual(ranks[-1], 1.0)
        ranks = paper.percentile_rank_series([1.0, 2.0, 3.0, 2.0], 4)
        self.assertAlmostEqual(ranks[-1], 1.0 / 3.0)


class StopAndFundingTests(unittest.TestCase):
    def test_stop_fill_and_gap(self):
        self.assertEqual(paper.stop_fill_price("long", 90, {"o": 100, "h": 101, "l": 89, "c": 95}), 90)
        self.assertEqual(paper.stop_fill_price("long", 90, {"o": 80, "h": 85, "l": 70, "c": 82}), 80)
        self.assertIsNone(paper.stop_fill_price("long", 90, {"o": 100, "h": 101, "l": 91, "c": 95}))
        self.assertEqual(paper.stop_fill_price("short", 110, {"o": 100, "h": 111, "l": 99, "c": 105}), 110)
        self.assertEqual(paper.stop_fill_price("short", 110, {"o": 120, "h": 125, "l": 115, "c": 122}), 120)

    def test_trail_ratchets_up_only(self):
        self.assertAlmostEqual(paper.ratchet_long_stop(94, 112, 2, 5), 102)
        self.assertAlmostEqual(paper.ratchet_long_stop(102, 112, 4, 5), 102)

    def test_fees_and_funding_sign(self):
        pos = {"side": "long", "entry": 100.0, "size": 1.0, "entry_t": 0, "risk_usd": 25.0}
        acct = paper.account(pos, 100.0, paper.HOUR_MS, [])
        self.assertAlmostEqual(acct["fees"], 0.14)
        self.assertAlmostEqual(acct["r"], -0.14 / 25.0)
        points = [(paper.HOUR_MS + 50, 0.0001)]
        paid = paper.funding_pnl("long", 100.0, 1.0, 0, paper.HOUR_MS, points)
        received = paper.funding_pnl("short", 100.0, 1.0, 0, paper.HOUR_MS, points)
        self.assertAlmostEqual(paid, -0.01)
        self.assertAlmostEqual(received, 0.01)
        # The position that opens on that hour does not pay the same print.
        fresh = paper.funding_pnl("long", 100.0, 1.0, paper.HOUR_MS, 2 * paper.HOUR_MS, points)
        self.assertAlmostEqual(fresh, 0.0)

    def test_book_parameters(self):
        self.assertEqual(trend4h.CFG["max_positions"], 10)
        self.assertEqual(trend4h.CFG["max_hold"], 180)
        self.assertEqual(trend4h.CFG["k"], 3.0)
        self.assertIsNone(trend4h.CFG["t_mult"])
        self.assertEqual(squeeze4h.CFG["max_positions"], 10)
        self.assertEqual(squeeze4h.CFG["max_hold"], 240)
        self.assertEqual(squeeze4h.CFG["k"], 3.0)
        self.assertEqual(squeeze4h.CFG["t_mult"], 5.0)
        self.assertEqual(trapped_shorts1h.CFG["max_positions"], 5)
        self.assertEqual(trapped_shorts1h.CFG["max_hold"], 120)
        self.assertEqual(trapped_shorts1h.CFG["k"], 2.0)
        self.assertEqual(trapped_shorts1h.CFG["t_mult"], 4.0)
        self.assertEqual(paper.RISK_USD, 25.0)
        self.assertEqual(paper.FEE_PER_SIDE, 0.0007)


class SimulateTests(unittest.TestCase):
    def test_real_cross_enters_on_the_next_open(self):
        candles = flat_then_up()
        series = {"AAA": trend4h.prepare(candles, ema_fast=2, ema_slow=5, atr_period=3)}
        signal = trend4h.make_signal(series, warmup=6)
        found = next(i for i in range(len(candles)) if (signal("AAA", i) or {}).get("side") == "long")
        atr = series["AAA"]["atr"][found]
        self.assertGreater(atr, 0)
        t = candles[found]["t"]
        # The next candle is not closed yet: the fill is the first price after the close.
        trimmed = {"AAA": trend4h.prepare(candles[:found + 1], ema_fast=2, ema_slow=5, atr_period=3)}
        signal = trend4h.make_signal(trimmed, warmup=6)
        result = run(
            trimmed, signal, ["AAA"], paper.empty_book("trend4h"), cfg(),
            t, t, t + STEP, {"AAA": 50.0}, marks={"AAA": 51.0},
        )
        self.assertEqual(len(result["positions"]), 1)
        pos = result["positions"][0]
        self.assertEqual(pos["side"], "long")
        self.assertAlmostEqual(pos["entry"], 50.0)
        self.assertAlmostEqual(pos["stop"], 50.0 - 3.0 * atr)
        self.assertEqual(result["events"][0]["type"], "trend4h_entry")
        self.assertTrue(result["events"][0]["paper"])
        self.assertEqual(result["events"][0]["candle_time"], paper.iso(t))
        self.assertAlmostEqual(result["events"][0]["mark"], 51.0)
        early = trend4h.make_signal(series, warmup=10_000)
        blocked = run(
            series, early, ["AAA"], paper.empty_book("trend4h"), cfg(),
            t, t, t + STEP, {"AAA": 50.0},
        )
        self.assertEqual(blocked["positions"], [])

    def test_stop_and_gap_and_reverse(self):
        candle = bars(STEP, [(100, 110, 89, 108, 10)])[0]
        series = {"AAA": paper.index_series([candle], [2.0])}

        def short_at_first(coin, i):
            return {"side": "short", "atr": 2.0, "ema100": 1.0, "ema200": 2.0} if i == 0 else None

        book = paper.empty_book("trend4h")
        book["positions"] = [{
            "coin": "AAA", "side": "long", "entry": 100.0, "stop": 90.0,
            "initial_stop": 90.0, "atr": 2.0, "signal_t": 0, "entry_t": 0,
            "size": 1.0, "risk_usd": 25.0, "highest_high": None,
        }]
        result = run(
            series, short_at_first, ["AAA"], book, cfg(),
            STEP, STEP, STEP * 2, {"AAA": 80.0}, funding={"AAA": []},
        )
        self.assertEqual(result["rows"][0]["exit_reason"], "stop")
        self.assertEqual(result["rows"][0]["exit"], "90")
        # The cross still schedules the short. Its fill is the gap-open price.
        self.assertEqual(result["positions"][0]["side"], "short")
        self.assertAlmostEqual(result["positions"][0]["entry"], 80.0)
        self.assertAlmostEqual(result["positions"][0]["stop"], 80.0 + 6.0)

        gap_candle = bars(STEP, [(80, 85, 70, 82, 10)])[0]
        gap_series = {"AAA": paper.index_series([gap_candle], [2.0])}
        gap = run(
            gap_series, lambda coin, i: None, ["AAA"], book, cfg(),
            STEP, STEP, STEP * 2, {}, funding={"AAA": []},
        )
        self.assertEqual(gap["rows"][0]["exit"], "80")
        self.assertEqual(gap["rows"][0]["exit_reason"], "stop")
        self.assertEqual(gap["positions"], [])

    def test_reverse_exits_and_reenters_at_the_same_open(self):
        candle = bars(STEP, [(100, 110, 99, 108, 10)])[0]
        series = {"AAA": paper.index_series([candle], [2.0])}

        def flip(coin, i):
            return {"side": "short", "atr": 2.0} if i == 0 else None

        book = paper.empty_book("trend4h")
        book["positions"] = [{
            "coin": "AAA", "side": "long", "entry": 100.0, "stop": 50.0,
            "atr": 2.0, "signal_t": 0, "entry_t": 0, "size": 2.0, "risk_usd": 25.0,
        }]
        result = run(
            series, flip, ["AAA"], book, cfg(),
            STEP, STEP, STEP * 2, {"AAA": 105.0}, funding={"AAA": []},
        )
        self.assertEqual(result["rows"][0]["exit_reason"], "reverse")
        self.assertEqual(result["rows"][0]["exit"], "105")
        self.assertEqual(result["positions"][0]["side"], "short")
        self.assertAlmostEqual(result["positions"][0]["entry"], 105.0)
        kinds = [e["type"] for e in result["events"]]
        self.assertEqual(kinds, ["trend4h_exit", "trend4h_entry"])

    def test_time_exit_after_max_hold(self):
        candle = bars(2 * STEP, [(100, 101, 99, 100, 5)])[0]
        # The exit bar is the only one we visit. Hold clock starts at entry 0.
        series = {"AAA": paper.index_series([candle], [1.0])}
        book = paper.empty_book("trend4h")
        book["positions"] = [{
            "coin": "AAA", "side": "long", "entry": 100.0, "stop": 10.0,
            "atr": 1.0, "signal_t": 0, "entry_t": 0, "size": 1.0, "risk_usd": 25.0,
        }]
        result = run(
            series, lambda coin, i: None, ["AAA"], book, cfg(max_hold=2),
            2 * STEP, 2 * STEP, 3 * STEP, {}, funding={"AAA": []},
        )
        self.assertEqual(result["rows"][0]["exit_reason"], "time")
        self.assertEqual(result["rows"][0]["exit"], "100")
        self.assertEqual(result["positions"], [])

    def test_cap_skips_the_rest_and_a_reverse_keeps_the_slot(self):
        candle = bars(0, [(10, 11, 9, 10, 5)])[0]

        def always(coin, i):
            return {"side": "long", "atr": 1.0} if i == 0 else None

        series = {c: paper.index_series([dict(candle)], [1.0]) for c in ("C", "A", "B")}
        result = run(
            series, always, ["A", "B", "C"], paper.empty_book("trend4h"),
            cfg(max_positions=2), 0, 0, STEP, {c: 50.0 for c in "ABC"},
        )
        self.assertEqual(sorted(p["coin"] for p in result["positions"]), ["A", "B"])
        skipped = [r for r in result["rows"] if r["status"] == "skipped_cap"]
        self.assertEqual([r["coin"] for r in skipped], ["C"])
        self.assertTrue(all(e["type"] == "trend4h_entry" for e in result["events"]))

        held = bars(0, [(10, 12, 9, 11, 5)])[0]
        nxt = bars(STEP, [(20, 21, 19, 20, 5)])[0]
        book = paper.empty_book("trend4h")
        book["positions"] = [{
            "coin": "A", "side": "long", "entry": 10.0, "stop": 1.0,
            "atr": 1.0, "signal_t": -STEP, "entry_t": -STEP, "size": 1.0, "risk_usd": 25.0,
        }]

        def flip_and_new(coin, i):
            if i != 0:
                return None
            if coin == "A":
                return {"side": "short", "atr": 1.0}
            if coin == "C":
                return {"side": "long", "atr": 1.0}
            return None

        series = {
            "A": paper.index_series([held, nxt], [1.0, 1.0]),
            "C": paper.index_series([held, nxt], [1.0, 1.0]),
        }
        rev = run(
            series, flip_and_new, ["C", "A"], book, cfg(max_positions=1),
            0, STEP, STEP * 2, {}, funding={"A": [], "C": []},
        )
        self.assertEqual([p["coin"] for p in rev["positions"]], ["A"])
        self.assertEqual(rev["positions"][0]["side"], "short")
        self.assertEqual([r["coin"] for r in rev["rows"] if r["status"] == "skipped_cap"], ["C"])
        self.assertEqual(rev["rows"][0]["exit_reason"], "reverse")

    def test_one_position_per_coin(self):
        candle = bars(STEP, [(10, 11, 9, 10, 1)])[0]
        series = {"AAA": paper.index_series([candle], [1.0])}
        book = paper.empty_book("trend4h")
        book["positions"] = [{
            "coin": "AAA", "side": "long", "entry": 10.0, "stop": 1.0,
            "atr": 1.0, "signal_t": 0, "entry_t": 0, "size": 1.0, "risk_usd": 25.0,
        }]
        result = run(
            series, lambda coin, i: {"side": "long", "atr": 1.0}, ["AAA"], book, cfg(),
            STEP, STEP, STEP * 2, {"AAA": 99.0},
        )
        self.assertEqual(len(result["positions"]), 1)
        self.assertAlmostEqual(result["positions"][0]["entry"], 10.0)
        self.assertEqual(result["events"], [])

    def test_trail_uses_the_stop_from_the_open_and_does_not_sink(self):
        rows = [
            (10, 11, 9, 10, 1),     # signal bar
            (100, 101, 99, 100, 1),  # entry, trail stays at the initial stop
            (100, 112, 100, 110, 1),  # new high lifts the stop to 102
            (110, 111, 105, 108, 1),  # wider ATR must not lower it
            (110, 111, 101, 108, 1),  # low tags 102
        ]
        candles = bars(0, rows)
        series = {"AAA": paper.index_series(candles, [2, 2, 2, 4, 2])}

        def once(coin, i):
            return {"side": "long", "atr": 2.0} if i == 0 else None

        result = run(
            series, once, ["AAA"], paper.empty_book("squeeze4h"),
            cfg(book="squeeze4h", t_mult=5, max_hold=50),
            0, 4 * STEP, 5 * STEP, {}, funding={"AAA": []},
        )
        self.assertEqual(result["positions"], [])
        self.assertEqual(result["rows"][0]["exit_reason"], "stop")
        self.assertEqual(result["rows"][0]["exit"], "102")
        self.assertEqual(result["events"][-1]["type"], "squeeze4h_exit")
        self.assertTrue(result["events"][-1]["paper"])


class SqueezeAndTrappedTests(unittest.TestCase):
    def _squeeze(self, close=12, vol=20, bwpct=0.04, high=11):
        candles = []
        for i in range(25):
            candles.append({"t": i, "o": 10, "h": high, "l": 9, "c": 10, "v": 10})
        candles[-1] = {"t": 24, "o": 10, "h": close, "l": 9, "c": close, "v": vol}
        ranks = [0.5] * 25
        ranks[21] = bwpct
        ranks[22] = 0.4
        ranks[23] = 0.4
        return {"candles": candles, "bwpct": ranks}

    def test_squeeze_needs_compression_breakout_and_volume(self):
        self.assertTrue(squeeze4h.trigger(self._squeeze(), 24))
        self.assertFalse(squeeze4h.trigger(self._squeeze(bwpct=0.06), 24))
        self.assertFalse(squeeze4h.trigger(self._squeeze(close=11), 24))
        self.assertFalse(squeeze4h.trigger(self._squeeze(vol=14), 24))
        bad = self._squeeze()
        bad["bwpct"][22] = None
        self.assertFalse(squeeze4h.trigger(bad, 24))

    def _trapped_candles(self):
        candles = []
        for i in range(169):
            cl = 100.0 - 10.0 * (i / 168.0)
            candles.append({
                "t": i * paper.HOUR_MS, "o": cl, "h": cl + 0.1, "l": cl - 0.1, "c": cl, "v": 10.0,
            })
        candles[-1].update({"o": 90.0, "h": 94.0, "l": 89.0, "c": 93.0, "v": 40.0})
        return candles

    def test_trapped_filters_and_warmup_gate(self):
        candles = self._trapped_candles()
        self.assertTrue(trapped_shorts1h.trigger(candles, 168, 120.0, 100.0, -0.001))
        self.assertFalse(trapped_shorts1h.trigger(candles, 168, 110.0, 100.0, -0.001))
        self.assertFalse(trapped_shorts1h.trigger(candles, 168, 120.0, 100.0, 0.0))
        self.assertFalse(trapped_shorts1h.trigger(candles, 168, 120.0, 100.0, None))
        quiet = [dict(c) for c in candles]
        quiet[-1] = dict(quiet[-1], c=91.0, h=92.0)
        self.assertFalse(trapped_shorts1h.trigger(quiet, 168, 120.0, 100.0, -0.001))
        thin = [dict(c) for c in candles]
        thin[-1] = dict(thin[-1], v=20.0)
        self.assertFalse(trapped_shorts1h.trigger(thin, 168, 120.0, 100.0, -0.001))
        gapped = [dict(c) for c in candles]
        gapped[-1] = dict(gapped[-1], t=gapped[-1]["t"] + paper.HOUR_MS)
        self.assertFalse(trapped_shorts1h.trigger(gapped, 168, 120.0, 100.0, -0.001))

        series = {"AAA": trapped_shorts1h.prepare(candles)}
        info = {"AAA": {"oi_now": 120.0, "oi_then": 100.0, "premium": -0.001}}
        cold = trapped_shorts1h.make_signal(series, info, candles[-1]["t"], enabled=False)
        self.assertIsNone(cold("AAA", 168))
        hot = trapped_shorts1h.make_signal(series, info, candles[-1]["t"], enabled=True)
        self.assertEqual(hot("AAA", 168)["side"], "long")
        self.assertIsNone(hot("AAA", 167))


class BookkeepingTests(unittest.TestCase):
    def test_liquid_universe_is_main_dex_and_five_million(self):
        names = paper.liquid_coins([
            {"dex": "main", "coin": "BTC", "dayNtlVlm": 6_000_000},
            {"dex": "main", "coin": "TINY", "dayNtlVlm": 4_999_999},
            {"dex": "xyz", "coin": "BIG", "dayNtlVlm": 9_000_000_000},
            {"dex": "main", "coin": "AAA", "dayNtlVlm": 5_000_000, "isDelisted": True},
        ])
        self.assertEqual(names, ["BTC"])

    def test_listing_splits_delisted_names(self):
        raw = [{"universe": [
            {"name": "BTC"},
            {"name": "MKR", "isDelisted": True},
        ]}]
        listed, delisted = paper.main_listing(raw)
        self.assertEqual(listed, {"BTC"})
        self.assertEqual(delisted, {"MKR"})

    def test_closed_candle_split(self):
        now = 5 * paper.FOUR_H_MS + 120_000
        candles = [
            {"t": 0, "o": 1, "h": 2, "l": 1, "c": 1, "v": 1},
            {"t": paper.FOUR_H_MS, "o": 1, "h": 2, "l": 1, "c": 1, "v": 1},
            {"t": 4 * paper.FOUR_H_MS, "o": 3, "h": 4, "l": 1, "c": 2, "v": 1},
            {"t": 5 * paper.FOUR_H_MS, "o": 2, "h": 3, "l": 1, "c": 2, "v": 1},
        ]
        closed, forming = paper.split_candles(candles, now, paper.FOUR_H_MS)
        self.assertEqual([c["t"] for c in closed], [0, paper.FOUR_H_MS, 4 * paper.FOUR_H_MS])
        self.assertEqual(forming["t"], 5 * paper.FOUR_H_MS)
        self.assertEqual(paper.latest_closed_open(now, paper.FOUR_H_MS), 4 * paper.FOUR_H_MS)

    def test_weekly_window_is_monday_at_08_utc_once(self):
        monday = datetime(2026, 11, 16, 8, 2, tzinfo=timezone.utc)
        self.assertTrue(paper.weekly_due(monday, None))
        self.assertFalse(paper.weekly_due(monday, "2026-11-16"))
        self.assertFalse(paper.weekly_due(monday.replace(hour=9), None))
        self.assertFalse(paper.weekly_due(monday - timedelta(days=1), None))

    def test_market_log_dedupes_the_hour_and_oi_lookup(self):
        now = datetime(2026, 11, 16, 14, 2, tzinfo=timezone.utc)
        old = now - timedelta(hours=168)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            asset = {
                "dex": "main", "coin": "BTC", "mark": 10, "oi_coins": 5,
                "oi_usd": 50, "funding": 0.0001, "premium": -0.002, "dayNtlVlm": 8_000_000,
            }
            self.assertTrue(paper.append_market_log([asset], old, directory))
            self.assertFalse(paper.append_market_log([asset], old, directory))
            later = dict(asset, oi_coins=8)
            self.assertTrue(paper.append_market_log([later], now, directory))
            with (directory / f"{now:%Y-%m-%d}.csv").open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["oi"], "8")
            self.assertIn("oi_usd", rows[0])
            self.assertTrue(paper.spans_warmup(directory, 168))
            target = int(now.timestamp() * 1000) - 168 * paper.HOUR_MS
            # old was 14:02 floored to 14:00, target is 14:00 seven days earlier.
            self.assertEqual(paper.oi_near(directory, "BTC", target), 5.0)


if __name__ == "__main__":
    unittest.main()
