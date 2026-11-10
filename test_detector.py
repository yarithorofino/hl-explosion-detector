#!/usr/bin/env python3
"""Unit tests. They must not call Hyperliquid or any webhook."""
import contextlib
import csv
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import detector
import perps_all

NOW = 1_800_000_000
SECRET_URL = "http://127.0.0.1:9/super-secret-path"
SECRET_AUTH = "Authorization: Bearer SUPERSECRETTOKEN"
ENV_KEYS = (
    "EXPLOSION_WEBHOOK_URL",
    "EXPLOSION_WEBHOOK_AUTH",
    "EXPLOSION_WEBHOOK_KEY",
    "FORCE_TEST_POST",
    "GITHUB_STEP_SUMMARY",
)


def load_cfg(**over):
    cfg = json.loads(detector.CFG.read_text(encoding="utf-8"))
    cfg.update(over)
    return cfg


def asset(coin, mark=100.0, pct_24h=20.0, day=2_000_000.0, dex="main", oi=1_000_000.0,
          funding=0.0000125, premium=-0.0003642):
    return {
        "dex": dex, "coin": coin, "mark": mark, "prevDayPx": mark, "oracle": mark,
        "pct_24h": pct_24h, "dayNtlVlm": day, "oi_coins": 1.0,
        "oi_usd": None if oi is None else oi,
        "funding": funding,
        "funding_pct": None if funding is None else round(funding * 100, 6),
        "premium": premium,
        "premium_pct": None if premium is None else round(premium * 100, 4),
        "maxLeverage": 10,
    }


def snap(assets, errors=None):
    return {"assets": assets, "errors": errors or [], "count": len(assets), "dexs": ["main"], "ts": NOW}


def bars(n, last_pct, last_v, base_v=10.0, price=100.0, step_ms=3_600_000, high_extra=0.0, low_extra=0.0):
    cs = []
    t0 = NOW * 1000 - (n - 1) * step_ms
    for i in range(n):
        o = c = price
        h = l = price
        v = base_v
        if i == n - 1:
            c = price * (1 + last_pct / 100.0)
            h = max(o, c) + high_extra
            l = min(o, c) - low_extra
            v = last_v
        cs.append({"t": t0 + i * step_ms, "o": o, "h": h, "l": l, "c": c, "v": v})
    return cs


def hit_1h():
    return bars(40, last_pct=10.0, last_v=2000.0)


def quiet_15m():
    return bars(40, last_pct=0.2, last_v=10.0, step_ms=900_000)


def route(hour=None, quarter=None):
    def candles(coin, interval, n_bars):
        if interval == "15m":
            return quiet_15m() if quarter is None else quarter
        return hit_1h() if hour is None else hour
    return candles


class Resp(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(data)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def http_error(code, retry_after=None):
    hdrs = EmailMessage()
    if retry_after is not None:
        hdrs["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(
        "https://api.hyperliquid.xyz/info", code, "err", hdrs, io.BytesIO(b"")
    )


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "data").mkdir()
        (root / "signals").mkdir()
        (root / "logs").mkdir()
        self._saved = {
            "time": detector.time.time,
            "sleep": detector.time.sleep,
            "dt": detector.datetime,
            "scan": detector.scan,
            "candles": detector.candles,
            "send": detector.send,
            "post": detector.post,
            "state": detector.STATE,
            "alerts": detector.ALERTS,
            "log": detector.LOG,
            "urlopen": urllib.request.urlopen,
            "p_post": perps_all.post,
            "p_sleep": perps_all.time.sleep,
            "p_spent": list(perps_all._spent),
            "p_blocked": perps_all._blocked_until,
            "p_next": perps_all._next_ok,
        }
        self._env = {k: os.environ.get(k) for k in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        detector.STATE = root / "data" / "detector_state.json"
        detector.ALERTS = root / "signals" / "explosion_alerts.jsonl"
        detector.LOG = root / "logs" / "detector.log"
        detector.time.time = lambda: NOW
        detector.time.sleep = lambda *_a, **_k: None
        perps_all.time.sleep = lambda *_a, **_k: None

        def no_net(*_a, **_k):
            raise AssertionError("el test ha intentado llamar a Hyperliquid")

        detector.post = no_net
        perps_all.post = no_net
        self.freeze(12)

    def tearDown(self):
        detector.time.time = self._saved["time"]
        detector.time.sleep = self._saved["sleep"]
        detector.datetime = self._saved["dt"]
        detector.scan = self._saved["scan"]
        detector.candles = self._saved["candles"]
        detector.send = self._saved["send"]
        detector.post = self._saved["post"]
        detector.STATE = self._saved["state"]
        detector.ALERTS = self._saved["alerts"]
        detector.LOG = self._saved["log"]
        urllib.request.urlopen = self._saved["urlopen"]
        perps_all.post = self._saved["p_post"]
        perps_all.time.sleep = self._saved["p_sleep"]
        perps_all._spent = self._saved["p_spent"]
        perps_all._blocked_until = self._saved["p_blocked"]
        perps_all._next_ok = self._saved["p_next"]
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
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

    def cycle(self, assets, candle_fn=None, cfg=None, state=None):
        detector.scan = lambda: snap(assets)
        detector.candles = candle_fn or route()
        if state is not None:
            detector.STATE.write_text(json.dumps(state), encoding="utf-8")
        use = load_cfg()
        if cfg:
            use.update(cfg)
        # These scans are about the explosion rules. Extra liquid names and
        # the live trade tape are covered by their own tests.
        use["early_extra_names"] = 0
        use["early_rotate_names"] = 0
        use["cvd_max_coins"] = 0
        return detector.run_cycle(use)

    def read_state(self):
        return json.loads(detector.STATE.read_text(encoding="utf-8"))

    def alerts(self):
        if not detector.ALERTS.exists():
            return []
        return [json.loads(ln) for ln in detector.ALERTS.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def latest(self):
        return (detector.STATE.parent / "scans" / "latest.md").read_text(encoding="utf-8")

    def test_config_thresholds(self):
        cfg = load_cfg()
        self.assertEqual(cfg["atr_period"], 14)
        self.assertEqual(cfg["atr_mult"], 3.0)
        self.assertEqual(cfg["floor_pct_major"], 3.0)
        self.assertEqual(cfg["floor_pct_other"], 5.0)
        self.assertEqual(cfg["major_coins"], ["BTC", "ETH", "SOL"])
        self.assertEqual(cfg["major_day_ntl_usd"], 100_000_000)
        self.assertEqual(cfg["vol_mult_min"], 5.0)
        self.assertEqual(cfg["vol_1h_min_usd"], 100_000)
        self.assertEqual(cfg["vol_15m_min_usd"], 25_000)
        self.assertEqual(cfg["pct_15m_min"], 4.0)
        self.assertEqual(cfg["liquidity_min_day_ntl_usd"], 300_000)
        self.assertEqual(cfg["hip3_min_oi_usd"], 500_000)
        self.assertEqual(cfg["tradable_min_day_ntl_usd"], 1_000_000)
        self.assertEqual(cfg["max_alerts_per_day"], 10)
        self.assertEqual(cfg["legacy_pct_1h"], 8.0)
        self.assertEqual(cfg["escalera_pct"], 10.0)
        self.assertEqual(cfg["escalera_candles"], 3)
        self.assertEqual(cfg["escalera_min_aligned"], 2)
        self.assertFalse(cfg["escalera_ignore_quiet_hours"])
        self.assertEqual(cfg["prefilter_pct_3h"], 6.0)
        self.assertEqual(cfg["max_alerts_per_hour"], 2)
        self.assertEqual(cfg["cooldown_hours"], 4)
        self.assertEqual(cfg["quiet_start_hour"], 0)
        self.assertEqual(cfg["quiet_end_hour"], 8)
        self.assertEqual(cfg["prefilter_pct_15m"], 3.0)
        self.assertEqual(cfg["scan_keep_days"], 14)

    def test_floor_matches_the_agreed_rule(self):
        cfg = load_cfg()
        self.assertEqual(detector.floor_for(asset("BTC", day=1_000), cfg), 3.0)
        self.assertEqual(detector.floor_for(asset("xyz:GOLD", dex="xyz", day=5_000_000_000, oi=2_000_000), cfg), 5.0)
        self.assertEqual(detector.floor_for(asset("DOGE", dex="main", day=100_000_000), cfg), 3.0)
        self.assertEqual(detector.floor_for(asset("DOGE", dex="main", day=99_000_000), cfg), 5.0)
        self.assertFalse(detector.passes_liquidity(asset("TINY", day=299_000), cfg))
        self.assertFalse(detector.passes_liquidity(asset("HIP", dex="xyz", day=2_000_000, oi=499_000), cfg))
        self.assertTrue(detector.passes_liquidity(asset("HIP", dex="xyz", day=2_000_000, oi=500_000), cfg))

    def test_atr_multiple_blocks_a_noisy_name(self):
        cs = []
        t0 = NOW * 1000 - 39 * 3_600_000
        for i in range(40):
            o = c = 100.0
            h, l, v = 110.0, 90.0, 10.0
            if i == 39:
                c, h, l, v = 106.0, 106.0, 100.0, 2000.0
            cs.append({"t": t0 + i * 3_600_000, "o": o, "h": h, "l": l, "c": c, "v": v})
        view = detector.series_view(cs, "1h", load_cfg(), 5.0)
        self.assertGreater(abs(view["pct"]), 5)
        self.assertFalse(view["hit"])

    def test_wick_ratio_is_the_rejection_wick(self):
        up = {"o": 100, "h": 130, "l": 100, "c": 110, "v": 1}
        down = {"o": 100, "h": 100, "l": 70, "c": 90, "v": 1}
        self.assertGreater(detector.wick_ratio_pct(up), 50)
        self.assertGreater(detector.wick_ratio_pct(down), 50)
        held = {"o": 100, "h": 110, "l": 99, "c": 110, "v": 1}
        self.assertLess(detector.wick_ratio_pct(held), 50)

    def test_dry_run_alert_carries_the_new_fields(self):
        state = {"marks": [{"ts": NOW - 3600, "m": {"AAA": 100.0}, "oi": {"AAA": 100.0}}],
                 "last_fire": {}, "wakes": {}}
        self.assertTrue(self.cycle([asset("AAA", oi=150.0)], state=state))
        item = self.alerts()[0]["alerts"][0]
        self.assertEqual(item["trigger"], "1h_atr+1h_legacy")
        self.assertEqual(item["floor_pct"], 5.0)
        self.assertGreaterEqual(abs(item["pct_1h"]), 5)
        self.assertGreaterEqual(item["vol_1h_usd"], 100_000)
        self.assertGreaterEqual(item["vol_mult"], 5)
        self.assertEqual(item["dex"], "main")
        self.assertEqual(item["funding_pct"], 0.00125)
        self.assertEqual(item["premium_pct"], -0.0364)
        self.assertEqual(item["oi_usd"], 150.0)
        self.assertEqual(item["oi_change_1h_pct"], 50.0)
        self.assertFalse(item["exhaustion_flag"])
        self.assertIsNone(item["exhaustion_label"])
        self.assertTrue(item["tradable"])
        self.assertEqual(item["label"], "operable")
        self.assertIn("ATR %", self.latest())
        self.assertIn("alerta", self.latest())
        st = self.read_state()
        self.assertEqual(st["wakes"]["2026-06-15"], 1)
        self.assertEqual(st["wakes_hour"]["2026-06-15T12"], 1)
        self.assertEqual(st["last_fire"]["AAA"], NOW)
        self.assertIn("status=dry_run", detector.LOG.read_text(encoding="utf-8"))

    def test_small_book_is_flagged_not_tradable(self):
        self.assertTrue(self.cycle([asset("MEME", day=400_000)]))
        item = self.alerts()[0]["alerts"][0]
        self.assertFalse(item["tradable"])
        self.assertEqual(item["label"], "vigilancia, no operable")

    def test_major_floor_is_3_percent_and_the_rest_stay_at_5(self):
        seen = []

        def candles(coin, interval, n_bars):
            seen.append((coin, interval))
            if interval == "1h":
                return bars(40, last_pct=4.0, last_v=2000.0)
            return quiet_15m()

        self.assertTrue(self.cycle([
            asset("BTC", pct_24h=10),
            asset("MEME", pct_24h=10),
        ], candle_fn=candles))
        by_coin = {a["coin"]: a for a in self.alerts()[0]["alerts"]}
        self.assertIn("BTC", by_coin)
        self.assertNotIn("MEME", by_coin)
        self.assertEqual(by_coin["BTC"]["floor_pct"], 3.0)
        self.assertEqual(by_coin["BTC"]["trigger"], "1h_atr")
        self.assertNotIn("1h_legacy", by_coin["BTC"]["trigger"])
        text = self.latest()
        self.assertIn("alerta", text)
        self.assertIn("no llega", text)

    def test_repeated_absorption_opens_a_breakout_watch(self):
        def candles(coin, interval, n_bars):
            if interval == "15m":
                return quiet_15m()
            cs = []
            price = 100.0
            for i in range(80):
                o = price
                c = price * (1.012 if i % 2 == 0 else 0.988)
                h = max(o, c) * 1.002
                l = min(o, c) * 0.998
                cs.append({"t": i * 3_600_000, "o": o, "h": h, "l": l, "c": c, "v": 10.0})
                price = c
            # The confirming spike is the hour that just closed. The live hour is quiet.
            for idx in (len(cs) - 9, len(cs) - 2):
                o = cs[idx]["o"]
                cs[idx] = {
                    "t": cs[idx]["t"], "o": o, "h": o * 1.004, "l": o * 0.998,
                    "c": o * 1.003, "v": 200.0,
                }
            return cs

        self.assertTrue(self.cycle(
            [asset("SOPH", pct_24h=10, day=1_270_000)], candle_fn=candles,
        ))
        item = self.alerts()[0]["alerts"][0]
        self.assertEqual(item["trigger"], "absorption")
        self.assertEqual(item["urgency"], "baja")
        self.assertEqual(item["side"], "long")
        self.assertGreaterEqual(item["absorption_repeats"], 2)
        self.assertGreaterEqual(item["absorption_rvol"], 8)
        watches = [w for w in self.read_state()["watches"] if w.get("status") == "open"]
        self.assertEqual(len(watches), 1)
        self.assertEqual(watches[0]["kind"], "absorption")
        self.assertEqual(watches[0]["side"], "long")
        self.assertGreater(watches[0]["impulse_high"], watches[0]["impulse_low"])

    def test_15m_early_trigger(self):
        def candles(coin, interval, n_bars):
            if interval == "15m":
                return bars(40, last_pct=6.0, last_v=500.0, step_ms=900_000)
            return bars(40, last_pct=0.4, last_v=10.0)

        self.assertTrue(self.cycle([asset("AAA")], candle_fn=candles))
        item = self.alerts()[0]["alerts"][0]
        self.assertEqual(item["trigger"], "15m_early")
        self.assertGreaterEqual(abs(item["pct_15m"]), 4)
        self.assertGreaterEqual(item["vol_15m_usd"], 25_000)
        self.assertEqual(item["early_floor_pct"], 4.0)
        self.assertEqual(item["floor_pct"], 5.0)

    def test_exhaustion_flag(self):
        def candles(coin, interval, n_bars):
            if interval == "1h":
                return bars(40, last_pct=10.0, last_v=2000.0, high_extra=30.0)
            return quiet_15m()

        self.assertTrue(self.cycle([asset("AAA")], candle_fn=candles))
        item = self.alerts()[0]["alerts"][0]
        self.assertGreater(item["wick_ratio"], 50)
        self.assertTrue(item["exhaustion_flag"])
        self.assertEqual(item["exhaustion_label"], "posible agotamiento")

    def test_quiet_hours_do_not_send_or_count(self):
        self.freeze(3)

        def forbid_send(*_a, **_k):
            raise AssertionError("no se debe llamar al webhook en silencio")

        detector.send = forbid_send
        self.assertTrue(self.cycle([asset("AAA")]))
        st = self.read_state()
        self.assertEqual(st["wakes"], {})
        self.assertEqual(st["wakes_hour"], {})
        self.assertEqual(st["last_fire"], {})
        self.assertEqual(self.alerts()[0]["status"], "quiet_hours_logged")
        self.assertIn("silencio", self.latest())

    def test_quiet_hours_end_at_08(self):
        self.freeze(8)
        self.assertTrue(self.cycle([asset("AAA")]))
        self.assertEqual(self.alerts()[0]["status"], "dry_run")
        self.assertIn("alerta", self.latest())

    def test_daily_and_hourly_caps(self):
        def forbid_send(*_a, **_k):
            raise AssertionError("el tope no debe hacer POST")

        detector.send = forbid_send
        self.assertTrue(self.cycle(
            [asset("AAA")],
            state={"marks": [], "last_fire": {}, "wakes": {"2026-06-15": 10}, "wakes_hour": {}},
        ))
        self.assertEqual(self.alerts()[0]["status"], "daily_cap_logged")
        self.assertIn("tope", self.latest())
        self.assertEqual(self.read_state()["wakes"]["2026-06-15"], 10)

        self.assertTrue(self.cycle(
            [asset("BBB", pct_24h=20)],
            state={"marks": [], "last_fire": {}, "wakes": {}, "wakes_hour": {"2026-06-15T12": 2}},
        ))
        self.assertEqual(self.alerts()[-1]["status"], "hourly_cap_logged")

    def test_cooldown_suppresses_repeat(self):
        self.assertTrue(self.cycle(
            [asset("AAA")],
            state={"marks": [], "last_fire": {"AAA": NOW - 60}, "wakes": {}},
        ))
        self.assertEqual(self.alerts(), [])
        self.assertIn("pausa", self.latest())
        self.assertEqual(self.read_state()["last_fire"]["AAA"], NOW - 60)
        self.assertIn("status=none", detector.LOG.read_text(encoding="utf-8"))

    def test_post_failed_does_not_consume_a_slot(self):
        detector.send = lambda payload, cfg: "post_failed"
        self.assertTrue(self.cycle([asset("AAA")]))
        st = self.read_state()
        self.assertEqual(st["wakes"], {})
        self.assertEqual(st["wakes_hour"], {})
        self.assertEqual(st["last_fire"], {})
        self.assertEqual(self.alerts()[0]["status"], "post_failed")

    def test_one_post_batches_every_hit_as_one_slot(self):
        calls = []

        def record(payload, cfg):
            calls.append(payload)
            return "posted"

        detector.send = record
        self.assertTrue(self.cycle([asset("AAA", pct_24h=30), asset("BBB", pct_24h=12)]))
        self.assertEqual(len(calls), 1)
        self.assertEqual({p["coin"] for p in calls[0]}, {"AAA", "BBB"})
        self.assertEqual(self.read_state()["wakes"]["2026-06-15"], 1)
        self.assertEqual(self.read_state()["wakes_hour"]["2026-06-15T12"], 1)

    def test_prefilter_keeps_the_hottest_and_the_15m_move(self):
        seen = []

        def candles(coin, interval, n_bars):
            seen.append(coin)
            return hit_1h() if interval == "1h" else quiet_15m()

        self.assertTrue(self.cycle(
            [asset("BBB", pct_24h=9), asset("AAA", pct_24h=40), asset("CCC", pct_24h=12)],
            candle_fn=candles,
            cfg=load_cfg(max_candle_calls=1),
        ))
        self.assertEqual(set(seen), {"AAA"})
        with (detector.STATE.parent / "scans" / "2026-06-15.csv").open(encoding="utf-8") as fh:
            results = {row["coin"]: row["result"] for row in csv.DictReader(fh)}
        self.assertEqual(results["AAA"], "alerta")
        self.assertEqual(results["BBB"], "fuera del top")
        self.assertEqual(results["CCC"], "fuera del top")

        seen.clear()

        def quiet(coin, interval, n_bars):
            seen.append(coin)
            if interval == "1h":
                return bars(40, last_pct=0.3, last_v=10.0)
            return quiet_15m()

        state = {"marks": [{"ts": NOW - 900, "m": {"ZZZ": 100.0}, "oi": {}}], "last_fire": {}, "wakes": {}}
        self.assertTrue(self.cycle(
            [asset("ZZZ", mark=104.0, pct_24h=0.5)],
            candle_fn=quiet,
            state=state,
        ))
        self.assertIn("ZZZ", seen)
        day = detector.STATE.parent / "scans" / "2026-06-15.csv"
        with day.open(encoding="utf-8") as fh:
            results = {row["coin"]: row["result"] for row in csv.DictReader(fh)}
        self.assertEqual(results["ZZZ"], "no llega")

        seen.clear()
        state = {"marks": [{"ts": NOW - 120, "m": {"ZZZ": 100.0}}], "last_fire": {}, "wakes": {}}
        self.assertTrue(self.cycle(
            [asset("ZZZ", mark=110.0, pct_24h=0.2)],
            candle_fn=candles,
            state=state,
        ))
        self.assertEqual(seen, [])

    def test_illiquid_and_thin_hip3_are_not_fetched(self):
        seen = []

        def candles(coin, interval, n_bars):
            seen.append(coin)
            return hit_1h() if interval == "1h" else quiet_15m()

        self.assertTrue(self.cycle([
            asset("DUST", pct_24h=40, day=1_000),
            asset("HIP", dex="xyz", pct_24h=40, day=2_000_000, oi=100_000),
            asset("OK", dex="xyz", pct_24h=40, day=2_000_000, oi=600_000),
        ], candle_fn=candles))
        self.assertEqual(set(seen), {"OK"})

    def test_scan_files_and_summary_and_retention(self):
        summary = self.tmp.name + "/summary.md"
        os.environ["GITHUB_STEP_SUMMARY"] = summary
        scans = detector.STATE.parent / "scans"
        scans.mkdir(parents=True)
        (scans / "2026-05-01.csv").write_text("ts_madrid,dex\nold\n", encoding="utf-8")
        (scans / "2026-06-10.csv").write_text("ts_madrid,dex\nkeep\n", encoding="utf-8")
        self.assertTrue(self.cycle([asset("AAA")]))
        text = Path(summary).read_text(encoding="utf-8")
        self.assertIn("Hora (Madrid): 2026-06-15 12:30", text)
        self.assertIn("Perps revisados:", text)
        self.assertIn("| dex | coin | 15m % | 1h % |", text)
        self.assertIn("AAA", text)
        self.assertIn("AAA", self.latest())
        daily = scans / "2026-06-15.csv"
        with daily.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]["coin"], "AAA")
        self.assertEqual(rows[0]["result"], "alerta")
        self.assertFalse((scans / "2026-05-01.csv").exists())
        self.assertTrue((scans / "2026-06-10.csv").exists())

    def test_failed_scan_does_not_touch_state(self):
        detector.STATE.write_text('{"marks":[{"ts":1,"m":{"AAA":1}}],"last_fire":{},"wakes":{}}', encoding="utf-8")
        before = detector.STATE.read_bytes()

        def boom():
            raise RuntimeError("perpDexs: down")

        detector.scan = boom
        self.assertFalse(detector.run_cycle(load_cfg()))
        self.assertEqual(detector.STATE.read_bytes(), before)
        self.assertFalse((detector.STATE.parent / "state_ready").exists())
        self.assertEqual(detector.live(False), 1)
        self.assertEqual(detector.STATE.read_bytes(), before)

    def test_empty_scan_does_not_touch_state(self):
        detector.STATE.write_text('{"marks":[],"last_fire":{"AAA":1},"wakes":{}}', encoding="utf-8")
        before = detector.STATE.read_bytes()
        detector.scan = lambda: snap([], errors=["main: down"])
        self.assertFalse(detector.run_cycle(load_cfg()))
        self.assertEqual(detector.STATE.read_bytes(), before)
        detector.scan = lambda: snap([asset("AAA", mark=None, pct_24h=80)])
        self.assertFalse(detector.run_cycle(load_cfg()))
        self.assertEqual(detector.STATE.read_bytes(), before)

    def test_corrupt_state_is_not_overwritten(self):
        detector.scan = lambda: snap([asset("AAA")])
        detector.candles = route()
        detector.STATE.write_text("{{{", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            detector.run_cycle(load_cfg())
        self.assertEqual(detector.STATE.read_text(encoding="utf-8"), "{{{")

    def test_escalera_sets_its_label_and_can_ignore_quiet_hours(self):
        def candles(coin, interval, n_bars):
            if interval != "1h":
                return quiet_15m()
            cs = bars(24, last_pct=0.0, last_v=10.0)
            price = 100.0
            start = cs[-3]["t"]
            built = cs[:-3]
            for k in range(3):
                o = price
                c = round(price * 1.04, 4)
                built.append({
                    "t": start + k * 3_600_000, "o": o, "h": c + 0.3, "l": o - 0.05, "c": c, "v": 20,
                })
                price = c
            return built

        self.freeze(3)
        self.assertTrue(self.cycle([asset("STEP")], candle_fn=candles))
        self.assertEqual(self.alerts()[0]["status"], "quiet_hours_logged")
        self.assertEqual(self.alerts()[0]["alerts"][0]["label"], "escalera")
        self.assertEqual(self.alerts()[0]["alerts"][0]["trigger"], "3h_escalera")
        self.assertIn("silencio", self.latest())

        self.assertTrue(self.cycle(
            [asset("STEP")],
            candle_fn=candles,
            cfg=load_cfg(escalera_ignore_quiet_hours=True),
            state={"marks": [], "last_fire": {}, "wakes": {}, "wakes_hour": {}},
        ))
        self.assertEqual(self.alerts()[-1]["status"], "dry_run")
        self.assertTrue(self.alerts()[-1]["alerts"][0]["tradable"])

    def test_three_hour_move_enters_the_candle_set(self):
        seen = []

        def candles(coin, interval, n_bars):
            seen.append(coin)
            return hit_1h() if interval == "1h" else quiet_15m()

        # 4h old: inside the 3h window (2h–5h) and outside the 1h window (50min–3h).
        state = {
            "marks": [{"ts": NOW - 4 * 3600, "m": {"SLOW": 100.0, "QUIET": 100.0}, "oi": {}}],
            "last_fire": {}, "wakes": {},
        }
        self.assertTrue(self.cycle([
            asset("SLOW", mark=107.0, pct_24h=1.0),
            asset("QUIET", mark=104.0, pct_24h=1.0),
            asset("INIT", mark=152.0, pct_24h=52.0),
        ], candle_fn=candles, state=state))
        self.assertIn("SLOW", seen)
        self.assertIn("INIT", seen)
        self.assertNotIn("QUIET", seen)

    def test_init_9_nov_replay(self):
        raw = json.loads(Path("tests/fixtures/init_2026-11-09.json").read_text(encoding="utf-8"))
        cfg = load_cfg()
        events = detector.replay_candles(raw["candles_1h"], raw["candles_15m"], cfg, floor=5.0)
        stair = [e for e in events if e["interval"] == "1h" and "3h_escalera" in e["triggers"]]
        nineteen = [e for e in stair if e["madrid"] == "2026-11-09 19:00"]
        self.assertEqual(len(nineteen), 1, stair)
        self.assertAlmostEqual(nineteen[0]["close"], 0.1468, places=4)
        twenty = next(e for e in events if e["interval"] == "1h" and e["madrid"] == "2026-11-09 20:00")
        self.assertIn("1h_legacy", twenty["triggers"])
        self.assertNotIn("1h_atr", twenty["triggers"])
        self.assertGreater(twenty["pct"], 11)
        self.assertGreater(twenty["vol_mult"], 5)
        self.assertAlmostEqual(twenty["close"], 0.164, places=3)
        early = [e for e in events if "15m_early" in e["triggers"] and e["madrid"] >= "2026-11-09 18:00"]
        self.assertTrue(early)
        self.assertEqual(early[0]["madrid"], "2026-11-09 21:00")
        self.assertGreater(early[0]["pct"], 4)
        self.assertGreater(early[0]["vol_mult"], 5)
        self.assertAlmostEqual(early[0]["open"], 0.16385, places=4)

    def test_alert_log_is_capped(self):
        path = detector.ALERTS
        for i in range(5):
            detector.append_jsonl(path, {"i": i}, limit=3)
        lines = [json.loads(ln)["i"] for ln in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lines, [2, 3, 4])

    def test_redact_and_send_do_not_log_secrets(self):
        os.environ["EXPLOSION_WEBHOOK_URL"] = SECRET_URL
        os.environ["EXPLOSION_WEBHOOK_AUTH"] = SECRET_AUTH
        hidden = detector.redact(f"fail url={SECRET_URL} auth=Bearer SUPERSECRETTOKEN")
        self.assertNotIn("super-secret-path", hidden)
        self.assertNotIn("SUPERSECRETTOKEN", hidden)
        self.assertNotIn("127.0.0.1", hidden)

        seen = {}

        def boom(req, timeout=20):
            seen["auth"] = req.get_header("Authorization")
            seen["url"] = req.full_url
            raise RuntimeError(f"fail url={req.full_url} auth={req.get_header('Authorization')}")

        urllib.request.urlopen = boom
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mode = detector.log_webhook_mode()
            status = detector.send([{"test": True}], load_cfg())
        self.assertEqual(mode, "ready")
        self.assertEqual(status, "post_failed")
        self.assertEqual(seen["auth"], "Bearer SUPERSECRETTOKEN")
        self.assertEqual(seen["url"], SECRET_URL)
        leaked = buf.getvalue() + detector.LOG.read_text(encoding="utf-8")
        self.assertNotIn("SUPERSECRETTOKEN", leaked)
        self.assertNotIn("super-secret-path", leaked)
        self.assertNotIn(SECRET_URL, leaked)
        self.assertIn("webhook error:", leaked)

    def test_scan_output_does_not_contain_secrets(self):
        os.environ["EXPLOSION_WEBHOOK_URL"] = SECRET_URL
        os.environ["EXPLOSION_WEBHOOK_AUTH"] = SECRET_AUTH
        urllib.request.urlopen = lambda req, timeout=20: (_ for _ in ()).throw(
            RuntimeError(f"fail url={req.full_url} auth={req.get_header('Authorization')}")
        )
        self.assertTrue(self.cycle([asset("AAA")]))
        blob = self.latest() + detector.STATE.read_text(encoding="utf-8")
        blob += (detector.STATE.parent / "scans" / "2026-06-15.csv").read_text(encoding="utf-8")
        self.assertNotIn("SUPERSECRETTOKEN", blob)
        self.assertNotIn("super-secret-path", blob)
        self.assertNotIn(SECRET_URL, blob)

    def test_send_puts_full_header_line_and_key_fallback(self):
        os.environ["EXPLOSION_WEBHOOK_URL"] = SECRET_URL
        os.environ["EXPLOSION_WEBHOOK_AUTH"] = "Bearer SUPERSECRETTOKEN"
        seen = {}

        def ok(req, timeout=20):
            seen.setdefault("auth", []).append(req.get_header("Authorization"))
            return Resp(b"", status=204)

        urllib.request.urlopen = ok
        self.assertEqual(detector.send([{"a": 1}], load_cfg()), "posted")

        os.environ.pop("EXPLOSION_WEBHOOK_AUTH", None)
        os.environ["EXPLOSION_WEBHOOK_KEY"] = "old-key"
        self.assertEqual(detector.send([{"a": 1}], load_cfg()), "posted")
        self.assertEqual(seen["auth"], ["Bearer SUPERSECRETTOKEN", "Bearer old-key"])
        leaked = detector.LOG.read_text(encoding="utf-8")
        self.assertNotIn("SUPERSECRETTOKEN", leaked)
        self.assertNotIn("old-key", leaked)

    def test_send_dry_run_does_not_call_the_network(self):
        def boom(*_a, **_k):
            raise AssertionError("dry-run no debe abrir una conexión")

        urllib.request.urlopen = boom
        self.assertEqual(detector.send([{"a": 1}], load_cfg()), "dry_run")

    def test_force_test_does_not_spend_a_wake(self):
        detector.scan = lambda: snap([asset("AAA", pct_24h=1.0)])
        calls = []

        def record(payload, cfg):
            calls.append(payload)
            return "posted"

        detector.send = record
        self.assertEqual(detector.live(force_test_post=True), 0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0]["test"])
        self.assertEqual(calls[0][0]["message"], "POST de prueba manual")
        st = self.read_state()
        self.assertEqual(st["wakes"], {})
        self.assertEqual(st.get("wakes_hour") or {}, {})
        self.assertEqual(st["last_fire"], {})
        self.assertEqual(self.alerts()[-1]["status"], "test_posted")
        self.assertIn("force_test status=posted", detector.LOG.read_text(encoding="utf-8"))

    def test_force_test_failure_keeps_saved_state(self):
        detector.scan = lambda: snap([asset("AAA", pct_24h=1.0)])
        detector.send = lambda payload, cfg: "post_failed"
        self.assertEqual(detector.live(force_test_post=True), 1)
        self.assertTrue(detector.STATE.exists())
        self.assertEqual(self.read_state()["wakes"], {})
        self.assertTrue((detector.STATE.parent / "state_ready").exists())

    def test_post_retries_429_and_not_400(self):
        perps_all.post = self._saved["p_post"]
        perps_all._spent = []
        perps_all._blocked_until = 0.0
        perps_all._next_ok = 0.0
        calls = {"n": 0}

        def fake(req, timeout=30):
            calls["n"] += 1
            if calls["n"] == 1:
                raise http_error(429, "1")
            return Resp(json.dumps([{"name": "main"}]).encode(), status=200)

        urllib.request.urlopen = fake
        self.assertEqual(perps_all.post({"type": "perpDexs"}), [{"name": "main"}])
        self.assertEqual(calls["n"], 2)

        calls["n"] = 0

        def bad(req, timeout=30):
            calls["n"] += 1
            raise http_error(400)

        urllib.request.urlopen = bad
        with self.assertRaises(urllib.error.HTTPError):
            perps_all.post({"type": "perpDexs"})
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
