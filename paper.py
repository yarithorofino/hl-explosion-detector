#!/usr/bin/env python3
"""Paper accounting shared by trend4h, squeeze4h, trapped_shorts1h and c1_maker.

Public Hyperliquid info only. This module never places an order and never
reads an exchange key. Webhook URL and auth, when present, come from the
environment (EXPLOSION_WEBHOOK_URL, EXPLOSION_WEBHOOK_AUTH) and are not logged.
"""
import copy
import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import detector
import perps_all

ROOT = Path(__file__).resolve().parent
HOUR_MS = 3_600_000
FOUR_H_MS = 4 * HOUR_MS
EQUITY_USDC = 10_000.0
RISK_PCT = 0.0025
RISK_USD = EQUITY_USDC * RISK_PCT
FEE_PER_SIDE = 0.0007
FUNDING_SLACK_MS = 5_000
MARKET_LOG_KEEP_DAYS = 120
LIQUID_MIN_DAY_NTL = 5_000_000.0

TRADE_FIELDS = (
    "status", "coin", "side", "signal_time", "entry_time", "exit_time",
    "entry", "exit", "stop", "atr", "ema100", "ema200",
    "size_coins", "notional", "risk_usd", "gross_pnl", "fees", "funding_pnl",
    "net_pnl", "r", "exit_reason", "mark_entry", "mark_exit", "funding_ok",
)
MARKET_FIELDS = (
    "ts", "dex", "coin", "mark", "oi", "oi_usd", "funding", "premium", "dayNtlVlm",
)
BOOKS = ("trend4h", "squeeze4h", "trapped_shorts1h", "c1_maker")
# c1_maker risks 0.25% of 1,000 USDC and pays maker/taker fees of its own.
# fee_per_side here is the maker entry fee; the book stores the taker fee too.
BOOK_PARAMS = {
    "c1_maker": {
        "equity_usdc": 1000.0,
        "risk_pct": 0.0025,
        "fee_per_side": 0.00015,
    },
}


def log(msg):
    text = detector.redact(msg)
    if len(text) > 500:
        text = text[:500] + "..."
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {text}"
    print(line, flush=True)


def iso(ms):
    if ms is None:
        return ""
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    if not text:
        return None
    try:
        dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def jnum(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return float(format(float(value), ".10g"))


def csv_num(value):
    n = jnum(value)
    if n is None:
        return ""
    return format(n, ".12g")


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = text.encode("utf-8")
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


def ema_series(values, period):
    """EMA seeded with the SMA of the first `period` closes. None before that."""
    period = int(period)
    if values is None or len(values) < period or period < 1:
        return None
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out = [None] * (period - 1) + [seed]
    prev = seed
    for v in values[period:]:
        prev = v * k + prev * (1.0 - k)
        out.append(prev)
    return out


def wilder_atr(candles, period):
    """Wilder ATR aligned to candles. First value sits on index `period`.

    True range uses the previous close. The seed is the SMA of the first
    `period` true ranges that have a previous close (indexes 1..period).
    """
    period = int(period)
    if not candles or len(candles) < period + 1:
        return [None] * len(candles or [])
    prev_close = candles[0].get("c")
    if prev_close is None:
        return [None] * len(candles)
    trs = [max(candles[0]["h"] - candles[0]["l"], 0.0)]
    for c in candles[1:]:
        trs.append(max(c["h"] - c["l"], abs(c["h"] - prev_close), abs(c["l"] - prev_close)))
        prev_close = c["c"]
    atr = [None] * len(candles)
    first = sum(trs[1:period + 1]) / period
    atr[period] = first
    prev = first
    for i in range(period + 1, len(candles)):
        prev = (prev * (period - 1) + trs[i]) / period
        atr[i] = prev
    return atr


def bandwidth_series(closes, period=20, nstd=2.0):
    """Bollinger bandwidth (upper-lower)/mid. Population stdev, SMA mid."""
    out = [None] * len(closes)
    period = int(period)
    if period < 2:
        return out
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        mid = sum(window) / period
        if mid <= 0:
            continue
        var = sum((x - mid) ** 2 for x in window) / period
        out[i] = (2.0 * float(nstd) * (var ** 0.5)) / mid
    return out


def percentile_rank_series(values, lookback):
    """Rank of values[i] inside the last `lookback` points, on 0..1.

    Fraction of the other points in the window that are strictly lower.
    Ties do not count as lower. None until the window is complete.
    """
    out = [None] * len(values)
    lookback = int(lookback)
    if lookback < 2:
        return out
    for i in range(lookback - 1, len(values)):
        window = values[i - lookback + 1:i + 1]
        if any(v is None for v in window):
            continue
        less = sum(1 for v in window[:-1] if v < window[-1])
        out[i] = less / (lookback - 1)
    return out


def cross_side(fast, slow, i):
    """EMA cross on candle i. 'long' if fast crosses above slow, 'short' below."""
    if not fast or not slow or i < 1 or i >= len(fast) or i >= len(slow):
        return None
    a0, b0, a1, b1 = fast[i - 1], slow[i - 1], fast[i], slow[i]
    if None in (a0, b0, a1, b1):
        return None
    if a0 <= b0 and a1 > b1:
        return "long"
    if a0 >= b0 and a1 < b1:
        return "short"
    return None


def stop_fill_price(side, stop, bar):
    """Fill if the closed bar's high/low hits a long or short stop.

    A bar that opens through the stop fills at the open (the gap). Otherwise
    the fill is the stop.
    """
    if side == "long":
        if bar["l"] <= stop:
            return bar["o"] if bar["o"] <= stop else stop
    elif side == "short":
        if bar["h"] >= stop:
            return bar["o"] if bar["o"] >= stop else stop
    return None


def ratchet_long_stop(stop, highest_high, atr, t_mult):
    """Trail = highest high since entry minus t*ATR. Never moves down."""
    if atr is None or atr <= 0 or t_mult is None:
        return stop
    return max(stop, highest_high - float(t_mult) * atr)


def funding_anchor(ts):
    """Pull a print that lands a few ms after the hour back onto that hour."""
    mod = int(ts) % HOUR_MS
    if mod <= FUNDING_SLACK_MS:
        return int(ts) - mod
    return int(ts)


def funding_pnl(side, entry, size, entry_t, exit_t, points):
    """Hourly funding on entry notional. Positive funding is paid by longs.

    A settlement stamped a few milliseconds after an hour counts for the
    position that was open into that hour, not for one that opens on it.
    """
    if not points or exit_t is None or entry_t is None or exit_t <= entry_t:
        return 0.0
    sign = 1.0 if side == "long" else -1.0
    notional = size * entry
    total = 0.0
    for ts, rate in points:
        anchor = funding_anchor(ts)
        if entry_t < anchor <= exit_t:
            total += -sign * float(rate) * notional
    return total


def account(pos, exit_px, exit_t, points):
    entry = pos["entry"]
    size = pos["size"]
    if pos["side"] == "long":
        gross = (exit_px - entry) * size
    else:
        gross = (entry - exit_px) * size
    fees = FEE_PER_SIDE * size * (entry + exit_px)
    if points is None:
        fund = 0.0
        ok = False
    else:
        fund = funding_pnl(pos["side"], entry, size, pos["entry_t"], exit_t, points)
        ok = True
    net = gross - fees + fund
    risk = pos["risk_usd"] or RISK_USD
    r = net / risk if risk else None
    return {
        "gross_pnl": gross, "fees": fees, "funding_pnl": fund, "net_pnl": net,
        "r": r, "funding_ok": ok,
    }


def normalize_candles(raw, interval_ms):
    by_t = {}
    if not isinstance(raw, list):
        return []
    for c in raw:
        if not isinstance(c, dict):
            continue
        try:
            t = int(c["t"])
            o, h, l, cl = (float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"]))
            v = float(c.get("v") or 0)
        except (TypeError, ValueError, KeyError):
            continue
        if t % interval_ms != 0 or min(o, h, l, cl) <= 0 or h < l or v < 0:
            continue
        by_t[t] = {"t": t, "o": o, "h": h, "l": l, "c": cl, "v": v}
    return [by_t[t] for t in sorted(by_t)]


def split_candles(candles, now_ms, interval_ms):
    """Closed candles, and the one  interval that has opened and not closed."""
    boundary = int(now_ms) - (int(now_ms) % interval_ms)
    closed = [c for c in candles if c["t"] + interval_ms <= now_ms and c["t"] < boundary]
    forming = None
    for c in candles:
        if c["t"] == boundary and c["t"] + interval_ms > now_ms:
            forming = c
            break
    return closed, forming


def latest_closed_open(now_ms, interval_ms):
    boundary = int(now_ms) - (int(now_ms) % interval_ms)
    return boundary - interval_ms


def index_series(candles, atr=None):
    return {
        "candles": candles,
        "by_t": {c["t"]: i for i, c in enumerate(candles)},
        "atr": atr if atr is not None else wilder_atr(candles, 14),
    }


def _spec_from_signal(coin, bar, sig, fill_t, reverse):
    return {
        "coin": coin,
        "fill_t": fill_t,
        "side": sig["side"],
        "atr": sig["atr"],
        "ema100": sig.get("ema100"),
        "ema200": sig.get("ema200"),
        "signal_t": bar["t"],
        "reverse": bool(reverse),
    }


def _row_base(status, coin, side):
    row = {k: "" for k in TRADE_FIELDS}
    row["status"] = status
    row["coin"] = coin
    row["side"] = side or ""
    return row


def simulate(series, signal_fn, book, cfg, *, process_from, latest_closed,
             forming_t, forming_opens, marks, universe, funding):
    """Walk closed candles and return the next book plus csv rows and alerts.

    Fills that belong to the still-forming candle use its open, or the mark
    when the open is not in the snapshot yet. Pending orders are returned so
    a later run can fill them. `funding[coin]` is a list of (ts, rate) or
    None when the history could not be read.
    """
    positions = {p["coin"]: copy.deepcopy(p) for p in (book.get("positions") or [])}
    pending_entries = {p["coin"]: copy.deepcopy(p) for p in (book.get("pending_entries") or [])}
    pending_exits = {p["coin"]: copy.deepcopy(p) for p in (book.get("pending_exits") or [])}
    rows = []
    events = []
    order = {coin: i for i, coin in enumerate(universe)}
    universe_set = set(universe)
    interval = int(cfg["interval"])
    max_hold = int(cfg["max_hold"])
    max_positions = int(cfg["max_positions"])
    k_mult = float(cfg["k"])
    t_mult = cfg.get("t_mult")
    name = cfg["book"]

    def bar_of(coin, t):
        prep = series.get(coin)
        if not prep:
            return None, None
        i = prep["by_t"].get(t)
        if i is None:
            return None, None
        return prep["candles"][i], i

    def price_of(coin, t):
        bar, _ = bar_of(coin, t)
        if bar is not None:
            return bar["o"], "open"
        if forming_t is not None and t == forming_t:
            opened = (forming_opens or {}).get(coin)
            if opened:
                return opened, "open"
            mark = (marks or {}).get(coin)
            if mark:
                return mark, "mark"
        return None, None

    def mark_of(coin):
        if not marks:
            return None
        return marks.get(coin)

    def close_pos(pos, exit_px, exit_t, reason, candle_ms):
        if funding is None:
            points = None
        else:
            points = funding.get(pos["coin"], [])
        acct = account(pos, exit_px, exit_t, points)
        row = _row_base("closed", pos["coin"], pos["side"])
        row.update({
            "signal_time": iso(pos.get("signal_t")),
            "entry_time": iso(pos.get("entry_t")),
            "exit_time": iso(exit_t),
            "entry": csv_num(pos["entry"]),
            "exit": csv_num(exit_px),
            "stop": csv_num(pos["stop"]),
            "atr": csv_num(pos.get("atr")),
            "ema100": csv_num(pos.get("ema100")),
            "ema200": csv_num(pos.get("ema200")),
            "size_coins": csv_num(pos["size"]),
            "notional": csv_num(pos["size"] * pos["entry"]),
            "risk_usd": csv_num(pos["risk_usd"]),
            "gross_pnl": csv_num(acct["gross_pnl"]),
            "fees": csv_num(acct["fees"]),
            "funding_pnl": csv_num(acct["funding_pnl"]) if acct["funding_ok"] else "",
            "net_pnl": csv_num(acct["net_pnl"]),
            "r": csv_num(acct["r"]),
            "exit_reason": reason,
            "mark_entry": csv_num(pos.get("mark_entry")),
            "mark_exit": csv_num(mark_of(pos["coin"])),
            "funding_ok": "1" if acct["funding_ok"] else "0",
        })
        rows.append(row)
        events.append({
            "type": f"{name}_exit",
            "paper": True,
            "coin": pos["coin"],
            "side": pos["side"],
            "entry": jnum(pos["entry"]),
            "exit": jnum(exit_px),
            "stop": jnum(pos["stop"]),
            "atr": jnum(pos.get("atr")),
            "ema100": jnum(pos.get("ema100")),
            "ema200": jnum(pos.get("ema200")),
            "candle_time": iso(candle_ms),
            "signal_time": iso(pos.get("signal_t")),
            "entry_time": iso(pos.get("entry_t")),
            "exit_time": iso(exit_t),
            "r": jnum(acct["r"]),
            "exit_reason": reason,
            "fees": jnum(acct["fees"]),
            "funding_pnl": jnum(acct["funding_pnl"]) if acct["funding_ok"] else None,
            "funding": "ok" if acct["funding_ok"] else "missing",
            "net_pnl": jnum(acct["net_pnl"]),
            "mark": jnum(mark_of(pos["coin"])),
            "event_id": f"{name}_exit:{pos['coin']}:{pos.get('entry_t')}:{reason}:{exit_t}",
        })
        positions.pop(pos["coin"], None)
        pending_exits.pop(pos["coin"], None)

    def skip_cap(spec, px):
        stop = px - k_mult * spec["atr"] if spec["side"] == "long" else px + k_mult * spec["atr"]
        row = _row_base("skipped_cap", spec["coin"], spec["side"])
        row.update({
            "signal_time": iso(spec.get("signal_t")),
            "entry_time": iso(spec.get("fill_t")),
            "entry": csv_num(px),
            "stop": csv_num(stop),
            "atr": csv_num(spec.get("atr")),
            "ema100": csv_num(spec.get("ema100")),
            "ema200": csv_num(spec.get("ema200")),
            "risk_usd": csv_num(cfg.get("risk_usd") or RISK_USD),
            "exit_reason": "skipped_cap",
            "funding_ok": "",
        })
        rows.append(row)

    def open_pos(spec, px):
        atr = spec["atr"]
        if spec["side"] == "long":
            stop = px - k_mult * atr
        else:
            stop = px + k_mult * atr
        dist = abs(px - stop)
        if dist <= 0 or stop <= 0:
            return
        risk = float(cfg.get("risk_usd") or RISK_USD)
        size = risk / dist
        pos = {
            "coin": spec["coin"],
            "side": spec["side"],
            "entry": px,
            "stop": stop,
            "initial_stop": stop,
            "atr": atr,
            "ema100": spec.get("ema100"),
            "ema200": spec.get("ema200"),
            "signal_t": spec.get("signal_t"),
            "entry_t": spec["fill_t"],
            "size": size,
            "risk_usd": risk,
            "mark_entry": mark_of(spec["coin"]),
            "highest_high": px if t_mult else None,
        }
        positions[spec["coin"]] = pos
        events.append({
            "type": f"{name}_entry",
            "paper": True,
            "coin": spec["coin"],
            "side": spec["side"],
            "entry": jnum(px),
            "stop": jnum(stop),
            "atr": jnum(atr),
            "ema100": jnum(spec.get("ema100")),
            "ema200": jnum(spec.get("ema200")),
            "candle_time": iso(spec.get("signal_t")),
            "signal_time": iso(spec.get("signal_t")),
            "entry_time": iso(spec["fill_t"]),
            "mark": jnum(pos["mark_entry"]),
            "size_coins": jnum(size),
            "risk_usd": jnum(risk),
            "event_id": f"{name}_entry:{spec['coin']}:{spec.get('signal_t')}:{spec['fill_t']}",
        })

    def fill_exits(t):
        for coin, pos in list(positions.items()):
            px, _kind = price_of(coin, t)
            if px is None:
                continue
            pend = pending_exits.get(coin)
            time_due = t >= pos["entry_t"] + max_hold * interval
            if pend and pend.get("fill_t", t + 1) <= t:
                close_pos(pos, px, t, pend.get("reason") or "reverse", t)
            elif time_due:
                close_pos(pos, px, t, "time", t)

    def fill_entries(t):
        ready = []
        for coin, spec in list(pending_entries.items()):
            if spec.get("fill_t") != t:
                continue
            if coin in positions:
                pending_entries.pop(coin, None)
                continue
            px, _kind = price_of(coin, t)
            if px is None:
                continue
            ready.append((spec, px))
        ready.sort(key=lambda item: (0 if item[0].get("reverse") else 1, order.get(item[0]["coin"], 10 ** 9)))
        for spec, px in ready:
            pending_entries.pop(spec["coin"], None)
            if spec["coin"] in positions:
                continue
            if len(positions) >= max_positions:
                skip_cap(spec, px)
                continue
            open_pos(spec, px)

    def signal_at(coin, i, bar):
        if coin not in universe_set:
            return None
        sig = signal_fn(coin, i)
        if not sig or sig.get("side") not in ("long", "short"):
            return None
        if t_mult and sig.get("side") != "long":
            return None
        atr = sig.get("atr")
        if atr is None:
            series_atr = (series.get(coin) or {}).get("atr") or []
            atr = series_atr[i] if i < len(series_atr) else None
        if atr is None or atr <= 0:
            return None
        out = dict(sig)
        out["atr"] = atr
        out.setdefault("signal_t", bar["t"])
        return out

    def evaluate(t):
        seen = set()
        coins = []
        for coin in list(universe) + list(positions):
            if coin in seen:
                continue
            seen.add(coin)
            bar, i = bar_of(coin, t)
            if bar is None:
                continue
            coins.append((coin, bar, i))
        for coin, bar, i in coins:
            pos = positions.get(coin)
            sig = signal_at(coin, i, bar)
            stopped = False
            if pos:
                fill = stop_fill_price(pos["side"], pos["stop"], bar)
                if fill is not None:
                    gap = (
                        (pos["side"] == "long" and bar["o"] <= pos["stop"])
                        or (pos["side"] == "short" and bar["o"] >= pos["stop"])
                    )
                    exit_t = bar["t"] if gap else bar["t"] + interval
                    close_pos(pos, fill, exit_t, "stop", bar["t"])
                    pos = None
                    stopped = True
                else:
                    if t_mult and pos["side"] == "long":
                        atr_now = None
                        series_atr = (series.get(coin) or {}).get("atr") or []
                        if i < len(series_atr):
                            atr_now = series_atr[i]
                        hh = max(pos.get("highest_high") or pos["entry"], bar["h"])
                        pos["highest_high"] = hh
                        pos["stop"] = ratchet_long_stop(pos["stop"], hh, atr_now, t_mult)
                    if sig and sig["side"] != pos["side"]:
                        nxt = t + interval
                        pending_exits[coin] = {"coin": coin, "fill_t": nxt, "reason": "reverse"}
                        pending_entries[coin] = _spec_from_signal(coin, bar, sig, nxt, True)
                        sig = None
                    else:
                        sig = None
            if pos is None and sig and coin not in pending_entries:
                nxt = t + interval
                pending_entries[coin] = _spec_from_signal(coin, bar, sig, nxt, stopped)

    if (
        process_from is not None and latest_closed is not None
        and interval > 0 and process_from <= latest_closed
    ):
        t = int(process_from)
        while t <= int(latest_closed):
            fill_exits(t)
            fill_entries(t)
            evaluate(t)
            t += interval
    if forming_t is not None:
        fill_exits(forming_t)
        fill_entries(forming_t)

    return {
        "positions": [positions[c] for c in sorted(positions)],
        "pending_entries": [pending_entries[c] for c in sorted(pending_entries)],
        "pending_exits": [pending_exits[c] for c in sorted(pending_exits)],
        "rows": rows,
        "events": events,
    }


def book_params(strategy):
    """Equity, risk and the recorded per-side fee for one book."""
    return dict(BOOK_PARAMS.get(strategy) or {
        "equity_usdc": EQUITY_USDC,
        "risk_pct": RISK_PCT,
        "fee_per_side": FEE_PER_SIDE,
    })


def empty_book(strategy):
    params = book_params(strategy)
    return {
        "version": 1,
        "strategy": strategy,
        "equity_usdc": params["equity_usdc"],
        "risk_pct": params["risk_pct"],
        "fee_per_side": params["fee_per_side"],
        "last_processed_open_ms": None,
        "last_run": None,
        "positions": [],
        "pending_entries": [],
        "pending_exits": [],
    }


def book_dir(strategy):
    return ROOT / "data" / strategy


def load_book(strategy):
    path = book_dir(strategy) / "open.json"
    if not path.exists():
        return empty_book(strategy)
    data = json.loads(path.read_text(encoding="utf-8"))
    base = empty_book(strategy)
    if isinstance(data, dict):
        base.update(data)
    base["strategy"] = strategy
    for key in ("positions", "pending_entries", "pending_exits"):
        if not isinstance(base.get(key), list):
            base[key] = []
    return base


def save_book(strategy, book):
    book = dict(book)
    book["strategy"] = strategy
    book.update(book_params(strategy))
    text = json.dumps(book, ensure_ascii=False, indent=1, sort_keys=True)
    atomic_write(book_dir(strategy) / "open.json", text + "\n")


def _dedupe_key(row):
    return (
        row.get("status"), row.get("coin"), row.get("side"),
        row.get("signal_time"), row.get("entry_time"), row.get("exit_time"),
        row.get("exit_reason"),
    )


def write_trades(strategy, rows, fieldnames=None):
    """Append rows. `fieldnames` defaults to the shared book columns."""
    fieldnames = tuple(fieldnames or TRADE_FIELDS)
    path = book_dir(strategy) / "trades.csv"
    existing = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as fh:
            existing = list(csv.DictReader(fh))
    seen = {_dedupe_key(r) for r in existing}
    for row in rows:
        key = _dedupe_key(row)
        if key in seen:
            continue
        existing.append(row)
        seen.add(key)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for row in existing:
        writer.writerow({k: row.get(k, "") for k in fieldnames})
    atomic_write(path, buf.getvalue())
    return path


def read_trades(strategy):
    path = book_dir(strategy) / "trades.csv"
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _fnum(row, key):
    try:
        return float(row.get(key) or "")
    except (TypeError, ValueError):
        return None


def summarize_book(rows, positions, now_ms):
    closed = [r for r in rows if r.get("status") == "closed"]
    skipped = [r for r in rows if r.get("status") == "skipped_cap"]
    rs = [v for v in (_fnum(r, "r") for r in closed) if v is not None]
    wins = sum(1 for v in rs if v > 0)
    week_cut = int(now_ms) - 7 * 24 * HOUR_MS
    week_rs = []
    for r in closed:
        exit_ms = parse_iso(r.get("exit_time"))
        val = _fnum(r, "r")
        if exit_ms is not None and exit_ms >= week_cut and val is not None:
            week_rs.append(val)
    def pack(values):
        if not values:
            return {"closed_trades": 0, "cumulative_r": 0.0, "win_rate": None}
        return {
            "closed_trades": len(values),
            "cumulative_r": jnum(sum(values)),
            "win_rate": jnum(sum(1 for v in values if v > 0) / len(values)),
        }
    opens = []
    for pos in positions or []:
        opens.append({
            "coin": pos.get("coin"),
            "side": pos.get("side"),
            "entry": jnum(pos.get("entry")),
            "stop": jnum(pos.get("stop")),
            "atr": jnum(pos.get("atr")),
            "ema100": jnum(pos.get("ema100")),
            "ema200": jnum(pos.get("ema200")),
            "candle_time": iso(pos.get("signal_t")),
            "entry_time": iso(pos.get("entry_t")),
            "paper": True,
        })
    all_time = pack(rs)
    # pack() counts only rows with a numeric R. Keep the raw closed count too.
    all_time["closed_trades"] = len(closed)
    if rs:
        all_time["win_rate"] = jnum(wins / len(rs))
        all_time["cumulative_r"] = jnum(sum(rs))
    week = pack(week_rs)
    return {
        "open_positions": opens,
        "open_count": len(opens),
        "closed_trades": len(closed),
        "skipped_cap": len(skipped),
        "cumulative_r": all_time["cumulative_r"],
        "win_rate": all_time["win_rate"],
        "week_closed_trades": week["closed_trades"],
        "week_cumulative_r": week["cumulative_r"],
        "week_win_rate": week["win_rate"],
    }


def as_utc(now):
    """Naive timestamps are UTC. Aware ones are converted."""
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def iso_week_id(now):
    """ISO week label, such as 2026-W47. The week-year can differ from the calendar year."""
    year, week, _weekday = as_utc(now).isocalendar()
    return f"{year}-W{week:02d}"


def weekly_due(now, last_sent):
    """True on the first run at or after Monday 08:00 UTC of an unsent ISO week.

    A run at 07:29 UTC stays quiet. 09:21 UTC the same Monday is due, and so
    is Tuesday when Monday never ran. `last_sent` is the ISO week id, not a date.
    """
    now = as_utc(now)
    monday_open = (now - timedelta(days=now.weekday())).replace(
        hour=8, minute=0, second=0, microsecond=0,
    )
    if now < monday_open:
        return False
    return last_sent != iso_week_id(now)


def liquid_coins(assets, min_ntl=LIQUID_MIN_DAY_NTL):
    """Main-dex names with 24h notional at or above the floor. No HIP-3."""
    names = []
    for asset in assets or []:
        if asset.get("dex") != "main" or asset.get("isDelisted"):
            continue
        coin = asset.get("coin")
        try:
            vol = float(asset.get("dayNtlVlm") or 0)
        except (TypeError, ValueError):
            continue
        if coin and vol >= min_ntl:
            names.append(coin)
    return sorted(set(names))


def market_log_dir():
    return ROOT / "data" / "market_log"


def append_market_log(assets, now, directory=None):
    """Append one hourly row per perp. A second run in the same hour is a no-op."""
    directory = Path(directory) if directory is not None else market_log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    hour = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    ts = hour.strftime("%Y-%m-%dT%H:%M:%SZ")
    path = directory / f"{hour:%Y-%m-%d}.csv"
    if path.exists():
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("ts") == ts:
                    return False
    fresh = not path.exists() or path.stat().st_size == 0
    rows = []
    for asset in sorted(assets or [], key=lambda a: (a.get("dex") or "", a.get("coin") or "")):
        coin = asset.get("coin")
        if not coin:
            continue
        rows.append({
            "ts": ts,
            "dex": asset.get("dex") or "main",
            "coin": coin,
            "mark": csv_num(asset.get("mark")),
            "oi": csv_num(asset.get("oi_coins")),
            "oi_usd": csv_num(asset.get("oi_usd")),
            "funding": csv_num(asset.get("funding")),
            "premium": csv_num(asset.get("premium")),
            "dayNtlVlm": csv_num(asset.get("dayNtlVlm")),
        })
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=MARKET_FIELDS, lineterminator="\n")
        if fresh:
            writer.writeheader()
        writer.writerows(rows)
    prune_market_log(directory, now.date())
    return True


def prune_market_log(directory, today, keep_days=MARKET_LOG_KEEP_DAYS):
    cutoff = today - timedelta(days=keep_days)
    for path in Path(directory).glob("*.csv"):
        try:
            day = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            path.unlink()


def snapshot_span_ms(directory):
    """Earliest and latest snapshot timestamps, or (None, None)."""
    directory = Path(directory)
    files = sorted(p for p in directory.glob("*.csv") if _is_day_name(p.name))
    if not files:
        return None, None
    first = _first_ts(files[0])
    last = _last_ts(files[-1])
    return first, last


def _is_day_name(name):
    return len(name) == 14 and name.endswith(".csv") and name[4] == "-" and name[7] == "-"


def _first_ts(path):
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            return parse_iso(row.get("ts"))
    return None


def _last_ts(path):
    last = None
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            parsed = parse_iso(row.get("ts"))
            if parsed is not None:
                last = parsed
    return last


def spans_warmup(directory, hours=168):
    first, last = snapshot_span_ms(directory)
    if first is None or last is None:
        return False
    return last - first >= hours * HOUR_MS


def oi_near(directory, coin, target_ms, tolerance_ms=3 * HOUR_MS, dex="main"):
    """Coin open interest from the hourly log, at or just before `target_ms`."""
    directory = Path(directory)
    if target_ms is None:
        return None
    start = datetime.fromtimestamp((target_ms - tolerance_ms) / 1000, timezone.utc).date()
    end = datetime.fromtimestamp(target_ms / 1000, timezone.utc).date()
    best = None
    best_ts = None
    day = start
    while day <= end:
        path = directory / f"{day:%Y-%m-%d}.csv"
        day += timedelta(days=1)
        if not path.exists():
            continue
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("coin") != coin or (row.get("dex") or "main") != dex:
                    continue
                ts = parse_iso(row.get("ts"))
                if ts is None or ts > target_ms or target_ms - ts > tolerance_ms:
                    continue
                if best_ts is None or ts > best_ts:
                    try:
                        best = float(row.get("oi") or "")
                    except ValueError:
                        continue
                    best_ts = ts
    return best


def load_runner():
    path = ROOT / "data" / "paper" / "runner.json"
    if not path.exists():
        return {"last_weekly_sent": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"last_weekly_sent": None}
    if not isinstance(data, dict):
        return {"last_weekly_sent": None}
    sent = data.get("last_weekly_sent")
    if not isinstance(sent, str) or not sent:
        sent = None
    return {"last_weekly_sent": sent}


def save_runner(state):
    atomic_write(
        ROOT / "data" / "paper" / "runner.json",
        json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
    )


def post_webhook(payload):
    """POST a JSON list. Dry-run when the detector secrets are absent."""
    cfg = {"webhook_auth_header": "Authorization", "webhook_auth_format": "Bearer {key}"}
    return detector.send(payload, cfg)


def mark_ready(summary):
    atomic_write(ROOT / "data" / "run_summary.txt", summary + "\n")
    atomic_write(ROOT / "data" / "state_ready", "ok\n")


def write_summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines).rstrip() + "\n")
    except OSError as exc:
        log(f"no se pudo escribir el resumen del job: {exc}")


def fetch_candles(coin, interval, start_ms, end_ms):
    data = perps_all.post({"type": "candleSnapshot", "req": {
        "coin": coin, "interval": interval, "startTime": int(start_ms), "endTime": int(end_ms),
    }})
    if not isinstance(data, list):
        raise RuntimeError("velas no son una lista")
    return data


def fetch_funding(coin, start_ms, end_ms):
    """Hourly fundingRate decimals from start_ms (exclusive) through end_ms."""
    rows = []
    cursor = int(start_ms)
    end_ms = int(end_ms)
    last = cursor
    for _ in range(20):
        data = perps_all.post({"type": "fundingHistory", "coin": coin, "startTime": cursor})
        if not isinstance(data, list) or not data:
            break
        page_last = last
        for item in data:
            try:
                when = int(item.get("time"))
                rate = float(item.get("fundingRate"))
            except (TypeError, ValueError):
                continue
            page_last = max(page_last, when)
            if start_ms < when <= end_ms + FUNDING_SLACK_MS:
                rows.append((when, rate))
        if page_last <= last or page_last >= end_ms:
            break
        last = page_last
        cursor = page_last + 1
    rows.sort()
    deduped = []
    for row in rows:
        if deduped and deduped[-1][0] == row[0]:
            deduped[-1] = row
        else:
            deduped.append(row)
    return deduped


def main_listing(raw):
    """Split a metaAndAssetCtxs payload into listed and delisted main-dex names."""
    if not isinstance(raw, (list, tuple)) or len(raw) < 1 or not isinstance(raw[0], dict):
        return set(), set()
    listed, delisted = set(), set()
    for uni in raw[0].get("universe") or []:
        name = uni.get("name")
        if not name:
            continue
        if uni.get("isDelisted"):
            delisted.add(name)
        else:
            listed.add(name)
    return listed, delisted
