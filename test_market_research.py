#!/usr/bin/env python3
"""Parsing, listing diffs and the research CSV writers. HTTP is a fake post()."""
import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import market_research
import paper
import paper_run
import perps_all

HOUR = "2026-11-16T18:00:00Z"
NOW = datetime(2026, 11, 16, 18, 2, tzinfo=timezone.utc)


def raw_book(bids, asks, when=1_763_318_400_000):
    return {
        "time": when,
        "levels": [
            [{"px": str(px), "sz": str(sz), "n": 1} for px, sz in bids],
            [{"px": str(px), "sz": str(sz), "n": 1} for px, sz in asks],
        ],
    }


def parsed(bids, asks, when=1_763_318_400_000):
    return market_research.parse_book(raw_book(bids, asks, when))


class BookMathTests(unittest.TestCase):
    def test_aggregation_matches_the_live_btc_buckets(self):
        self.assertEqual(market_research.aggregation_step(64785, 5), 1)
        self.assertEqual(market_research.aggregation_step(64785, 4), 10)
        self.assertEqual(market_research.aggregation_step(64785, 3), 100)
        self.assertEqual(market_research.aggregation_step(156.5, 3), 1)
        self.assertAlmostEqual(market_research.aggregation_step(0.0001335, 3), 1e-6)
        self.assertIsNone(market_research.aggregation_step(64785, None))

    def test_sigfigs_cover_each_band_without_a_wider_bucket(self):
        # BTC near 65k: nSigFigs 3 reaches 2% and still resolves 0.5%.
        self.assertEqual(market_research.aggregated_sigfigs(64785), (3,))
        # A price whose 3-sig bucket is wider than 0.5% needs 4 for that band.
        self.assertEqual(market_research.aggregated_sigfigs(156.5), (4, 3))
        self.assertEqual(market_research.aggregated_sigfigs(100), (4, 3))
        # Significand near 9.8: 3-sig levels stop just short of 2%, so 2 is added.
        self.assertEqual(market_research.aggregated_sigfigs(98000), (3, 2))
        self.assertEqual(market_research.aggregated_sigfigs(None), ())

    def test_depth_stops_at_the_band_and_flags_a_capped_book(self):
        levels = [{"px": 100 - i, "sz": 1} for i in range(20)]
        notional, complete = market_research.depth_side(levels, 100, 0.05, "bid")
        self.assertTrue(complete)
        # 100 down to the 95 edge. The next level, 94, sits outside the band.
        self.assertAlmostEqual(notional, sum(px for px in range(95, 101)))
        capped, done = market_research.depth_side(levels, 100, 0.5, "bid")
        self.assertFalse(done)
        self.assertAlmostEqual(capped, sum(lvl["px"] for lvl in levels))
        empty, empty_done = market_research.depth_side([], 100, 0.01, "ask")
        self.assertEqual((empty, empty_done), (0.0, True))
        self.assertIsNone(market_research.imbalance(0, 0))
        self.assertAlmostEqual(market_research.imbalance(3, 1), 0.5)

    def test_spread_comes_from_the_tight_book_and_depth_from_the_band(self):
        # Twenty raw levels still inside 0.5%: the feed is capped, so the
        # band has to come from the aggregated book. The inside quote is unchanged.
        tight = parsed(
            [(99.99 - 0.0001 * i, 1) for i in range(20)],
            [(100.01 + 0.0001 * i, 1) for i in range(20)],
        )
        bids4 = [(100 - 0.05 * i, 1) for i in range(1, 21)]
        asks4 = [(100 + 0.05 * i, 1) for i in range(1, 21)]
        wide4 = parsed(bids4, asks4)
        wide3 = parsed([(99, 2), (98, 2), (97, 5)], [(101, 2), (102, 2), (103, 5)])
        row = market_research.orderbook_row(
            {"dex": "main", "coin": "BTC", "dayNtlVlm": 5_000_000_000, "mark": 100},
            tight, {4: wide4, 3: wide3}, HOUR,
        )
        self.assertAlmostEqual(row["mid"], 100)
        self.assertAlmostEqual(row["spread_bps"], 2.0)
        self.assertEqual(row["spread_aggregated"], 0)
        self.assertEqual(row["sig_0_5"], "4")
        self.assertEqual(row["complete_0_5"], 1)
        self.assertAlmostEqual(row["bid_ntl_0_5"], 997.25)
        self.assertAlmostEqual(row["ask_ntl_0_5"], 1002.75)
        self.assertAlmostEqual(row["imb_0_5"], (997.25 - 1002.75) / (997.25 + 1002.75))
        self.assertEqual(row["sig_2"], "3")
        self.assertEqual(row["complete_2"], 1)
        self.assertAlmostEqual(row["bid_ntl_2"], 99 * 2 + 98 * 2)
        self.assertAlmostEqual(row["ask_ntl_2"], 101 * 2 + 102 * 2)
        # Twenty raw levels still inside 2% are a lower bound, not a full band.
        alone = market_research.choose_depth(100, 0.02, None, {4: wide4})
        self.assertEqual(alone[2], False)
        self.assertEqual(alone[3], 4)

    def test_missing_quote_does_not_invent_a_row(self):
        self.assertIsNone(market_research.parse_book({"levels": [[], []]}))
        self.assertIsNone(market_research.orderbook_row(
            {"coin": "BTC"}, None, {}, HOUR,
        ))

    def test_l2book_weight_is_two_and_candles_stay_heavier(self):
        self.assertEqual(perps_all._body_weight({"type": "l2Book", "coin": "BTC"}), 2)
        candle = perps_all._body_weight({"type": "candleSnapshot", "req": {
            "coin": "BTC", "interval": "1h",
            "startTime": 0, "endTime": 40 * paper.HOUR_MS,
        }})
        self.assertGreaterEqual(candle, 20)
        self.assertEqual(perps_all._body_weight({"type": "metaAndAssetCtxs"}), 20)


class UniverseTests(unittest.TestCase):
    def test_selects_the_top_slice_plus_anything_over_a_million(self):
        assets = [
            {"dex": "main", "coin": f"T{i:03d}", "dayNtlVlm": 1_000_000 + (120 - i)}
            for i in range(120)
        ]
        assets.extend([
            {"dex": "xyz", "coin": "xyz:GOLD", "dayNtlVlm": 2_000_000},
            {"dex": "main", "coin": "JUST", "dayNtlVlm": 1_000_000},
            {"dex": "main", "coin": "TINY", "dayNtlVlm": 10},
            {"dex": "main", "coin": "GONE", "dayNtlVlm": 9_000_000, "isDelisted": True},
            {"dex": "xyz", "coin": "xyz:THIN", "dayNtlVlm": 50},
        ])
        chosen = market_research.select_coins(assets)
        names = [asset["coin"] for asset in chosen]
        self.assertEqual(names[0], "xyz:GOLD")
        self.assertIn("T119", names)
        self.assertNotIn("JUST", names)
        self.assertNotIn("TINY", names)
        self.assertNotIn("GONE", names)
        self.assertNotIn("xyz:THIN", names)
        # GOLD plus T000..T119 are all strictly above $1M. T119 is past the
        # top 120 and is kept for that reason; JUST sits on the $1M line.
        self.assertEqual(len(names), 121)
        short = market_research.select_coins([
            {"dex": "main", "coin": "TINY", "dayNtlVlm": 10},
            {"dex": "xyz", "coin": "xyz:ALSO", "dayNtlVlm": 20},
        ])
        self.assertEqual([asset["coin"] for asset in short], ["xyz:ALSO", "TINY"])

    def test_market_log_columns_stay_what_trapped_shorts_reads(self):
        self.assertEqual(paper.MARKET_FIELDS, (
            "ts", "dex", "coin", "mark", "oi", "oi_usd", "funding", "premium", "dayNtlVlm",
        ))


class ListingTests(unittest.TestCase):
    def setUp(self):
        self.prev = [
            {"dex": "main", "coin": "BTC", "max_leverage": 40},
            {"dex": "xyz", "coin": "xyz:GOLD", "max_leverage": 10},
        ]

    def test_first_snapshot_emits_nothing(self):
        snap = {
            "dexs": ["main", "xyz"],
            "errors": [],
            "assets": [
                {"dex": "main", "coin": "BTC", "maxLeverage": 40},
                {"dex": "xyz", "coin": "xyz:GOLD", "maxLeverage": 10},
            ],
        }
        rows, events = market_research.plan_listing_update(None, snap)
        self.assertEqual(events, [])
        self.assertEqual([row["coin"] for row in rows], ["BTC", "xyz:GOLD"])

    def test_listing_delisting_and_leverage(self):
        snap = {
            "dexs": ["main", "xyz"],
            "errors": [],
            "assets": [
                {"dex": "main", "coin": "BTC", "maxLeverage": 50},
                {"dex": "main", "coin": "ETH", "maxLeverage": 25},
                {"dex": "xyz", "coin": "xyz:GOLD", "maxLeverage": 10, "isDelisted": True},
            ],
        }
        _rows, events = market_research.plan_listing_update(self.prev, snap)
        kinds = {(event["coin"], event["event"], event["max_leverage"], event["prev_max_leverage"]) for event in events}
        self.assertEqual(kinds, {
            ("ETH", "listing", 25, None),
            ("BTC", "max_leverage", 50, 40),
            ("xyz:GOLD", "delisting", None, 10),
        })

    def test_a_failed_dex_is_carried_forward(self):
        snap = {
            "dexs": ["main", "xyz"],
            "errors": ["xyz: timeout leyendo meta"],
            "assets": [{"dex": "main", "coin": "BTC", "maxLeverage": 40}],
        }
        rows, events = market_research.plan_listing_update(self.prev, snap)
        self.assertEqual(events, [])
        self.assertEqual(
            {(row["dex"], row["coin"], row["max_leverage"]) for row in rows},
            {("main", "BTC", 40), ("xyz", "xyz:GOLD", 10)},
        )

    def test_perp_dexs_failure_does_not_wipe_hip3(self):
        snap = {
            "dexs": ["main"],
            "errors": ["perpDexs: timeout"],
            "assets": [{"dex": "main", "coin": "BTC", "maxLeverage": 40}],
        }
        rows, events = market_research.plan_listing_update(self.prev, snap)
        self.assertEqual(events, [])
        self.assertIn(("xyz", "xyz:GOLD"), {(row["dex"], row["coin"]) for row in rows})

    def test_a_dex_dropped_by_perp_dexs_is_a_delisting(self):
        snap = {
            "dexs": ["main"],
            "errors": [],
            "assets": [{"dex": "main", "coin": "BTC", "maxLeverage": 40}],
        }
        rows, events = market_research.plan_listing_update(self.prev, snap)
        self.assertEqual([event["event"] for event in events], ["delisting"])
        self.assertEqual(events[0]["coin"], "xyz:GOLD")
        self.assertEqual([row["coin"] for row in rows], ["BTC"])


class ProxyAndTapeTests(unittest.TestCase):
    def test_proxy_needs_an_hourly_oi_drop_plus_range_and_volume(self):
        self.assertTrue(market_research.is_proxy_hit(0.02, 1, 0.01, 2.0))
        self.assertFalse(market_research.is_proxy_hit(0.02, 2, 0.01, 2.0))
        self.assertFalse(market_research.is_proxy_hit(0.019, 1, 0.01, 2.0))
        self.assertFalse(market_research.is_proxy_hit(-0.05, 1, 0.05, 5))
        self.assertFalse(market_research.is_proxy_hit(0.05, 1, 0.009, 5))
        candle = {"t": 0, "o": 10.0, "h": 10.2, "l": 9.9, "c": 10.0, "v": 30.0}
        prior = [{"t": -i, "o": 10, "h": 10, "l": 10, "c": 10, "v": 10} for i in range(1, 25)]
        asset = {"dex": "main", "coin": "BTC", "oi_coins": 90, "oi_usd": 9000, "dayNtlVlm": 5}
        prev = {"oi": 100.0, "oi_usd": 10000.0, "gap_h": 1}
        row = market_research.proxy_row(asset, candle, prior, prev, HOUR)
        self.assertEqual(row["proxy"], 1)
        self.assertAlmostEqual(row["oi_drop_pct"], 0.1)
        self.assertAlmostEqual(row["range_pct"], 0.03)
        self.assertAlmostEqual(row["vol_mult"], 3.0)
        self.assertEqual(row["vol_samples"], 24)
        thin = market_research.proxy_row(asset, candle, prior[:5], prev, HOUR)
        self.assertIsNone(thin["vol_mult"])
        self.assertEqual(thin["proxy"], 0)

    def test_public_trade_without_a_liquidation_field_is_not_one(self):
        public = {
            "coin": "BTC", "side": "B", "px": "100", "sz": "1",
            "hash": "0xabc", "time": 1, "tid": 9, "users": ["0xa", "0xb"],
        }
        self.assertIsNone(market_research.classify_trade(public))
        self.assertIsNone(market_research.classify_trade(dict(public, liquidation=None)))
        self.assertIsNone(market_research.classify_trade(dict(public, liquidation={})))
        marked = dict(public, liquidation={"liquidatedUser": "0x1", "markPx": "100", "method": "market"})
        self.assertEqual(market_research.classify_trade(marked), "liquidation")
        state = {"trades": 0, "flagged": [], "capped": False}
        market_research.absorb_message({"channel": "trades", "data": [public, marked]}, state)
        market_research.absorb_message({"channel": "subscriptionResponse", "data": {}}, state)
        self.assertEqual(state["trades"], 2)
        self.assertEqual(len(state["flagged"]), 1)
        rows = market_research.trade_rows(state["flagged"], {"BTC": "main"}, HOUR)
        self.assertEqual(rows[0]["marker"], "liquidation")
        self.assertEqual(rows[0]["dex"], "main")
        self.assertAlmostEqual(rows[0]["notional"], 100)
        self.assertNotIn("users", rows[0])
        self.assertNotIn("0x1", json.dumps(rows[0]))

    def test_lookup_reads_contract_oi_from_the_previous_hour(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            old = NOW - timedelta(hours=1)
            asset = {
                "dex": "main", "coin": "BTC", "mark": 100, "oi_coins": 100,
                "oi_usd": 10000, "funding": 0.0001, "premium": -0.001, "dayNtlVlm": 8_000_000,
            }
            paper.append_market_log([asset], old, directory)
            paper.append_market_log([dict(asset, oi_coins=90)], NOW, directory)
            hour_ms = int(datetime(2026, 11, 16, 18, tzinfo=timezone.utc).timestamp() * 1000)
            found = market_research.lookup_oi(directory, "main", "BTC", hour_ms)
            self.assertEqual(found["oi"], 100.0)
            self.assertEqual(found["gap_h"], 1)
            self.assertIsNone(market_research.lookup_oi(directory, "xyz", "BTC", hour_ms))


class CollectTests(unittest.TestCase):
    def _candles(self):
        end = datetime(2026, 11, 16, 17, tzinfo=timezone.utc)
        bars = []
        for i in range(25, -1, -1):
            start = end - timedelta(hours=i)
            bars.append({
                "t": int(start.timestamp() * 1000),
                "o": "10", "h": "10.2", "l": "9.9", "c": "10",
                "v": "30" if i == 0 else "10",
            })
        forming = end + timedelta(hours=1)
        bars.append({
            "t": int(forming.timestamp() * 1000),
            "o": "10", "h": "10.1", "l": "9.9", "c": "10", "v": "1",
        })
        return bars

    def _books(self):
        tight = raw_book(
            [(99.99 - 0.0001 * i, 1) for i in range(20)],
            [(100.01 + 0.0001 * i, 1) for i in range(20)],
        )
        bids4 = [(100 - 0.05 * i, 1) for i in range(1, 21)]
        asks4 = [(100 + 0.05 * i, 1) for i in range(1, 21)]
        wide3 = raw_book([(99, 2), (98, 2), (97, 5)], [(101, 2), (102, 2), (103, 5)])
        return {None: tight, 4: raw_book(bids4, asks4), 3: wide3}

    def test_collect_writes_logs_and_a_second_pass_does_not_refetch(self):
        books = self._books()
        calls = []
        listens = []

        def post(body):
            calls.append(body)
            kind = body.get("type")
            if kind == "l2Book":
                self.assertEqual(set(body), {"type", "coin"} | ({"nSigFigs"} if "nSigFigs" in body else set()))
                self.assertNotIn("user", body)
                return books[body.get("nSigFigs")]
            if kind == "candleSnapshot":
                self.assertEqual(body["req"]["coin"], "BTC")
                self.assertEqual(body["req"]["interval"], "1h")
                return self._candles()
            raise AssertionError(body)

        def listen(coins, seconds):
            listens.append((list(coins), seconds))
            return {
                "seconds": seconds,
                "coins": len(coins),
                "trades": 4,
                "flagged": [{
                    "coin": "BTC", "side": "A", "px": "100", "sz": "0.5",
                    "time": 1_763_318_400_000, "tid": 7, "hash": "0xabc",
                    "users": ["0xaaa", "0xbbb"],
                    "liquidation": {"liquidatedUser": "0xaaa", "markPx": "100", "method": "market"},
                }],
            }

        asset = {
            "dex": "main", "coin": "BTC", "mark": 100, "maxLeverage": 40,
            "oi_coins": 90, "oi_usd": 9000, "dayNtlVlm": 5_000_000_000,
        }
        snap = {"dexs": ["main"], "errors": [], "assets": [asset]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = {
                "dex": "main", "coin": "BTC", "mark": 100, "oi_coins": 100,
                "oi_usd": 10000, "funding": 0, "premium": 0, "dayNtlVlm": 5_000_000_000,
            }
            paper.append_market_log([old], NOW - timedelta(hours=1), root / "market_log")
            wrote, notes = market_research.collect(
                [asset], snap, NOW, post=post, listen=listen, root=root, workers=1,
            )
            self.assertTrue(wrote)
            self.assertTrue(any("Libro" in note for note in notes))
            book_path = root / "orderbook_log" / "2026-11-16.csv"
            with book_path.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(list(rows[0]), list(market_research.ORDERBOOK_FIELDS))
            self.assertEqual(rows[0]["spread_bps"], "2")
            self.assertEqual(rows[0]["spread_aggregated"], "0")
            self.assertEqual(rows[0]["sig_2"], "3")
            self.assertEqual(rows[0]["complete_2"], "1")
            with (root / "liq_proxy_log" / "2026-11-16.csv").open(newline="", encoding="utf-8") as fh:
                proxy_rows = list(csv.DictReader(fh))
            self.assertEqual(proxy_rows[0]["proxy"], "1")
            self.assertEqual(proxy_rows[0]["oi"], "90")
            self.assertEqual(proxy_rows[0]["oi_prev"], "100")
            tape = (root / "liq_tape_log" / "2026-11-16.csv").read_text(encoding="utf-8")
            self.assertIn("marcados", "".join(notes))
            self.assertTrue(tape.strip().endswith("180,1,4,1"))
            trade_text = (root / "liq_trades_log" / "2026-11-16.csv").read_text(encoding="utf-8")
            self.assertIn("liquidation", trade_text)
            self.assertNotIn("0xaaa", trade_text)
            self.assertNotIn("users", trade_text)
            snap_path = root / "listings_snapshot.json"
            stored = json.loads(snap_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["coins"][0]["coin"], "BTC")
            self.assertFalse((root / "listings_log.csv").exists())
            n_calls = len(calls)
            self.assertGreaterEqual(n_calls, 3)
            self.assertTrue(all(call["type"] in {"l2Book", "candleSnapshot"} for call in calls))
            self.assertEqual(listens, [(["BTC"], market_research.TAPE_SECONDS)])
            market_header = (root / "market_log" / "2026-11-16.csv").read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(market_header, ",".join(paper.MARKET_FIELDS))

            wrote_again, _notes = market_research.collect(
                [asset], snap, NOW, post=post, listen=listen, root=root, workers=1,
            )
            self.assertEqual(len(calls), n_calls)
            self.assertEqual(len(listens), 1)
            self.assertTrue(wrote_again)
            with book_path.open(newline="", encoding="utf-8") as fh:
                self.assertEqual(len(list(csv.DictReader(fh))), 1)

    def test_a_broken_book_request_still_records_listings(self):
        def post(_body):
            raise RuntimeError("l2Book caido")

        asset = {"dex": "main", "coin": "BTC", "mark": 100, "maxLeverage": 40, "dayNtlVlm": 5_000_000_000, "oi_coins": 1}
        snap = {"dexs": ["main"], "errors": [], "assets": [asset]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrote, notes = market_research.collect(
                [asset], snap, NOW, post=post, listen=lambda coins, seconds: {
                    "seconds": seconds, "coins": len(coins), "trades": 0, "flagged": [],
                }, root=root, workers=1,
            )
            self.assertTrue(wrote)
            stored = json.loads((root / "listings_snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["coins"][0]["max_leverage"], 40)
            self.assertFalse((root / "orderbook_log").exists())
            self.assertTrue((root / "liq_proxy_log" / "2026-11-16.csv").exists())
            self.assertTrue(any(note.startswith("- Listados:") for note in notes))
            self.assertTrue(any("fallo" in note for note in notes) or any("0 filas" in note for note in notes))

    def test_a_corrupt_snapshot_is_left_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "listings_snapshot.json"
            path.write_text("{", encoding="utf-8")
            paths = market_research.data_paths(root)
            with self.assertRaises(json.JSONDecodeError):
                market_research.update_listings(
                    {"dexs": ["main"], "errors": [], "assets": [
                        {"dex": "main", "coin": "BTC", "maxLeverage": 40},
                    ]},
                    NOW, paths,
                )
            self.assertEqual(path.read_text(encoding="utf-8"), "{")

    def test_research_failure_does_not_raise_into_the_books(self):
        original = market_research.collect

        def boom(*_args, **_kwargs):
            raise RuntimeError("libro roto")

        market_research.collect = boom
        try:
            wrote, notes = paper_run._collect_research([], {}, NOW)
        finally:
            market_research.collect = original
        self.assertFalse(wrote)
        self.assertIn("libros", notes[0])


class ReaderTests(unittest.TestCase):
    def test_loader_round_trip(self):
        try:
            import pandas  # noqa: F401
        except ImportError:
            self.skipTest("pandas no está instalado")
        import research_logs

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = root / "orderbook_log"
            day.mkdir(parents=True)
            header = ",".join(market_research.ORDERBOOK_FIELDS)
            (day / "2026-11-16.csv").write_text(
                header + "\n" + ",".join(
                    ["2026-11-16T18:00:00Z", "main", "BTC", "2026-11-16T18:02:00Z", "100", "2", "0"]
                    + ["1", "1", "0", "1", "4"] * 3
                    + ["5"]
                ) + "\n",
                encoding="utf-8",
            )
            (root / "listings_log.csv").write_text(
                "ts,dex,coin,event,max_leverage,prev_max_leverage\n"
                "2026-11-16T18:02:03Z,main,ETH,listing,25,\n",
                encoding="utf-8",
            )
            frame = research_logs.load_orderbook_log(root)
            self.assertEqual(len(frame), 1)
            self.assertEqual(frame.loc[0, "coin"], "BTC")
            self.assertAlmostEqual(frame.loc[0, "spread_bps"], 2.0)
            self.assertIsNotNone(frame.loc[0, "ts"].tzinfo)
            listings = research_logs.load_listings_log(root)
            self.assertEqual(listings.loc[0, "event"], "listing")
            self.assertEqual(listings.loc[0, "max_leverage"], 25)
            self.assertTrue(research_logs.load_liq_trades_log(root).empty)
            loaded = research_logs.load_all(root)
            self.assertIn("liq_proxy", loaded)


if __name__ == "__main__":
    unittest.main()
