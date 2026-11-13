#!/usr/bin/env python3
"""Confluence rules, tape coverage on ack, and the one live webhook."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import confluence
import detector
import trade_tape
from test_detector import NOW, asset, load_cfg, snap

H = 3_600_000
M15 = 900_000


def _series(n, step, start, t0, vol=10.0):
    out = []
    price = start
    for i in range(n):
        price += step
        out.append({
            "t": t0 + i * H, "o": price - 0.2, "h": price + 0.4,
            "l": price - 0.8, "c": price, "v": vol,
        })
    return out


def _book(coin, start, end, buy=1_000.0, sell=100.0, run="run1"):
    flow = {}
    bucket = (start // M15) * M15
    while bucket < end:
        flow[str(int(bucket))] = {run: {"buy": buy, "sell": sell}}
        bucket += M15
    return {"cov": {coin: {run: [[start, end]]}}, "flow": {coin: flow}}


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_cfg()

    def _long(self):
        prior = _series(70, 0.3, 80.0, 0)
        level = max(c["h"] for c in prior[-20:])
        close = level + 0.8
        bar = {"t": prior[-1]["t"] + H, "o": level, "h": close, "l": level - 0.2, "c": close, "v": 40}
        delta = {"ok": True, "buy": 5_000, "sell": 1_000, "delta": 4_000, "coverage": 1, "max_gap_ms": 0}
        return prior, bar, close, delta

    def test_long_needs_all_three_and_stops_beyond_the_base(self):
        prior, bar, close, delta = self._long()
        got = confluence.score_bar(prior, bar, close, asset("BTC", mark=close, day=6_000_000), delta, self.cfg)
        self.assertEqual(got["status"], "alerta")
        self.assertEqual(got["side"], "long")
        self.assertGreaterEqual(got["vol_mult"], 1.5)
        self.assertGreater(got["ema20"], got["ema50"])
        self.assertLessEqual(got["sl"], got["donchian"])
        self.assertGreaterEqual(close - got["sl"], 2 * got["atr"] - 1e-6)
        self.assertIn(got["tp_kind"], ("estructural", "mixto", "medido"))
        self.assertNotEqual(got["tp_kind"], "2r")
        thin = dict(bar, v=10)
        self.assertEqual(confluence.score_bar(prior, thin, close, asset("BTC", mark=close, day=6e6), delta, self.cfg)["status"], "no")
        flat = dict(delta, buy=1, sell=1, delta=0)
        self.assertEqual(confluence.score_bar(prior, bar, close, asset("BTC", mark=close, day=6e6), flat, self.cfg)["status"], "sin_presion")
        missing = {"ok": False, "reason": "sin cinta", "coverage": 0, "buy": 0, "sell": 0, "delta": 0, "max_gap_ms": 0}
        stood_in = confluence.score_bar(prior, bar, close, asset("BTC", mark=close, day=6e6), missing, self.cfg)
        self.assertEqual(stood_in["status"], "alerta")
        self.assertEqual(stood_in["delta_source"], "close_location")
        self.assertIsNone(stood_in["delta"])
        self.assertGreater(stood_in["close_location"], 0)
        # Still a breakout, but the close sits in the lower half of the bar.
        weak = dict(bar, h=close + 3.0, l=close - 0.5, c=close - 0.4)
        rejected = confluence.score_bar(prior, weak, weak["c"], asset("BTC", mark=weak["c"], day=6e6), missing, self.cfg)
        self.assertEqual(rejected["status"], "sin_presion")
        self.assertEqual(rejected["delta_source"], "close_location")

    def test_extension_above_two_atr_does_not_alert(self):
        prior, _bar, _close, delta = self._long()
        level = max(c["h"] for c in prior[-20:])
        atr = __import__("setup_scanner").wilder_atr(prior, 14)[-1]
        close = level + 2.2 * atr
        bar = {"t": prior[-1]["t"] + H, "o": level, "h": close, "l": level, "c": close, "v": 80}
        got = confluence.score_bar(prior, bar, close, asset("BTC", mark=close, day=6e6), delta, self.cfg)
        self.assertEqual(got["status"], "extendido")

    def test_short_funding_veto(self):
        prior = _series(70, -0.3, 120.0, 0)
        level = min(c["l"] for c in prior[-20:])
        close = level - 0.8
        bar = {"t": prior[-1]["t"] + H, "o": level, "h": level + 0.2, "l": close, "c": close, "v": 40}
        delta = {"ok": True, "buy": 100, "sell": 5_000, "delta": -4900, "coverage": 1, "max_gap_ms": 0}
        good = asset("ETH", mark=close, day=8_000_000, funding=-0.00005)
        got = confluence.score_bar(prior, bar, close, good, delta, self.cfg)
        self.assertEqual(got["status"], "alerta")
        self.assertEqual(got["side"], "short")
        self.assertGreaterEqual(got["sl"], got["donchian"])
        risk = got["sl"] - close
        self.assertGreater(risk, 0)
        self.assertAlmostEqual(got["tp1"], close - 2 * risk)
        self.assertAlmostEqual(got["tp2"], close - 3 * risk)
        self.assertEqual(got["tp_kind"], "2r")
        self.assertEqual(got["rr"], 2.0)
        self.assertEqual(got["rr_tp2"], 3.0)
        crowded = asset("ETH", mark=close, day=8_000_000, funding=-0.0002)
        self.assertEqual(confluence.score_bar(prior, bar, close, crowded, delta, self.cfg)["status"], "veto_funding")
        blind = asset("ETH", mark=close, day=8_000_000, funding=None)
        self.assertEqual(confluence.score_bar(prior, bar, close, blind, delta, self.cfg)["status"], "veto_funding")

    def test_replay_does_not_pretend_delta_existed(self):
        prior, bar, close, _delta = self._long()
        got = confluence.score_bar(prior, bar, close, asset("BTC", mark=close, day=6e6), None, self.cfg, require_delta=False)
        self.assertEqual(got["status"], "alerta")
        self.assertIsNone(got["delta"])

    def test_walk_prefers_the_stop_when_a_bar_trades_both(self):
        both = [{"h": 10, "l": 9, "c": 10}, {"h": 12, "l": 7, "c": 8}]
        self.assertEqual(confluence.walk_outcome(both, 0, "long", 10, 8, 11), "sl")
        up = [{"h": 10, "l": 9, "c": 10}, {"h": 12, "l": 9.5, "c": 11.5}]
        self.assertEqual(confluence.walk_outcome(up, 0, "long", 10, 8, 11.2), "tp1")
        quiet = [{"h": 10, "l": 9, "c": 10}, {"h": 10.2, "l": 9.5, "c": 10}]
        self.assertEqual(confluence.walk_outcome(quiet, 0, "long", 10, 8, 12), "open")

    def test_coverage_gap_and_run_merge(self):
        fine = confluence.coverage_stats([[0, 1_000_000], [1_120_000, H]], 0, H)
        self.assertGreaterEqual(fine[0], 0.95)
        self.assertLessEqual(fine[1], 180_000)
        hole = confluence.coverage_stats([[0, 1_000_000], [1_240_000, H]], 0, H)
        self.assertLess(hole[0], 0.95)
        self.assertTrue(confluence.measure_delta(_book("BTC", 0, H), "BTC", 0, H, self.cfg)["ok"])
        self.assertFalse(confluence.measure_delta(confluence.empty_delta(), "BTC", 0, H, self.cfg)["ok"])
        tape = confluence.Tape("run1")
        tape.note_subscribed("BTC", 1_000)
        tape.add_trade("BTC", 1_000, "B", 10, 2, 5_000)
        state = confluence.merge_tape(None, tape.snapshot(5_000), 5_000)
        tape.add_trade("BTC", 1_000, "B", 10, 3, 6_000)
        state = confluence.merge_tape(state, tape.snapshot(6_000), 6_000)
        self.assertEqual(state["flow"]["BTC"]["0"]["run1"]["buy"], 50)
        other = confluence.Tape("run2")
        other.note_subscribed("BTC", 10_000)
        other.add_trade("BTC", 10_000, "A", 10, 1, 20_000)
        state = confluence.merge_tape(state, other.snapshot(20_000), 20_000)
        measured = confluence.measure_delta(state, "BTC", 0, M15, self.cfg)
        self.assertEqual(measured["buy"], 50)
        self.assertEqual(measured["sell"], 10)

    def test_coverage_starts_on_ack_not_on_the_print(self):
        tape = confluence.Tape("run1")
        now = 10_000
        trade = {"coin": "BTC", "side": "B", "px": "10", "sz": "1", "time": now - 5_000}
        trade_tape.handle_message(tape, {"channel": "trades", "data": [trade]}, now)
        self.assertEqual(tape.flow, {})
        self.assertNotIn("BTC", tape.open_since)
        # A print timestamped before the ack is still dropped after the ack.
        trade_tape.handle_message(tape, {
            "channel": "subscriptionResponse",
            "data": {"subscription": {"type": "trades", "coin": "BTC"}},
        }, now + 500)
        self.assertEqual(tape.cov["BTC"][0][0], now + 500)
        trade_tape.handle_message(tape, {"channel": "trades", "data": [trade]}, now + 500)
        self.assertEqual(tape.flow, {})
        later = {"coin": "BTC", "side": "B", "px": "10", "sz": "2", "time": now + 800}
        trade_tape.handle_message(tape, {"channel": "trades", "data": [later]}, now + 800)
        bucket = str((now + 800) // M15 * M15)
        self.assertEqual(tape.flow["BTC"][bucket]["buy"], 20)
        erred = confluence.Tape("run2")
        trade_tape.handle_message(erred, {
            "channel": "subscriptionResponse",
            "data": {"error": "no", "subscription": {"type": "trades", "coin": "ETH"}},
        }, now)
        self.assertNotIn("ETH", erred.open_since)

    def test_universe_skips_spot_and_a_thin_hip3_book(self):
        self.assertTrue(confluence.in_universe(asset("BTC", day=5_000_000), self.cfg))
        self.assertFalse(confluence.in_universe(asset("BTC", day=4_999_999), self.cfg))
        self.assertFalse(confluence.in_universe(asset("HYPE/USDC", day=9_000_000), self.cfg))
        self.assertFalse(confluence.in_universe(asset("xyz:GOLD", dex="xyz", day=6_000_000, oi=999_999), self.cfg))
        self.assertTrue(confluence.in_universe(asset("xyz:GOLD", dex="xyz", day=6_000_000, oi=1_000_000), self.cfg))

    def test_websocket_frames_reassemble(self):
        payload = b'{"channel":"trades","data":[]}'
        raw = bytes([0x81, len(payload)]) + payload
        frames, _rest = trade_tape.pop_frames(raw[:4])
        self.assertEqual(frames, [])
        frames, rest = trade_tape.pop_frames(raw)
        self.assertEqual(rest, b"")
        self.assertEqual(frames[0][2], payload)


def _fifteen_down(start_ms, close, vol):
    bars = []
    price = close + 0.6
    for i in range(4):
        price -= 0.2
        bars.append({
            "t": start_ms + i * M15, "o": price + 0.2, "h": price + 0.3,
            "l": price - 0.1, "c": price, "v": vol,
        })
    bars[-1]["c"] = close
    bars[-1]["l"] = min(bars[-1]["l"], close)
    return bars


def _fifteen(start_ms, close, vol):
    bars = []
    price = close - 0.6
    for i in range(4):
        price += 0.2
        bars.append({
            "t": start_ms + i * M15, "o": price - 0.2, "h": price + 0.1,
            "l": price - 0.3, "c": price, "v": vol,
        })
    bars[-1]["c"] = close
    bars[-1]["h"] = max(bars[-1]["h"], close)
    return bars


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "data").mkdir()
        (root / "signals").mkdir()
        (root / "logs").mkdir()
        self._saved = (detector.time.time, detector.scan, detector.candles, detector.send,
                       detector.STATE, detector.ALERTS, detector.LOG, detector.datetime)
        detector.STATE = root / "data" / "detector_state.json"
        detector.ALERTS = root / "signals" / "explosion_alerts.jsonl"
        detector.LOG = root / "logs" / "detector.log"
        detector.time.time = lambda: NOW
        self.calls = []
        detector.send = lambda payload, cfg: self.calls.append(payload) or "posted"
        self.books = {}

        def candles(coin, interval, n_bars):
            return self.books[(coin, interval)]

        detector.candles = candles
        self.freeze(12)

    def tearDown(self):
        (detector.time.time, detector.scan, detector.candles, detector.send,
         detector.STATE, detector.ALERTS, detector.LOG, detector.datetime) = self._saved
        self.tmp.cleanup()

    def freeze(self, hour):
        fixed = datetime(2026, 6, 15, hour, 30, tzinfo=detector.MADRID)

        class Fixed(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return fixed.replace(tzinfo=None)
                return fixed.astimezone(tz)

        detector.datetime = Fixed

    def _books_for(self, coin, shift=0.8, vol=40.0):
        now_ms = NOW * 1000
        hours = _series(70, 0.3, 80.0, now_ms - 69 * H)
        level = max(c["h"] for c in hours if c["t"] + H <= now_ms - H)
        close = level + shift
        self.books[(coin, "1h")] = hours
        self.books[(coin, "15m")] = _fifteen(now_ms - H, close, vol) + [
            {"t": now_ms, "o": close, "h": close, "l": close, "c": close, "v": 1},
        ]
        return close

    def _books_short(self, coin, shift=0.8, vol=40.0):
        now_ms = NOW * 1000
        hours = _series(70, -0.3, 120.0, now_ms - 69 * H)
        level = min(c["l"] for c in hours if c["t"] + H <= now_ms - H)
        close = level - shift
        self.books[(coin, "1h")] = hours
        self.books[(coin, "15m")] = _fifteen_down(now_ms - H, close, vol) + [
            {"t": now_ms, "o": close, "h": close, "l": close, "c": close, "v": 1},
        ]
        return close

    def cycle(self, assets, state=None):
        detector.scan = lambda: snap(assets)
        if state is not None:
            detector.STATE.write_text(json.dumps(state), encoding="utf-8")
        return detector.run_cycle(load_cfg())

    def test_posts_a_batch_as_one_wake(self):
        btc = self._books_short("BTC")
        eth = self._books_short("ETH")
        start, end = (NOW - 2 * 3600) * 1000, (NOW + 60) * 1000
        delta = {"cov": {}, "flow": {}}
        for coin in ("BTC", "ETH"):
            part = _book(coin, start, end, buy=100.0, sell=5_000.0)
            delta["cov"].update(part["cov"])
            delta["flow"].update(part["flow"])
        self.assertTrue(self.cycle([
            asset("BTC", mark=btc, pct_24h=-0.4, day=6_000_000),
            asset("ETH", mark=eth, pct_24h=-0.4, day=7_000_000),
            asset("xyz:THIN", dex="xyz", mark=eth, pct_24h=-0.4, day=6_000_000, oi=100_000),
        ], state={"marks": [], "last_fire": {}, "wakes": {}, "delta": delta}))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual({item["coin"] for item in self.calls[0]}, {"BTC", "ETH"})
        item = self.calls[0][0]
        self.assertEqual(item["type"], "confluence_1h")
        self.assertEqual(item["side"], "short")
        for key in ("ema20", "ema50", "donchian", "vol_mult", "delta", "mark", "dayNtlVlm",
                    "oi_usd", "funding_pct", "premium_pct", "atr", "entry", "sl", "tp1", "tp2", "rr"):
            self.assertIsNotNone(item[key])
        risk = item["sl"] - item["entry"]
        self.assertAlmostEqual(item["tp1"], item["entry"] - 2 * risk)
        self.assertAlmostEqual(item["tp2"], item["entry"] - 3 * risk)
        self.assertEqual(item["rr"], 2.0)
        self.assertEqual(item["rr_tp2"], 3.0)
        self.assertEqual(item["tp_kind"], "2r")
        self.assertEqual(json.loads(detector.STATE.read_text())["confluence_wakes"]["2026-06-15"], 1)

    def test_long_is_logged_and_does_not_wake_or_block_a_short(self):
        import csv
        close = self._books_for("BTC")
        short = self._books_short("ETH")
        self.assertTrue(self.cycle([
            asset("BTC", mark=close, pct_24h=0.4, day=6_000_000),
            asset("ETH", mark=short, pct_24h=-0.4, day=7_000_000),
        ]))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([item["coin"] for item in self.calls[0]], ["ETH"])
        self.assertEqual(self.calls[0][0]["side"], "short")
        path = detector.STATE.parent / "confluence" / "2026-06-15.csv"
        with path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        longs = [row for row in rows if row["coin"] == "BTC"]
        self.assertEqual([row["status"] for row in longs], ["shadow_long"])
        st = json.loads(detector.STATE.read_text())
        self.assertEqual(st["confluence_wakes"]["2026-06-15"], 1)
        self.assertNotIn("BTC", st["confluence_fire"])
        self.assertIn("ETH", st["confluence_fire"])

        self.calls.clear()
        path.unlink()
        detector.send = lambda payload, cfg: (_ for _ in ()).throw(AssertionError("el largo no se envía"))
        self.assertTrue(self.cycle([
            asset("BTC", mark=close, pct_24h=0.4, day=6_000_000),
        ], state={"marks": [], "last_fire": {}, "wakes": {}, "confluence_wakes": {"2026-06-15": 8}}))
        with path.open(encoding="utf-8") as fh:
            self.assertEqual(list(csv.DictReader(fh))[-1]["status"], "shadow_long")
        st = json.loads(detector.STATE.read_text())
        self.assertEqual(st["confluence_wakes"]["2026-06-15"], 8)
        self.assertEqual(st.get("confluence_fire") or {}, {})

    def test_missing_delta_quiet_cap_and_failed_post(self):
        import csv
        close = self._books_short("BTC")
        self.assertTrue(self.cycle([asset("BTC", mark=close, pct_24h=0.2, day=6_000_000)]))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0]["delta_source"], "close_location")
        self.assertIsNone(self.calls[0][0]["delta"])
        self.calls.clear()
        path = detector.STATE.parent / "confluence" / "2026-06-15.csv"
        with path.open(encoding="utf-8") as fh:
            self.assertEqual(list(csv.DictReader(fh))[0]["delta_source"], "close_location")

        start, end = (NOW - 2 * 3600) * 1000, (NOW + 60) * 1000
        delta = _book("BTC", start, end, buy=100.0, sell=5_000.0)
        self.freeze(3)
        detector.send = lambda payload, cfg: (_ for _ in ()).throw(AssertionError("noche"))
        self.assertTrue(self.cycle([
            asset("BTC", mark=close, pct_24h=0.2, day=6_000_000),
        ], state={"marks": [], "last_fire": {}, "wakes": {}, "delta": delta}))
        self.assertEqual(json.loads(detector.STATE.read_text())["confluence_fire"], {})

        self.freeze(12)
        path.unlink()
        detector.send = lambda payload, cfg: (_ for _ in ()).throw(AssertionError("tope"))
        self.assertTrue(self.cycle([
            asset("BTC", mark=close, pct_24h=0.2, day=6_000_000),
        ], state={"marks": [], "last_fire": {}, "wakes": {}, "confluence_wakes": {"2026-06-15": 8}, "delta": delta}))
        with path.open(encoding="utf-8") as fh:
            self.assertEqual(list(csv.DictReader(fh))[-1]["status"], "tope")

        detector.send = lambda payload, cfg: "post_failed"
        self.assertTrue(self.cycle([
            asset("BTC", mark=close, pct_24h=0.2, day=6_000_000),
        ], state={"marks": [], "last_fire": {}, "wakes": {}, "delta": delta}))
        st = json.loads(detector.STATE.read_text())
        self.assertEqual(st.get("confluence_wakes") or {}, {})
        self.assertEqual(st["confluence_fire"], {})


class ReplayAuditTests(unittest.TestCase):
    def test_funding_print_after_the_close_is_this_hour_only(self):
        import confluence_replay as replay
        close = 3_600_000
        series = [
            (close - 3_600_000 + 26, 0.001, 0.01),
            (close + 26, 0.002, 0.2),
            (close + 3_600_000 + 26, 0.05, 1.0),
        ]
        self.assertEqual(replay.funding_at(series, close), (0.002, 0.2))

    def test_costs_come_off_both_sides_and_a_stop_is_negative(self):
        import confluence_replay as replay
        # Planned 1R stop. Costs make a stop slightly worse than -1.
        lost = replay.net_r("long", 100, 90, 90)
        self.assertLess(lost, -1)
        won = replay.net_r("long", 100, 120, 90)
        self.assertLess(won, 2)
        self.assertGreater(won, 0)

    def test_upper_wick_and_hot_funding_mark_exhaustion(self):
        import confluence_replay as replay
        bar = {"o": 10, "h": 12, "l": 10, "c": 10.2}
        self.assertGreater(replay.upper_wick_pct(bar), 50)
        kind, reasons = replay.short_rule({
            "signal_bar": bar, "funding_pct": 0.02, "premium_pct": 0.05,
        })
        self.assertEqual(kind, "agotamiento")
        self.assertIn("mecha", reasons)
        self.assertIn("funding", reasons)
        plain, why = replay.short_rule({
            "signal_bar": {"o": 10, "h": 10.4, "l": 9, "c": 9.2},
            "funding_pct": 0.001, "premium_pct": 0.01,
        })
        self.assertEqual(plain, "ruptura")
        self.assertEqual(why, [])

    def test_path_starts_after_the_signal_and_a_double_touch_is_a_stop(self):
        signal = {"h": 11, "l": 8, "c": 10}
        both = {"h": 12, "l": 7, "c": 9}
        later = {"h": 13, "l": 9.5, "c": 12}
        self.assertEqual(
            confluence.walk_outcome([signal, both, later], 0, "long", 10, 8, 11),
            "sl",
        )


if __name__ == "__main__":
    unittest.main()
