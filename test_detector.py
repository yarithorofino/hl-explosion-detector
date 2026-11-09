#!/usr/bin/env python3
"""Unit tests. They must not call Hyperliquid or any webhook."""
import contextlib
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


def asset(coin, mark=100.0, pct_24h=1.0, day=1_000_000.0, dex="main"):
    return {
        "dex": dex,
        "coin": coin,
        "mark": mark,
        "prevDayPx": mark,
        "pct_24h": pct_24h,
        "dayNtlVlm": day,
        "oi_coins": 1.0,
        "oi_usd": None if mark is None else mark,
        "funding": 0.0,
        "maxLeverage": 10,
    }


def snap(assets, errors=None):
    return {
        "assets": assets,
        "errors": errors or [],
        "count": len(assets),
        "dexs": ["main"],
        "ts": NOW,
    }


def make_candles(n=30, last_pct=10.0, last_v=800.0, base_v=10.0, price=100.0):
    cs = []
    t0 = NOW * 1000 - (n - 1) * 3_600_000
    for i in range(n):
        o = price
        c = price
        v = base_v
        if i == n - 1:
            c = price * (1 + last_pct / 100.0)
            v = last_v
        cs.append({
            "t": t0 + i * 3_600_000,
            "o": o,
            "h": max(o, c),
            "l": min(o, c),
            "c": c,
            "v": v,
        })
    return cs


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
        detector.candles = candle_fn or (lambda coin, hours: make_candles())
        if state is not None:
            detector.STATE.write_text(json.dumps(state), encoding="utf-8")
        return detector.run_cycle(cfg or load_cfg())

    def read_state(self):
        return json.loads(detector.STATE.read_text(encoding="utf-8"))

    def alerts(self):
        if not detector.ALERTS.exists():
            return []
        return [json.loads(ln) for ln in detector.ALERTS.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def test_config_thresholds(self):
        cfg = load_cfg()
        self.assertEqual(cfg["pct_1h_min"], 8.0)
        self.assertEqual(cfg["vol_mult_min"], 5.0)
        self.assertEqual(cfg["vol_1h_min_usd"], 50000)
        self.assertEqual(cfg["avg_window_hours"], 24)
        self.assertEqual(cfg["cooldown_hours"], 4)
        self.assertEqual(cfg["max_wakes_per_day"], 8)
        self.assertEqual(cfg["quiet_start_hour"], 0)
        self.assertEqual(cfg["quiet_end_hour"], 8)
        self.assertEqual(cfg["max_candle_calls"], 25)
        self.assertEqual(cfg["prefilter_pct_1h"], 4.0)
        self.assertEqual(cfg["prefilter_pct_24h"], 8.0)

    def test_eval_bar_and_logic(self):
        cfg = load_cfg()
        hit = make_candles()
        m = detector.eval_bar(hit, len(hit) - 1, cfg)
        self.assertIsNotNone(m)
        self.assertTrue(m["hit"])
        self.assertGreaterEqual(abs(m["pct_1h"]), 8)
        self.assertGreaterEqual(m["vol_1h_usd"], 50000)
        self.assertGreaterEqual(m["vol_mult"], 5)
        self.assertIsNone(detector.eval_bar(hit, 5, cfg))

        small_move = make_candles(last_pct=7.0, last_v=800.0)
        self.assertFalse(detector.eval_bar(small_move, len(small_move) - 1, cfg)["hit"])

        thin = make_candles(last_pct=10.0, last_v=10.0)
        self.assertFalse(detector.eval_bar(thin, len(thin) - 1, cfg)["hit"])

        # Volume is large enough in dollars but only ~2.5x the prior hour.
        loud_prior = make_candles(base_v=200.0, last_v=500.0, last_pct=10.0)
        missed = detector.eval_bar(loud_prior, len(loud_prior) - 1, cfg)
        self.assertGreaterEqual(missed["vol_1h_usd"], 50000)
        self.assertLess(missed["vol_mult"], 5)
        self.assertFalse(missed["hit"])

    def test_dry_run_counts_wake_and_cooldown(self):
        self.assertTrue(self.cycle([asset("AAA", pct_24h=20)]))
        st = self.read_state()
        self.assertEqual(st["wakes"]["2026-06-15"], 1)
        self.assertEqual(st["last_fire"]["AAA"], NOW)
        self.assertEqual(self.alerts()[0]["status"], "dry_run")
        self.assertEqual(self.alerts()[0]["alerts"][0]["coin"], "AAA")
        text = detector.LOG.read_text(encoding="utf-8")
        self.assertIn("status=dry_run", text)
        self.assertIn("dry-run:", text)

    def test_quiet_hours_do_not_send_or_count(self):
        self.freeze(3)

        def forbid_send(*_a, **_k):
            raise AssertionError("no se debe llamar al webhook en silencio")

        detector.send = forbid_send
        self.assertTrue(self.cycle([asset("AAA", pct_24h=20)]))
        st = self.read_state()
        self.assertEqual(st["wakes"], {})
        self.assertEqual(st["last_fire"], {})
        self.assertEqual(self.alerts()[0]["status"], "quiet_hours_logged")
        self.assertGreaterEqual(len(st["marks"]), 1)

    def test_daily_cap(self):
        def forbid_send(*_a, **_k):
            raise AssertionError("el tope diario no debe hacer POST")

        detector.send = forbid_send
        self.assertTrue(self.cycle(
            [asset("AAA", pct_24h=20)],
            state={"marks": [], "last_fire": {}, "wakes": {"2026-06-15": 8}},
        ))
        st = self.read_state()
        self.assertEqual(st["wakes"]["2026-06-15"], 8)
        self.assertEqual(st["last_fire"], {})
        self.assertEqual(self.alerts()[0]["status"], "daily_cap_logged")

    def test_cooldown_suppresses_repeat(self):
        self.assertTrue(self.cycle(
            [asset("AAA", pct_24h=20)],
            state={"marks": [], "last_fire": {"AAA": NOW - 60}, "wakes": {}},
        ))
        self.assertEqual(self.alerts(), [])
        st = self.read_state()
        self.assertEqual(st["last_fire"]["AAA"], NOW - 60)
        self.assertEqual(st["wakes"], {})
        self.assertIn("status=none", detector.LOG.read_text(encoding="utf-8"))

    def test_post_failed_does_not_consume_wake(self):
        detector.send = lambda payload, cfg: "post_failed"
        self.assertTrue(self.cycle([asset("AAA", pct_24h=20)]))
        st = self.read_state()
        self.assertEqual(st["wakes"], {})
        self.assertEqual(st["last_fire"], {})
        self.assertEqual(self.alerts()[0]["status"], "post_failed")
        self.assertTrue((detector.STATE.parent / "state_ready").exists())

    def test_one_post_batches_every_hit(self):
        calls = []

        def record(payload, cfg):
            calls.append(payload)
            return "posted"

        detector.send = record
        self.assertTrue(self.cycle([
            asset("AAA", pct_24h=30),
            asset("BBB", pct_24h=12),
        ]))
        self.assertEqual(len(calls), 1)
        self.assertEqual({p["coin"] for p in calls[0]}, {"AAA", "BBB"})
        self.assertEqual(self.read_state()["wakes"]["2026-06-15"], 1)

    def test_max_candle_calls_keeps_the_hottest(self):
        seen = []

        def candles(coin, hours):
            seen.append(coin)
            return make_candles()

        self.assertTrue(self.cycle(
            [asset("BBB", pct_24h=12), asset("AAA", pct_24h=40)],
            candle_fn=candles,
            cfg=load_cfg(max_candle_calls=1),
        ))
        self.assertEqual(seen, ["AAA"])

    def test_prefilter_uses_mark_from_about_an_hour_ago(self):
        seen = []

        def candles(coin, hours):
            seen.append((coin, hours))
            return make_candles()

        state = {"marks": [{"ts": NOW - 3600, "m": {"AAA": 100.0}}], "last_fire": {}, "wakes": {}}
        self.assertTrue(self.cycle(
            [asset("AAA", mark=110.0, pct_24h=1.0)],
            candle_fn=candles,
            state=state,
        ))
        self.assertEqual([c for c, _ in seen], ["AAA"])
        self.assertEqual(seen[0][1], load_cfg()["avg_window_hours"] + 3)
        self.assertEqual(len(self.read_state()["marks"]), 2)

    def test_recent_mark_does_not_count_as_one_hour(self):
        seen = []
        detector.candles = lambda coin, hours: seen.append(coin)
        state = {"marks": [{"ts": NOW - 600, "m": {"AAA": 100.0}}], "last_fire": {}, "wakes": {}}
        self.assertTrue(self.cycle(
            [asset("AAA", mark=110.0, pct_24h=1.0)],
            candle_fn=lambda coin, hours: seen.append(coin) or make_candles(),
            state=state,
        ))
        self.assertEqual(seen, [])

    def test_illiquid_names_are_not_fetched(self):
        seen = []
        self.assertTrue(self.cycle(
            [asset("AAA", pct_24h=40, day=1000)],
            candle_fn=lambda coin, hours: seen.append(coin) or make_candles(),
        ))
        self.assertEqual(seen, [])
        self.assertIn("status=none", detector.LOG.read_text(encoding="utf-8"))

    def test_one_bad_candle_does_not_drop_the_other_or_the_state(self):
        def candles(coin, hours):
            if coin == "BAD":
                raise RuntimeError("429 Too Many Requests")
            return make_candles()

        self.assertTrue(self.cycle([
            asset("BAD", pct_24h=30),
            asset("GOOD", pct_24h=12),
        ], candle_fn=candles))
        self.assertEqual(self.alerts()[0]["alerts"][0]["coin"], "GOOD")
        self.assertIn("GOOD", self.read_state()["marks"][-1]["m"])

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
        detector.scan = lambda: snap([asset("AAA", pct_24h=20)])
        detector.candles = lambda coin, hours: make_candles()
        detector.STATE.write_text("{{{", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            detector.run_cycle(load_cfg())
        self.assertEqual(detector.STATE.read_text(encoding="utf-8"), "{{{")

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
