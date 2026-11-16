#!/usr/bin/env python3
"""Hourly research logs beside the paper books. Public info API only.

Nothing here places an order or reads an exchange key. A failure is logged
and swallowed by the caller so trend4h, squeeze4h and trapped_shorts1h keep
their books. market_log columns are not touched; trapped_shorts1h still reads
open interest from that file.

Three records:

- data/orderbook_log/YYYY-MM-DD.csv
  l2Book depth. The raw book is at most 20 levels and, on a liquid name, does
  not reach 0.5% from the mid. Spread comes from the full-precision book.
  Depth at 0.5%, 1% and 2% comes from the finest aggregated book whose
  buckets still resolve that band and whose levels cover it.
- data/listings_log.csv and data/listings_snapshot.json
  Listings, delistings and max-leverage changes against the previous snapshot.
  The first hour only stores the snapshot.
- Liquidations. The public trades payload (websocket and recentTrades) is
  coin, side, px, sz, hash, time, tid, users. There is no liquidation flag.
  userFills carries one, but only for a single address, which an hourly job
  with no wallet list cannot use. data/liq_trades_log/ stores a trade only
  when the payload includes a liquidation field. data/liq_tape_log/ records
  that the 3-minute sample ran. data/liq_proxy_log/ is not a liquidation: it
  is an hourly OI-drop plus range plus volume flag.
"""
import csv
import io
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paper
import perps_all

BOOK_TOP_N = 120
BOOK_MIN_DAY_NTL = 1_000_000.0
BOOK_LEVELS = 20
WORKERS = 4
TAPE_TOP_N = 40
TAPE_SECONDS = 180
LIQ_FLAG_CAP = 2000
KEEP_DAYS = 120
CANDLE_HOURS = 40
OI_DROP_MIN = 0.02
RANGE_MIN = 0.01
VOL_MULT_MIN = 2.0
VOL_LOOKBACK = 24
MIN_VOL_SAMPLE = 12
OI_MAX_GAP_H = 6
LISTING_DEDUP_S = 3 * 3600
BANDS = (("0_5", 0.005), ("1", 0.01), ("2", 0.02))

ORDERBOOK_FIELDS = (
    "ts", "dex", "coin", "book_time", "mid", "spread_bps", "spread_aggregated",
    "bid_ntl_0_5", "ask_ntl_0_5", "imb_0_5", "complete_0_5", "sig_0_5",
    "bid_ntl_1", "ask_ntl_1", "imb_1", "complete_1", "sig_1",
    "bid_ntl_2", "ask_ntl_2", "imb_2", "complete_2", "sig_2",
    "dayNtlVlm",
)
ORDERBOOK_TEXT = {
    "ts", "dex", "coin", "book_time", "sig_0_5", "sig_1", "sig_2",
}
ORDERBOOK_NUMERIC = (
    "mid", "spread_bps", "spread_aggregated",
    "bid_ntl_0_5", "ask_ntl_0_5", "imb_0_5", "complete_0_5",
    "bid_ntl_1", "ask_ntl_1", "imb_1", "complete_1",
    "bid_ntl_2", "ask_ntl_2", "imb_2", "complete_2",
    "dayNtlVlm",
)
LISTING_FIELDS = (
    "ts", "dex", "coin", "event", "max_leverage", "prev_max_leverage",
)
LISTING_TEXT = {"ts", "dex", "coin", "event"}
LISTING_NUMERIC = ("max_leverage", "prev_max_leverage")
PROXY_FIELDS = (
    "ts", "dex", "coin", "oi", "oi_prev", "oi_usd", "oi_usd_prev", "oi_gap_h",
    "oi_drop_pct", "candle_t", "range_pct", "vol_usd", "vol_mult", "vol_samples",
    "proxy", "dayNtlVlm",
)
PROXY_TEXT = {"ts", "dex", "coin", "candle_t"}
PROXY_NUMERIC = (
    "oi", "oi_prev", "oi_usd", "oi_usd_prev", "oi_gap_h", "oi_drop_pct",
    "range_pct", "vol_usd", "vol_mult", "vol_samples", "proxy", "dayNtlVlm",
)
TAPE_FIELDS = ("ts", "seconds", "coins", "trades", "liq_flags")
TAPE_TEXT = {"ts"}
TAPE_NUMERIC = ("seconds", "coins", "trades", "liq_flags")
TRADE_FIELDS = (
    "ts", "dex", "coin", "side", "px", "sz", "notional", "trade_time",
    "tid", "hash", "marker",
)
TRADE_TEXT = {"ts", "dex", "coin", "side", "trade_time", "tid", "hash", "marker"}
TRADE_NUMERIC = ("px", "sz", "notional")


def _cuenta(n, uno, varios):
    n = int(n)
    word = uno if n == 1 else varios
    return f"{n} {word}"


def data_paths(root=None):
    root = Path(root) if root is not None else paper.ROOT / "data"
    return {
        "root": root,
        "orderbook": root / "orderbook_log",
        "proxy": root / "liq_proxy_log",
        "trades": root / "liq_trades_log",
        "tape": root / "liq_tape_log",
        "listings": root / "listings_log.csv",
        "snapshot": root / "listings_snapshot.json",
        "market": root / "market_log",
    }


def hour_stamp(now):
    """UTC hour floor, matching market_log's ts so the files join on it."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    hour = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return hour, hour.strftime("%Y-%m-%dT%H:%M:%SZ")


def seen_stamp(now):
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _opt_float(value):
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num or num in (float("inf"), float("-inf")):
        return None
    return num


def _leverage(value):
    num = _opt_float(value)
    if num is None:
        return None
    if num == int(num):
        return int(num)
    return num


def _volume(asset):
    vol = _opt_float(asset.get("dayNtlVlm"))
    return 0.0 if vol is None else vol


def select_coins(assets, top_n=BOOK_TOP_N, min_ntl=BOOK_MIN_DAY_NTL):
    """Top `top_n` by 24h notional, plus any other perp above `min_ntl`.

    Main and HIP-3 share one ranking. A delisted name is left out. Volume at
    exactly `min_ntl` does not get the extra seat; the top slice still includes it.
    """
    ranked = []
    for asset in assets or []:
        if not isinstance(asset, dict) or asset.get("isDelisted"):
            continue
        if not asset.get("coin"):
            continue
        ranked.append(asset)
    ranked.sort(key=lambda a: (-_volume(a), a.get("dex") or "", a.get("coin") or ""))
    chosen = []
    seen = set()
    for i, asset in enumerate(ranked):
        if i >= top_n and _volume(asset) <= min_ntl:
            continue
        key = (asset.get("dex") or "main", asset["coin"])
        if key in seen:
            continue
        seen.add(key)
        chosen.append(asset)
    return chosen


def aggregation_step(price, n_sig_figs):
    """Bucket width of an l2Book aggregated to `n_sig_figs`, or None if full."""
    if n_sig_figs is None or price is None or price <= 0:
        return None
    exp = math.floor(math.log10(price))
    return 10.0 ** (exp - int(n_sig_figs) + 1)


def levels_cover(price, n_sig_figs, band, levels=BOOK_LEVELS):
    """True when `levels` buckets reach `band` (fraction of price) from the touch."""
    step = aggregation_step(price, n_sig_figs)
    if step is None or price is None or price <= 0 or band <= 0:
        return False
    return (levels - 1) * step >= price * band * (1 - 1e-9)


def band_resolved(price, n_sig_figs, band):
    """A bucket wider than the band cannot separate depth inside it.

    Full precision (n_sig_figs None) resolves every band. Coverage is separate:
    twenty raw levels often stay inside 0.5% on a liquid book.
    """
    if price is None or price <= 0 or band <= 0:
        return False
    step = aggregation_step(price, n_sig_figs)
    if step is None:
        return True
    return step <= price * band * (1 + 1e-9)


def aggregated_sigfigs(price):
    """Finest nSigFigs that both resolves and covers each depth band.

    One price often needs two aggregations: 4 for the 0.5% band and 3 for 2%.
    A band that no aggregation can resolve is omitted rather than filled with
    a bucket that straddles the band.
    """
    chosen = []
    if price is None or price <= 0:
        return tuple(chosen)
    for _name, band in BANDS:
        pick = None
        for n in (5, 4, 3, 2):
            if band_resolved(price, n, band) and levels_cover(price, n, band):
                pick = n
                break
        if pick is not None and pick not in chosen:
            chosen.append(pick)
    return tuple(chosen)


def _parse_side(raw):
    out = []
    if not isinstance(raw, list):
        return out
    for lvl in raw:
        if not isinstance(lvl, dict):
            continue
        px = _opt_float(lvl.get("px"))
        sz = _opt_float(lvl.get("sz"))
        if px is None or sz is None or px <= 0 or sz < 0:
            continue
        out.append({"px": px, "sz": sz})
    return out


def parse_book(payload):
    """l2Book object to {bids, asks, time}. None when the payload has no levels."""
    if not isinstance(payload, dict):
        return None
    levels = payload.get("levels")
    if not isinstance(levels, list) or len(levels) < 2:
        return None
    bids = _parse_side(levels[0])
    asks = _parse_side(levels[1])
    if not bids and not asks:
        return None
    try:
        when = int(payload.get("time"))
    except (TypeError, ValueError):
        when = None
    return {"bids": bids, "asks": asks, "time": when}


def book_mid_spread(book):
    """Mid and spread in bps from the inside quote. None if a side is missing."""
    if not book or not book.get("bids") or not book.get("asks"):
        return None, None
    bid = max(lvl["px"] for lvl in book["bids"])
    ask = min(lvl["px"] for lvl in book["asks"])
    if bid <= 0 or ask < bid:
        return None, None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None, None
    return mid, (ask - bid) / mid * 10_000.0


def depth_side(levels, mid, band, side, cap=BOOK_LEVELS):
    """Notional (px * sz) of levels whose price sits inside the band.

    Returns (notional, complete). complete is false when the book was cut at
    `cap` levels while the worst level was still strictly inside the band:
    the sum is then a lower bound. An empty side is zero and complete.
    """
    if mid is None or mid <= 0 or band <= 0 or levels is None:
        return None, False
    if side == "bid":
        edge = mid * (1.0 - band)
        ordered = sorted(levels, key=lambda lvl: lvl["px"], reverse=True)

        def inside(px):
            return px >= edge * (1 - 1e-12)
    else:
        edge = mid * (1.0 + band)
        ordered = sorted(levels, key=lambda lvl: lvl["px"])

        def inside(px):
            return px <= edge * (1 + 1e-12)
    if not ordered:
        return 0.0, True
    total = 0.0
    for lvl in ordered:
        if not inside(lvl["px"]):
            return total, True
        total += lvl["px"] * lvl["sz"]
    if len(ordered) < cap:
        return total, True
    worst = ordered[-1]["px"]
    if side == "bid":
        on_edge = worst <= edge * (1 + 1e-9)
    else:
        on_edge = worst >= edge * (1 - 1e-9)
    return total, on_edge


def imbalance(bid, ask):
    if bid is None or ask is None:
        return None
    denom = bid + ask
    if denom <= 0:
        return None
    return (bid - ask) / denom


def choose_depth(mid, band, tight, books):
    """Finest resolved book that covers the band.

    A full-precision book that runs out of levels inside the band loses to a
    coarser book that reaches the edge. The return is
    (bid_ntl, ask_ntl, complete, sig) with sig None for full precision.
    """
    candidates = []
    if tight:
        candidates.append((None, tight))
    for n in sorted((k for k, book in (books or {}).items() if book), reverse=True):
        candidates.append((n, books[n]))
    partial = None
    for sig, book in candidates:
        if not band_resolved(mid, sig, band):
            continue
        bid, bid_ok = depth_side(book.get("bids"), mid, band, "bid")
        ask, ask_ok = depth_side(book.get("asks"), mid, band, "ask")
        if bid is None or ask is None:
            continue
        measurement = (bid, ask, bool(bid_ok and ask_ok), sig)
        if bid_ok and ask_ok:
            return measurement
        if partial is None:
            partial = measurement
    return partial


def _book_time(tight, books):
    for book in (tight, *[books[n] for n in sorted(books) if books.get(n)]):
        if book and book.get("time"):
            return paper.iso(book["time"])
    return ""


def orderbook_row(asset, tight, books, hour_iso):
    """One CSV row, or None when neither book has a usable inside quote."""
    mid, spread, aggregated = None, None, 0
    tmid, tspread = book_mid_spread(tight)
    if tmid is not None:
        mid, spread = tmid, tspread
    else:
        for n in sorted((k for k, book in (books or {}).items() if book), reverse=True):
            amid, aspread = book_mid_spread(books[n])
            if amid is not None:
                mid, spread, aggregated = amid, aspread, 1
                break
    if mid is None or spread is None:
        return None
    books = books or {}
    row = {
        "ts": hour_iso,
        "dex": asset.get("dex") or "main",
        "coin": asset.get("coin") or "",
        "book_time": _book_time(tight, books),
        "mid": mid,
        "spread_bps": spread,
        "spread_aggregated": aggregated,
        "dayNtlVlm": _opt_float(asset.get("dayNtlVlm")),
    }
    for name, band in BANDS:
        measurement = choose_depth(mid, band, tight, books)
        if measurement is None:
            row[f"bid_ntl_{name}"] = None
            row[f"ask_ntl_{name}"] = None
            row[f"imb_{name}"] = None
            row[f"complete_{name}"] = 0
            row[f"sig_{name}"] = ""
            continue
        bid, ask, complete, sig = measurement
        row[f"bid_ntl_{name}"] = bid
        row[f"ask_ntl_{name}"] = ask
        row[f"imb_{name}"] = imbalance(bid, ask)
        row[f"complete_{name}"] = 1 if complete else 0
        row[f"sig_{name}"] = "full" if sig is None else str(sig)
    return row


def _post_book(post, coin, n_sig):
    body = {"type": "l2Book", "coin": coin}
    if n_sig is not None:
        body["nSigFigs"] = int(n_sig)
    return parse_book(post(body))


def fetch_orderbook_row(asset, post, hour_iso):
    """Full-precision book plus the aggregations this mark needs. Per-call errors skip that book."""
    coin = asset.get("coin")
    hint = _opt_float(asset.get("mark"))
    tight = None
    books = {}
    errors = []
    try:
        tight = _post_book(post, coin, None)
    except Exception as exc:
        errors.append(exc)
    mid = None
    if tight:
        mid, _spread = book_mid_spread(tight)
    if mid is None:
        mid = hint
    for n in aggregated_sigfigs(mid):
        try:
            books[n] = _post_book(post, coin, n)
        except Exception as exc:
            errors.append(exc)
            books[n] = None
    row = orderbook_row(asset, tight, books, hour_iso)
    return row, errors


def _error_label(err):
    text = str(err)
    label, sep, _rest = text.partition(": ")
    if not sep:
        return ""
    return label.strip()


def rows_from_scan(snap, scanned):
    scanned = set(scanned)
    found = {}
    for asset in (snap or {}).get("assets") or []:
        if not isinstance(asset, dict) or asset.get("isDelisted"):
            continue
        coin = asset.get("coin")
        dex = asset.get("dex") or "main"
        if not coin or dex not in scanned:
            continue
        found[(dex, coin)] = {
            "dex": dex,
            "coin": coin,
            "max_leverage": _leverage(asset.get("maxLeverage")),
        }
    return [found[key] for key in sorted(found)]


def diff_listings(previous, current):
    """Listing, delisting and max-leverage events. Both inputs are listed names only."""
    prev = {(row["dex"], row["coin"]): row for row in previous or []}
    cur = {(row["dex"], row["coin"]): row for row in current or []}
    events = []
    for key in sorted(set(cur) - set(prev)):
        row = cur[key]
        events.append({
            "dex": row["dex"], "coin": row["coin"], "event": "listing",
            "max_leverage": row.get("max_leverage"), "prev_max_leverage": None,
        })
    for key in sorted(set(prev) & set(cur)):
        old = prev[key].get("max_leverage")
        new = cur[key].get("max_leverage")
        if old == new:
            continue
        if old is None and new is None:
            continue
        events.append({
            "dex": key[0], "coin": key[1], "event": "max_leverage",
            "max_leverage": new, "prev_max_leverage": old,
        })
    for key in sorted(set(prev) - set(cur)):
        row = prev[key]
        events.append({
            "dex": row["dex"], "coin": row["coin"], "event": "delisting",
            "max_leverage": None, "prev_max_leverage": row.get("max_leverage"),
        })
    return events


def plan_listing_update(previous, snap):
    """New snapshot rows and events.

    `previous` is None when no snapshot has been stored yet: that hour writes
    the picture and emits nothing (the whole universe is not a listing wave).
    A dex whose meta call failed is copied forward and is not judged. When
    perpDexs itself failed, HIP-3 names that were not queried are copied too.
    A dex that perpDexs no longer returns is a delisting of each of its names.
    """
    baseline = previous is None
    prev_rows = [] if previous is None else list(previous)
    labels_failed = set()
    perp_dexs_failed = False
    for err in (snap or {}).get("errors") or []:
        label = _error_label(err)
        if label == "perpDexs":
            perp_dexs_failed = True
        elif label:
            labels_failed.add(label)
    scanned = []
    for dex in (snap or {}).get("dexs") or []:
        if dex not in labels_failed:
            scanned.append(dex)
    scanned_set = set(scanned)
    known = set((snap or {}).get("dexs") or [])
    current = rows_from_scan(snap, scanned_set)
    judged_prev = []
    carried = []
    for row in prev_rows:
        dex = row.get("dex") or "main"
        if dex in labels_failed:
            carried.append(row)
        elif dex in scanned_set:
            judged_prev.append(row)
        elif not perp_dexs_failed and dex not in known:
            judged_prev.append(row)
        else:
            carried.append(row)
    events = [] if baseline else diff_listings(judged_prev, current)
    merged = {}
    for row in carried + current:
        merged[(row["dex"], row["coin"])] = {
            "dex": row["dex"],
            "coin": row["coin"],
            "max_leverage": _leverage(row.get("max_leverage")),
        }
    rows = [merged[key] for key in sorted(merged)]
    return rows, events


def is_proxy_hit(oi_drop, gap_h, range_pct, vol_mult):
    """OI down, wide hour, and heavy volume. The gap must be exactly one hour."""
    if gap_h != 1:
        return False
    if oi_drop is None or oi_drop + 1e-12 < OI_DROP_MIN:
        return False
    if range_pct is None or range_pct + 1e-12 < RANGE_MIN:
        return False
    if vol_mult is None or vol_mult + 1e-12 < VOL_MULT_MIN:
        return False
    return True


def closed_hour(candles, now_ms):
    """Last closed 1h candle and up to 24 closed hours before it."""
    closed, _forming = paper.split_candles(candles or [], now_ms, paper.HOUR_MS)
    if not closed:
        return None, []
    latest = closed[-1]
    prior = closed[:-1][-VOL_LOOKBACK:]
    return latest, prior


def proxy_row(asset, candle, prior, prev, hour_iso):
    range_pct = None
    vol_usd = None
    candle_t = ""
    if candle and candle.get("o"):
        range_pct = (candle["h"] - candle["l"]) / candle["o"]
        vol_usd = candle["v"] * candle["c"]
        candle_t = paper.iso(candle["t"])
    samples = []
    for bar in prior or []:
        if bar.get("c"):
            samples.append(bar["v"] * bar["c"])
    vol_mult = None
    if vol_usd is not None and len(samples) >= MIN_VOL_SAMPLE:
        avg = sum(samples) / len(samples)
        if avg > 0:
            vol_mult = vol_usd / avg
    oi_now = _opt_float(asset.get("oi_coins"))
    oi_usd_now = _opt_float(asset.get("oi_usd"))
    oi_prev = prev.get("oi") if prev else None
    oi_usd_prev = prev.get("oi_usd") if prev else None
    gap = prev.get("gap_h") if prev else None
    oi_drop = None
    if oi_now is not None and oi_prev is not None and oi_prev > 0:
        oi_drop = (oi_prev - oi_now) / oi_prev
    return {
        "ts": hour_iso,
        "dex": asset.get("dex") or "main",
        "coin": asset.get("coin") or "",
        "oi": oi_now,
        "oi_prev": oi_prev,
        "oi_usd": oi_usd_now,
        "oi_usd_prev": oi_usd_prev,
        "oi_gap_h": gap,
        "oi_drop_pct": oi_drop,
        "candle_t": candle_t,
        "range_pct": range_pct,
        "vol_usd": vol_usd,
        "vol_mult": vol_mult,
        "vol_samples": len(samples),
        "proxy": 1 if is_proxy_hit(oi_drop, gap, range_pct, vol_mult) else 0,
        "dayNtlVlm": _opt_float(asset.get("dayNtlVlm")),
    }


def classify_trade(trade):
    """Name of the liquidation marker, or None.

    WsTrade, and a live recentTrades print, have no such field. userFills use
    `liquidation` (an object with liquidatedUser, markPx, method). This checks
    that key so a public payload that grows the same field is stored, and a
    trade without it is not turned into a liquidation.
    """
    if not isinstance(trade, dict) or "liquidation" not in trade:
        return None
    marker = trade.get("liquidation")
    if marker is None or marker is False or marker == "" or marker == 0:
        return None
    if isinstance(marker, dict) and not marker:
        return None
    return "liquidation"


def absorb_message(message, state):
    """Count public trades and keep those with a liquidation marker."""
    if not isinstance(message, dict) or message.get("channel") != "trades":
        return
    for trade in message.get("data") or []:
        if not isinstance(trade, dict):
            continue
        state["trades"] += 1
        if not classify_trade(trade):
            continue
        if len(state["flagged"]) >= LIQ_FLAG_CAP:
            state["capped"] = True
            continue
        state["flagged"].append(trade)


def listen_trades(coins, seconds):
    """Public trades websocket for `seconds`. Returns counts and flagged trades."""
    import trade_tape

    seconds = max(1, int(seconds))
    state = {"trades": 0, "flagged": [], "capped": False}
    sock, leftover = trade_tape.connect()
    conn = trade_tape.Connection(sock, leftover)
    try:
        for coin in coins:
            conn.send_json({
                "method": "subscribe",
                "subscription": {"type": "trades", "coin": coin},
            })
        deadline = time.time() + seconds
        last_ping = time.time()
        while time.time() < deadline:
            timeout = min(5.0, max(0.2, deadline - time.time()))
            for message in conn.pull(timeout):
                absorb_message(message, state)
            now = time.time()
            if now - last_ping >= 20 and now < deadline:
                conn.send_json({"method": "ping"})
                last_ping = now
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return {
        "seconds": seconds,
        "coins": len(list(coins)),
        "trades": state["trades"],
        "flagged": state["flagged"],
        "capped": state["capped"],
    }


def lookup_oi(directory, dex, coin, hour_ms, max_gap_h=OI_MAX_GAP_H):
    """Latest market_log OI strictly before `hour_ms`, within `max_gap_h`."""
    directory = Path(directory)
    if hour_ms is None or not directory.exists():
        return None
    hour = datetime.fromtimestamp(hour_ms / 1000, timezone.utc)
    best = None
    for delta in range(0, 2):
        day = (hour - timedelta(days=delta)).date()
        path = directory / f"{day:%Y-%m-%d}.csv"
        if not path.exists():
            continue
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("coin") != coin or (row.get("dex") or "main") != dex:
                    continue
                ts = paper.parse_iso(row.get("ts"))
                if ts is None or ts >= hour_ms:
                    continue
                gap_h = (hour_ms - ts) / paper.HOUR_MS
                if gap_h > max_gap_h + 1e-9:
                    continue
                if best is None or ts > best["ts"]:
                    best = {
                        "ts": ts,
                        "gap_h": int(round(gap_h)),
                        "oi": _opt_float(row.get("oi")),
                        "oi_usd": _opt_float(row.get("oi_usd")),
                    }
    return best


def _day_path(directory, hour):
    return Path(directory) / f"{hour:%Y-%m-%d}.csv"


def hour_logged(path, ts):
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return False
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("ts") == ts:
                return True
    return False


def _emit(row, fields, text_fields):
    out = {}
    for key in fields:
        value = row.get(key)
        if key in text_fields:
            out[key] = "" if value is None else str(value)
        else:
            out[key] = paper.csv_num(value)
    return out


def append_rows(path, rows, fields, text_fields, ts):
    """Append rows for one hour. A second call in that hour writes nothing."""
    path = Path(path)
    if not rows or hour_logged(path, ts):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists() or path.stat().st_size == 0
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    if fresh:
        writer.writeheader()
    writer.writerows(_emit(row, fields, text_fields) for row in rows)
    with path.open("a", newline="", encoding="utf-8") as fh:
        fh.write(buf.getvalue())
    return True


def _map(fn, items, workers):
    items = list(items)
    if not items:
        return []
    workers = max(1, min(int(workers), len(items)))
    if workers == 1:
        out = []
        for item in items:
            try:
                out.append(fn(item))
            except Exception as exc:
                out.append(exc)
        return out
    results = [None] * len(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [(i, pool.submit(fn, item)) for i, item in enumerate(items)]
        for i, fut in futures:
            try:
                results[i] = fut.result()
            except Exception as exc:
                results[i] = exc
    return results


def _load_snapshot(path):
    path = Path(path)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("coins"), list):
        raise ValueError("listings_snapshot con forma inesperada")
    rows = []
    for item in data["coins"]:
        if not isinstance(item, dict) or not item.get("coin"):
            continue
        rows.append({
            "dex": item.get("dex") or "main",
            "coin": item["coin"],
            "max_leverage": _leverage(item.get("max_leverage")),
        })
    return rows


def _recent_listing_keys(path, now):
    found = set()
    path = Path(path)
    if not path.exists():
        return found
    cutoff = now.astimezone(timezone.utc).timestamp() - LISTING_DEDUP_S
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            ts = paper.parse_iso(row.get("ts"))
            if ts is None or ts / 1000 < cutoff:
                continue
            found.add((
                row.get("dex") or "", row.get("coin") or "", row.get("event") or "",
                row.get("max_leverage") or "", row.get("prev_max_leverage") or "",
            ))
    return found


def _write_snapshot(path, rows, now):
    payload = {
        "updated_at": seen_stamp(now),
        "coins": [
            {
                "dex": row["dex"],
                "coin": row["coin"],
                "max_leverage": row.get("max_leverage"),
            }
            for row in rows
        ],
    }
    paper.atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=1) + "\n")


def update_listings(snap, now, paths):
    """Diff, append events, store the snapshot. Returns (wrote, event_count, note)."""
    previous = _load_snapshot(paths["snapshot"])
    rows, events = plan_listing_update(previous, snap)
    stamp = seen_stamp(now)
    fresh = []
    if events:
        seen = _recent_listing_keys(paths["listings"], now)
        for event in events:
            event = dict(event, ts=stamp)
            emitted = _emit(event, LISTING_FIELDS, LISTING_TEXT)
            key = (
                emitted["dex"], emitted["coin"], emitted["event"],
                emitted["max_leverage"], emitted["prev_max_leverage"],
            )
            if key in seen:
                continue
            fresh.append(event)
            seen.add(key)
        if fresh:
            append_listing_rows(paths["listings"], fresh)
    _write_snapshot(paths["snapshot"], rows, now)
    if previous is None:
        note = "- Listados: foto inicial, sin eventos."
    else:
        note = f"- Listados: {_cuenta(len(fresh), 'evento', 'eventos')}."
    return True, len(fresh), note


def append_listing_rows(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists() or path.stat().st_size == 0
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=LISTING_FIELDS, lineterminator="\n")
    if fresh:
        writer.writeheader()
    writer.writerows(_emit(row, LISTING_FIELDS, LISTING_TEXT) for row in rows)
    with path.open("a", newline="", encoding="utf-8") as fh:
        fh.write(buf.getvalue())


def _fetch_candles(asset, now_ms, post):
    start = int(now_ms) - CANDLE_HOURS * paper.HOUR_MS
    raw = post({"type": "candleSnapshot", "req": {
        "coin": asset["coin"],
        "interval": "1h",
        "startTime": start,
        "endTime": int(now_ms),
    }})
    return paper.normalize_candles(raw, paper.HOUR_MS)


def write_orderbooks(assets, now, post, paths, workers):
    hour, ts = hour_stamp(now)
    path = _day_path(paths["orderbook"], hour)
    if hour_logged(path, ts):
        return False, "- Libro de órdenes: esta hora ya estaba anotada."
    coins = select_coins(assets)
    failures = []

    def one(asset):
        return fetch_orderbook_row(asset, post, ts)

    rows = []
    for asset, result in zip(coins, _map(one, coins, workers)):
        if isinstance(result, Exception):
            failures.append(f"{asset.get('coin')}: {result}")
            continue
        row, errors = result
        for err in errors:
            failures.append(f"{asset.get('coin')}: {err}")
        if row:
            rows.append(row)
    wrote = append_rows(path, rows, ORDERBOOK_FIELDS, ORDERBOOK_TEXT, ts)
    if wrote:
        paper.prune_market_log(paths["orderbook"], hour.date(), KEEP_DAYS)
    if failures:
        paper.log(f"research libro: {len(failures)} avisos, el primero: {failures[0]}")
    if not coins:
        return False, "- Libro de órdenes: universo vacío."
    return wrote, f"- Libro de órdenes: {_cuenta(len(rows), 'fila', 'filas')}."


def write_proxies(assets, now, post, paths, workers):
    hour, ts = hour_stamp(now)
    path = _day_path(paths["proxy"], hour)
    if hour_logged(path, ts):
        return False, "- Proxy de liquidaciones: esta hora ya estaba anotada."
    coins = select_coins(assets)
    if not coins:
        return False, "- Proxy de liquidaciones: universo vacío."
    # At :02 the bar that opened on the previous hour has closed. split_candles
    # keeps the bar that opened on this hour as still forming.
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_ms = int(now.astimezone(timezone.utc).timestamp() * 1000)
    failures = []
    hour_ms = int(hour.timestamp() * 1000)

    def one(asset):
        err = None
        candles = None
        try:
            candles = _fetch_candles(asset, now_ms, post)
        except Exception as exc:
            err = exc
        latest, prior = closed_hour(candles or [], now_ms)
        prev = lookup_oi(
            paths["market"], asset.get("dex") or "main", asset["coin"], hour_ms,
        )
        return proxy_row(asset, latest, prior, prev, ts), err

    rows = []
    hits = 0
    for asset, result in zip(coins, _map(one, coins, workers)):
        if isinstance(result, Exception):
            failures.append(f"{asset.get('coin')}: {result}")
            rows.append(proxy_row(asset, None, [], None, ts))
            continue
        row, err = result
        if err is not None:
            failures.append(f"{asset.get('coin')}: {err}")
        rows.append(row)
        hits += int(row.get("proxy") or 0)
    wrote = append_rows(path, rows, PROXY_FIELDS, PROXY_TEXT, ts)
    if wrote:
        paper.prune_market_log(paths["proxy"], hour.date(), KEEP_DAYS)
    if failures:
        paper.log(f"research proxy: {len(failures)} velas no leídas, la primera: {failures[0]}")
    return wrote, f"- Proxy de liquidaciones: {_cuenta(len(rows), 'fila', 'filas')}, patrón={hits}."


def _dex_of(coin, dex_by_coin):
    if coin in dex_by_coin:
        return dex_by_coin[coin]
    if isinstance(coin, str) and ":" in coin:
        return coin.split(":", 1)[0]
    return "main"


def trade_rows(flagged, dex_by_coin, hour_iso):
    rows = []
    for trade in flagged or []:
        if not isinstance(trade, dict):
            continue
        marker = classify_trade(trade)
        if not marker:
            continue
        px = _opt_float(trade.get("px"))
        sz = _opt_float(trade.get("sz"))
        notional = px * sz if px is not None and sz is not None else None
        try:
            trade_time = paper.iso(int(trade.get("time")))
        except (TypeError, ValueError):
            trade_time = ""
        coin = trade.get("coin") or ""
        tid = trade.get("tid")
        rows.append({
            "ts": hour_iso,
            "dex": _dex_of(coin, dex_by_coin),
            "coin": coin,
            "side": trade.get("side") or "",
            "px": px,
            "sz": sz,
            "notional": notional,
            "trade_time": trade_time,
            "tid": "" if tid is None else str(tid),
            "hash": trade.get("hash") or "",
            "marker": marker,
        })
    return rows


def write_tape(result, assets, now, paths):
    """Store the sample counts and any trades that carried a liquidation field."""
    hour, ts = hour_stamp(now)
    path = _day_path(paths["tape"], hour)
    dex_by_coin = {
        asset["coin"]: asset.get("dex") or "main"
        for asset in assets or [] if isinstance(asset, dict) and asset.get("coin")
    }
    flagged = trade_rows(result.get("flagged") or [], dex_by_coin, ts)
    wrote = False
    if flagged:
        trade_path = _day_path(paths["trades"], hour)
        if append_rows(trade_path, flagged, TRADE_FIELDS, TRADE_TEXT, ts):
            wrote = True
            paper.prune_market_log(paths["trades"], hour.date(), KEEP_DAYS)
    if hour_logged(path, ts):
        return wrote, "- Cinta de trades: esta hora ya estaba anotada."
    row = {
        "ts": ts,
        "seconds": result.get("seconds") or 0,
        "coins": result.get("coins") or 0,
        "trades": result.get("trades") or 0,
        "liq_flags": len(flagged),
    }
    if append_rows(path, [row], TAPE_FIELDS, TAPE_TEXT, ts):
        wrote = True
        paper.prune_market_log(paths["tape"], hour.date(), KEEP_DAYS)
    if result.get("capped"):
        paper.log(f"research cinta: tope de {LIQ_FLAG_CAP} trades marcados")
    note = (
        f"- Cinta de trades: {_cuenta(row['coins'], 'moneda', 'monedas')}, {row['seconds']}s, "
        f"trades={row['trades']}, marcados={row['liq_flags']}."
    )
    return wrote, note


def _start_tape(coins, listen):
    holder = {"result": None, "error": None}

    def run():
        try:
            holder["result"] = listen(coins, TAPE_SECONDS)
        except Exception as exc:
            holder["error"] = exc

    thread = threading.Thread(target=run, name="liq-tape", daemon=True)
    thread.start()
    return thread, holder


def collect(assets, snap, now, post=None, listen=None, root=None, workers=None):
    """Write the research logs. Each section fails on its own.

    Returns (wrote_anything, summary_lines). `post` and `listen` default to
    the public info API and the public trades websocket.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    post = post or perps_all.post
    listen = listen or listen_trades
    workers = WORKERS if workers is None else workers
    paths = data_paths(root)
    notes = []
    wrote = False

    try:
        _wrote, _count, note = update_listings(snap or {}, now, paths)
        wrote = wrote or _wrote
        notes.append(note)
        paper.log(note.lstrip("- ").rstrip("."))
    except Exception as exc:
        paper.log(f"research listados: {exc}")
        notes.append("- Listados: fallo, el paper sigue.")

    hour, ts = hour_stamp(now)
    tape_thread = None
    tape_holder = None
    coins = [asset["coin"] for asset in select_coins(assets)[:TAPE_TOP_N]]
    if coins and not hour_logged(_day_path(paths["tape"], hour), ts):
        tape_thread, tape_holder = _start_tape(coins, listen)

    try:
        section_wrote, note = write_orderbooks(assets, now, post, paths, workers)
        wrote = wrote or section_wrote
        notes.append(note)
        paper.log(note.lstrip("- ").rstrip("."))
    except Exception as exc:
        paper.log(f"research libro: {exc}")
        notes.append("- Libro de órdenes: fallo, el paper sigue.")

    try:
        section_wrote, note = write_proxies(assets, now, post, paths, workers)
        wrote = wrote or section_wrote
        notes.append(note)
        paper.log(note.lstrip("- ").rstrip("."))
    except Exception as exc:
        paper.log(f"research proxy: {exc}")
        notes.append("- Proxy de liquidaciones: fallo, el paper sigue.")

    try:
        if tape_thread is not None:
            tape_thread.join()
            if tape_holder.get("error") is not None:
                raise tape_holder["error"]
            section_wrote, note = write_tape(tape_holder.get("result") or {}, assets, now, paths)
        elif hour_logged(_day_path(paths["tape"], hour), ts):
            section_wrote, note = False, "- Cinta de trades: esta hora ya estaba anotada."
        elif not coins:
            section_wrote, note = False, "- Cinta de trades: universo vacío."
        else:
            section_wrote, note = False, "- Cinta de trades: sin muestra."
        wrote = wrote or section_wrote
        notes.append(note)
        paper.log(note.lstrip("- ").rstrip("."))
    except Exception as exc:
        paper.log(f"research cinta: {exc}")
        notes.append("- Cinta de trades: fallo, el paper sigue.")
    return wrote, notes
