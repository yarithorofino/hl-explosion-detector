#!/usr/bin/env python3
"""C1 pump-fade short with a maker limit. Paper and alerts only.

Evaluated on closed 1h candles of every main-dex Hyperliquid perp. No
liquidity filter and no exchange keys. This module never places an order.
The live book is variant A. B and C are the same engine with other stops,
targets, holds and, for C, a looser drawdown.

A bar can signal only when the 48 hourly closes that end on it, and the 24
hourly highs that end on it, are contiguous. A missing hour invalidates
that window.

- up48 = close / min(close of those 48 bars) - 1 >= 0.40
- dd24 = close / max(high of those 24 bars) - 1 <= dd24_max
  (A and B: -0.06, at least 6% under the 24h high, no upper bound.
   C: -0.03.)
- ret1h = close / previous close - 1 <= -0.03

up48 >= 1 (a 100% rise in closes) sets `ta_extreme` and does not change
the order. A new signal on a coin is ignored when it falls inside 12 bars
of that coin's previous signal, or while that coin already has a resting
limit or an open position.

The order is a sell limit at the signal close + 0.25 * ATR(14), live for
the next 3 bars. It fills only when a bar's high is strictly above
limit * (1 + 0.0005). The fill price is the limit and the fee is maker
0.015%. There is no touch-fill.

After the fill, 1R is the distance from the entry to the stop.
Variant A: stop = entry + 1.5 * ATR, target = entry - 1R, hold 12 bars.
Variant B: stop = entry + 2 * ATR, target = entry - 1R, hold 24 bars.
Variant C: the looser drawdown above, stop = entry + 2 * ATR,
target = entry - 1.5R, hold 36 bars.

The stop is a taker. It may fire on the fill bar. The fill is the stop,
or the open when the bar opens through it, worsened by 0.01% slippage.
The fee on that price is 0.045%. The target is a maker buy. It cannot
fill on the entry bar, and it fills only when the low trades strictly
below target * (1 - 0.0005), at the target price, maker 0.015%. A bar
that reaches both exits at the stop. The time exit is the close of the
last hold bar, worsened by the same 0.01% and charged the taker fee.
Funding is added with the same sign as the other paper books. One coin
at a time, at most 10 coins, each risking 0.25% of 1,000 USDC.

An order that never fills is logged with a taker entry at the next open
(same slippage, stop, target and hold) so the fill can be compared with
the trade the limit refused.
"""
import json

import paper

BOOK = "c1_maker"
UP_WINDOW = 48
DD_WINDOW = 24
UP48_MIN = 0.40
UP48_EXTREME = 1.0
RET1H_MAX = -0.03
LIMIT_ATR = 0.25
ATR_PERIOD = 14
VALID_BARS = 3
SIGNAL_GAP = 12
MAX_POSITIONS = 10
EQUITY_USDC = 1000.0
RISK_PCT = 0.0025
RISK_USD = EQUITY_USDC * RISK_PCT
FEE_MAKER = 0.00015
FEE_TAKER = 0.00045
# Strict trade-through. 0.05% stands in for one tick.
TRADE_THROUGH = 0.0005
# Taker slippage, the same 0.01% the confluence research applies as a worse fill.
SLIPPAGE = 0.0001
BOOK_LEVELS = 20
FETCH_BARS = 400
LIVE_VARIANT = "A"

FEE_RATE = {"maker": FEE_MAKER, "taker": FEE_TAKER}

# dd24_max is the most-positive drawdown that still qualifies (a more
# negative reading is a deeper fade). tp_r is the target in units of the
# initial stop distance.
VARIANTS = {
    "A": {
        "name": "A",
        "dd24_max": -0.06,
        "sl_atr": 1.5,
        "tp_r": 1.0,
        "max_hold": 12,
    },
    "B": {
        "name": "B",
        "dd24_max": -0.06,
        "sl_atr": 2.0,
        "tp_r": 1.0,
        "max_hold": 24,
    },
    "C": {
        "name": "C",
        "dd24_max": -0.03,
        "sl_atr": 2.0,
        "tp_r": 1.5,
        "max_hold": 36,
    },
}

TRADE_FIELDS = (
    "status", "coin", "side", "variant", "signal_time", "entry_time", "exit_time",
    "limit_px", "entry", "exit", "sl", "tp", "atr",
    "up48", "dd24", "ret1h", "ta_extreme", "day_ntl_vlm",
    "size_coins", "notional", "risk_usd",
    "gross_pnl", "fees", "funding_pnl", "net_pnl", "r",
    "exit_reason", "filled", "funding_ok",
    "hypothetical_entry", "hypothetical_exit", "hypothetical_exit_reason", "hypothetical_r",
    "best_ask", "ask_depth_ntl", "ask_depth_complete", "limit_vs_spread", "l2_snaps",
)


def variant(name=LIVE_VARIANT, **overrides):
    """Copy of a research variant. Unknown names raise KeyError."""
    cfg = dict(VARIANTS[name])
    cfg.update(overrides)
    cfg["name"] = name
    return cfg


def prepare(candles):
    return paper.index_series(candles, paper.wilder_atr(candles, ATR_PERIOD))


def _contiguous(candles, start, end):
    """candles[start:end] inclusive, or None when an hour is missing."""
    if start < 0 or end >= len(candles) or start > end:
        return None
    chunk = candles[start:end + 1]
    for prev, nxt in zip(chunk, chunk[1:]):
        if nxt["t"] - prev["t"] != paper.HOUR_MS:
            return None
    return chunk


def detect(candles, i, atr, cfg=None):
    """Signal dict for closed bar i, or None. `atr` is ATR(14) on that bar."""
    cfg = cfg or variant()
    if atr is None or atr <= 0 or not candles or i < 1 or i >= len(candles):
        return None
    up = _contiguous(candles, i - (UP_WINDOW - 1), i)
    down = _contiguous(candles, i - (DD_WINDOW - 1), i)
    if not up or not down:
        return None
    bar = candles[i]
    prev = candles[i - 1]
    if bar["c"] <= 0 or prev["c"] <= 0 or bar["t"] - prev["t"] != paper.HOUR_MS:
        return None
    min_close = min(c["c"] for c in up)
    max_high = max(c["h"] for c in down)
    if min_close <= 0 or max_high <= 0:
        return None
    up48 = bar["c"] / min_close - 1.0
    dd24 = bar["c"] / max_high - 1.0
    ret1h = bar["c"] / prev["c"] - 1.0
    if up48 + 1e-12 < UP48_MIN:
        return None
    if dd24 - 1e-12 > float(cfg["dd24_max"]):
        return None
    if ret1h - 1e-12 > RET1H_MAX:
        return None
    limit = bar["c"] + LIMIT_ATR * float(atr)
    if limit <= 0:
        return None
    return {
        "side": "short",
        "up48": up48,
        "dd24": dd24,
        "ret1h": ret1h,
        "min_close": min_close,
        "max_high": max_high,
        "atr": float(atr),
        "limit": limit,
        "ta_extreme": up48 + 1e-12 >= UP48_EXTREME,
        "signal_close": bar["c"],
        "signal_t": bar["t"],
    }


def traded_through_above(high, limit, extra=TRADE_THROUGH):
    """True when the high is strictly above limit * (1 + extra)."""
    if high is None or limit is None or limit <= 0:
        return False
    return high > limit * (1.0 + float(extra))


def traded_through_below(low, limit, extra=TRADE_THROUGH):
    """True when the low is strictly below limit * (1 - extra)."""
    if low is None or limit is None or limit <= 0:
        return False
    return low < limit * (1.0 - float(extra))


def planned_levels(entry, atr, cfg=None):
    """Stop above entry, and a target `tp_r` times that distance below it."""
    cfg = cfg or variant()
    dist = float(cfg["sl_atr"]) * float(atr)
    return entry + dist, entry - float(cfg["tp_r"]) * dist


def size_coins(entry, sl, risk_usd=RISK_USD):
    dist = sl - entry
    if dist <= 0 or risk_usd <= 0:
        return None
    return float(risk_usd) / dist


def worse_short(price, slippage=SLIPPAGE):
    """A taker short fill, pushed against the position by `slippage`."""
    return price * (1.0 + float(slippage))


def settle(entry, exit_px, size, entry_kind, exit_kind, risk_usd=RISK_USD,
           entry_t=None, exit_t=None, points=None):
    """Short PnL. `points` is funding (ts, rate) or None when it was not read.

    An empty list is a known history with no prints. Positive funding is
    received by the short, matching the other paper books.
    """
    gross = (entry - exit_px) * size
    fees = (
        FEE_RATE[entry_kind] * size * entry
        + FEE_RATE[exit_kind] * size * exit_px
    )
    if points is None:
        fund, ok = 0.0, False
    else:
        fund = paper.funding_pnl("short", entry, size, entry_t, exit_t, points)
        ok = True
    net = gross - fees + fund
    risk = float(risk_usd) if risk_usd else 0.0
    return {
        "gross_pnl": gross,
        "fees": fees,
        "funding_pnl": fund,
        "funding_ok": ok,
        "net_pnl": net,
        "r": (net / risk) if risk else None,
    }


def resolve_bar(leg, bar):
    """Exit of an open short on `bar`, or None.

    Returns (price, reason, fee kind). The stop may fire on the entry bar.
    The maker target may not, and it needs a trade-through. A bar that
    reaches both exits at the stop. The time exit is the close of the
    last hold bar.
    """
    if not leg or not bar or leg.get("exit_reason"):
        return None
    if bar["t"] < leg["entry_t"]:
        return None
    sl = leg["sl"]
    tp = leg["tp"]
    slip = float(leg.get("slippage") if leg.get("slippage") is not None else SLIPPAGE)
    extra = float(leg.get("trade_through") if leg.get("trade_through") is not None else TRADE_THROUGH)
    entry_bar = bar["t"] == leg["entry_t"]
    o, h, l, c = bar["o"], bar["h"], bar["l"], bar["c"]
    if o >= sl or h >= sl:
        base = o if o >= sl else sl
        return worse_short(base, slip), "sl", "taker"
    if not entry_bar and traded_through_below(l, tp, extra):
        return tp, "tp", "maker"
    held = int((bar["t"] - leg["entry_t"]) // paper.HOUR_MS) + 1
    if held >= int(leg.get("max_hold") or variant()["max_hold"]):
        return worse_short(c, slip), "time", "taker"
    return None


def limit_vs_spread(limit, best_bid, best_ask):
    """Where a sell limit sits relative to the inside quote.

    `through` crosses the bid, `inside` is at or inside the spread (including
    joining the ask), `beyond` rests strictly behind the best ask.
    """
    if limit is None or best_bid is None or best_ask is None or best_ask < best_bid:
        return "unknown"
    if limit <= best_bid:
        return "through"
    if limit <= best_ask:
        return "inside"
    return "beyond"


def ask_depth_within(asks, limit, cap=BOOK_LEVELS):
    """Ask notional at a price at or inside the limit.

    The second value is False when the visible book was cut at `cap` levels
    while every level was still at or inside the limit: the sum is then a
    lower bound. A missing book returns (None, False).
    """
    if asks is None or limit is None:
        return None, False
    ordered = sorted(asks, key=lambda lvl: lvl["px"])
    total = 0.0
    edge = limit * (1 + 1e-12)
    for lvl in ordered:
        if lvl["px"] <= edge:
            total += lvl["px"] * lvl["sz"]
        else:
            return total, True
    if len(ordered) >= int(cap):
        return total, False
    return total, True


def book_snapshot(limit, book, now_ms, cap=BOOK_LEVELS):
    """One l2 reading for a resting sell limit. `book` is a parsed l2Book."""
    bids = (book or {}).get("bids") if isinstance(book, dict) else None
    asks = (book or {}).get("asks") if isinstance(book, dict) else None
    best_bid = max((lvl["px"] for lvl in bids), default=None) if bids else None
    best_ask = min((lvl["px"] for lvl in asks), default=None) if asks else None
    depth, complete = ask_depth_within(asks, limit, cap)
    hour = int(now_ms) - (int(now_ms) % paper.HOUR_MS)
    return {
        "ts": paper.iso(hour),
        "best_bid": paper.jnum(best_bid),
        "best_ask": paper.jnum(best_ask),
        "ask_depth_ntl": paper.jnum(depth),
        "ask_depth_complete": 1 if complete else 0,
        "limit_vs_spread": limit_vs_spread(limit, best_bid, best_ask),
    }


def fetch_l2(coin):
    """Public l2Book. None when the payload has no levels. Never sends an order."""
    import market_research
    import perps_all
    payload = perps_all.post({"type": "l2Book", "coin": coin})
    return market_research.parse_book(payload)


def main_universe(assets):
    """Listed main-dex perps, with no volume floor. HIP-3 stays out."""
    names = []
    meta = {}
    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        if asset.get("dex") not in (None, "", "main"):
            continue
        if asset.get("isDelisted"):
            continue
        coin = asset.get("coin")
        if not coin:
            continue
        names.append(coin)
        meta[coin] = asset
    return sorted(set(names)), meta


def _day_volume(meta, coin):
    raw = (meta.get(coin) or {}).get("dayNtlVlm")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _bar(series, coin, t):
    prep = series.get(coin) or {}
    candles = prep.get("candles") or []
    i = (prep.get("by_t") or {}).get(t)
    if i is None or i >= len(candles):
        return None
    return candles[i]


def _rules(cfg):
    cfg = cfg or variant()
    return {
        "max_hold": int(cfg["max_hold"]),
        "trade_through": TRADE_THROUGH,
        "slippage": SLIPPAGE,
        "sl_atr": float(cfg["sl_atr"]),
        "tp_r": float(cfg["tp_r"]),
    }


def _make_leg(entry, atr, entry_t, model, entry_fee, rules, risk_usd=RISK_USD):
    cfg = {"sl_atr": rules["sl_atr"], "tp_r": rules["tp_r"]}
    sl, tp = planned_levels(entry, atr, cfg)
    size = size_coins(entry, sl, risk_usd)
    if size is None or entry <= 0 or sl <= 0 or tp <= 0:
        return None
    return {
        "model": model,
        "entry": entry,
        "entry_t": entry_t,
        "sl": sl,
        "tp": tp,
        "size": size,
        "risk_usd": float(risk_usd),
        "entry_fee": entry_fee,
        "max_hold": rules["max_hold"],
        "trade_through": rules["trade_through"],
        "slippage": rules["slippage"],
        "exit": None,
        "exit_t": None,
        "exit_reason": None,
        "exit_fee": None,
        "gross_pnl": None,
        "fees": None,
        "funding_pnl": None,
        "funding_ok": None,
        "net_pnl": None,
        "r": None,
    }


def _funding_points(funding, coin):
    """Funding rows for one coin.

    None as the whole map means the caller did not load funding: treat it
    as an empty, known history so tests stay on fees alone. A map that
    simply lacks the coin means the read failed.
    """
    if funding is None:
        return []
    if callable(funding):
        return funding(coin)
    if isinstance(funding, dict) and coin not in funding:
        return None
    return funding.get(coin)


def _apply_exit(leg, bar, price, reason, fee_kind, points):
    exit_t = bar["t"] + paper.HOUR_MS
    acct = settle(
        leg["entry"], price, leg["size"], leg["entry_fee"], fee_kind, leg["risk_usd"],
        entry_t=leg["entry_t"], exit_t=exit_t, points=points,
    )
    leg["exit"] = price
    leg["exit_t"] = exit_t
    leg["exit_reason"] = reason
    leg["exit_fee"] = fee_kind
    leg.update(acct)
    return leg


def _open_on_bar(sig, bar, model, points):
    """Open a maker leg at the limit, or a taker leg at the slipped open."""
    rules = sig["rules"]
    if model == "taker":
        # A short sold at market receives a worse (lower) price.
        entry = bar["o"] * (1.0 - rules["slippage"])
        fee = "taker"
    else:
        entry = sig["limit"]
        fee = "maker"
    leg = _make_leg(entry, sig["atr"], bar["t"], model, fee, rules)
    if leg is None:
        return None
    resolved = resolve_bar(leg, bar)
    if resolved:
        _apply_exit(leg, bar, *resolved, points)
    return leg


def _occupies(sig):
    if sig.get("window_open"):
        return True
    leg = sig.get("position")
    return bool(leg and not leg.get("exit_reason"))


def _done(sig):
    if sig.get("window_open"):
        return False
    for key in ("position", "shadow"):
        leg = sig.get(key)
        if leg and not leg.get("exit_reason"):
            return False
    return True


def _csv_flag(value):
    return "1" if value else "0"


def _blank_row():
    return {key: "" for key in TRADE_FIELDS}


def _row_from(sig, status):
    leg = sig.get("position")
    shadow = sig.get("shadow")
    primary = leg if status == "closed" else None
    sl, tp = planned_levels(sig["limit"], sig["atr"], {
        "sl_atr": sig["rules"]["sl_atr"], "tp_r": sig["rules"]["tp_r"],
    })
    row = _blank_row()
    row.update({
        "status": status,
        "coin": sig["coin"],
        "side": "short",
        "variant": sig.get("variant") or LIVE_VARIANT,
        "signal_time": paper.iso(sig.get("signal_t")),
        "entry_time": paper.iso(primary.get("entry_t")) if primary else "",
        "exit_time": paper.iso(primary.get("exit_t")) if primary else "",
        "limit_px": paper.csv_num(sig.get("limit")),
        "entry": paper.csv_num(primary.get("entry")) if primary else "",
        "exit": paper.csv_num(primary.get("exit")) if primary else "",
        "sl": paper.csv_num(primary.get("sl")) if primary else paper.csv_num(sl),
        "tp": paper.csv_num(primary.get("tp")) if primary else paper.csv_num(tp),
        "atr": paper.csv_num(sig.get("atr")),
        "up48": paper.csv_num(sig.get("up48")),
        "dd24": paper.csv_num(sig.get("dd24")),
        "ret1h": paper.csv_num(sig.get("ret1h")),
        "ta_extreme": _csv_flag(sig.get("ta_extreme")),
        "day_ntl_vlm": paper.csv_num(sig.get("day_ntl_vlm")),
        "size_coins": paper.csv_num(primary.get("size")) if primary else "",
        "notional": paper.csv_num(primary["size"] * primary["entry"]) if primary else "",
        "risk_usd": paper.csv_num(primary.get("risk_usd") if primary else RISK_USD),
        "gross_pnl": paper.csv_num(primary.get("gross_pnl")) if primary else "",
        "fees": paper.csv_num(primary.get("fees")) if primary else "",
        "funding_pnl": paper.csv_num(primary.get("funding_pnl")) if primary and primary.get("funding_ok") else "",
        "net_pnl": paper.csv_num(primary.get("net_pnl")) if primary else "",
        "r": paper.csv_num(primary.get("r")) if primary else "",
        "exit_reason": (primary or {}).get("exit_reason") or ("expired-unfilled" if status == "unfilled" else ""),
        "filled": _csv_flag(bool(leg)),
        "funding_ok": "1" if primary and primary.get("funding_ok") else ("0" if primary else ""),
        "hypothetical_entry": paper.csv_num(shadow.get("entry")) if shadow else "",
        "hypothetical_exit": paper.csv_num(shadow.get("exit")) if shadow else "",
        "hypothetical_exit_reason": (shadow or {}).get("exit_reason") or "",
        "hypothetical_r": paper.csv_num(shadow.get("r")) if shadow and shadow.get("exit_reason") else "",
        "best_ask": paper.csv_num(sig.get("best_ask")),
        "ask_depth_ntl": paper.csv_num(sig.get("ask_depth_ntl")),
        "ask_depth_complete": "" if sig.get("ask_depth_complete") is None else str(int(sig.get("ask_depth_complete"))),
        "limit_vs_spread": sig.get("limit_vs_spread") or "",
        "l2_snaps": json.dumps(sig.get("l2_snaps") or [], separators=(",", ":"), sort_keys=True),
    })
    return row


def _skipped_row(coin, found, volume, cfg):
    sl, tp = planned_levels(found["limit"], found["atr"], cfg)
    row = _blank_row()
    row.update({
        "status": "skipped_cap",
        "coin": coin,
        "side": "short",
        "variant": cfg["name"],
        "signal_time": paper.iso(found.get("signal_t")),
        "limit_px": paper.csv_num(found.get("limit")),
        "sl": paper.csv_num(sl),
        "tp": paper.csv_num(tp),
        "atr": paper.csv_num(found.get("atr")),
        "up48": paper.csv_num(found.get("up48")),
        "dd24": paper.csv_num(found.get("dd24")),
        "ret1h": paper.csv_num(found.get("ret1h")),
        "ta_extreme": _csv_flag(found.get("ta_extreme")),
        "day_ntl_vlm": paper.csv_num(volume),
        "risk_usd": paper.csv_num(RISK_USD),
        "exit_reason": "skipped_cap",
        "filled": "0",
        "l2_snaps": "[]",
    })
    return row


def _event(kind, sig, leg=None, exit_reason=None, r=None):
    sl, tp = planned_levels(sig["limit"], sig["atr"], {
        "sl_atr": sig["rules"]["sl_atr"], "tp_r": sig["rules"]["tp_r"],
    })
    if leg:
        sl, tp = leg["sl"], leg["tp"]
    event_id = f"{BOOK}_{kind}:{sig['coin']}:{sig.get('signal_t')}"
    if leg:
        event_id += f":{leg.get('model')}:{leg.get('entry_t')}"
    if exit_reason:
        event_id += f":{exit_reason}:{leg.get('exit_t') if leg else sig.get('expires_t')}"
    return {
        "type": f"{BOOK}_{kind}",
        "paper": True,
        "book": BOOK,
        "variant": sig.get("variant") or LIVE_VARIANT,
        "coin": sig["coin"],
        "side": "short",
        "side_label": "corto",
        "limit": paper.jnum(sig.get("limit")),
        "entry": paper.jnum(leg.get("entry")) if leg else None,
        "sl": paper.jnum(sl),
        "tp": paper.jnum(tp),
        "stop": paper.jnum(sl),
        "atr": paper.jnum(sig.get("atr")),
        "r": paper.jnum(r if r is not None else (leg.get("r") if leg and leg.get("exit_reason") else None)),
        "exit_reason": exit_reason,
        "exit": paper.jnum(leg.get("exit")) if leg and leg.get("exit") is not None else None,
        "ta_extreme": bool(sig.get("ta_extreme")),
        "up48": paper.jnum(sig.get("up48")),
        "dd24": paper.jnum(sig.get("dd24")),
        "ret1h": paper.jnum(sig.get("ret1h")),
        "dayNtlVlm": paper.jnum(sig.get("day_ntl_vlm")),
        "signal_time": paper.iso(sig.get("signal_t")),
        "entry_time": paper.iso(leg.get("entry_t")) if leg else None,
        "exit_time": paper.iso(leg.get("exit_t")) if leg and leg.get("exit_t") else None,
        "fill_model": leg.get("model") if leg else None,
        "best_ask": sig.get("best_ask"),
        "ask_depth_ntl": sig.get("ask_depth_ntl"),
        "limit_vs_spread": sig.get("limit_vs_spread"),
        "risk_usd": paper.jnum(RISK_USD),
        "funding_pnl": paper.jnum(leg.get("funding_pnl")) if leg and leg.get("funding_ok") else None,
        "event_id": event_id,
    }


def _new_signal(coin, found, volume, cfg):
    rules = _rules(cfg)
    sl, tp = planned_levels(found["limit"], found["atr"], cfg)
    return {
        "coin": coin,
        "side": "short",
        "variant": cfg["name"],
        "signal_t": found["signal_t"],
        "expires_t": found["signal_t"] + VALID_BARS * paper.HOUR_MS,
        "window_open": True,
        "limit": found["limit"],
        "atr": found["atr"],
        "sl": sl,
        "tp": tp,
        "up48": found["up48"],
        "dd24": found["dd24"],
        "ret1h": found["ret1h"],
        "ta_extreme": bool(found["ta_extreme"]),
        "day_ntl_vlm": volume,
        "rules": rules,
        "position": None,
        "shadow": None,
        "l2_snaps": [],
        "best_bid": None,
        "best_ask": None,
        "ask_depth_ntl": None,
        "ask_depth_complete": None,
        "limit_vs_spread": None,
        "checked_t": found["signal_t"],
    }


def _hours_after(cursor, end):
    hour = int(cursor) + paper.HOUR_MS
    end = int(end)
    while hour <= end:
        yield hour
        hour += paper.HOUR_MS


def _replay_shadow(sig, series, points):
    """Taker at the next open, walked through the bars already closed."""
    entry_t = sig["signal_t"] + paper.HOUR_MS
    bar = _bar(series, sig["coin"], entry_t)
    if bar is None or bar["o"] <= 0:
        sig["shadow"] = None
        return None
    leg = _open_on_bar(sig, bar, "taker", points)
    t = entry_t + paper.HOUR_MS
    while leg and not leg.get("exit_reason") and t <= sig["expires_t"]:
        nxt = _bar(series, sig["coin"], t)
        if nxt is not None:
            resolved = resolve_bar(leg, nxt)
            if resolved:
                _apply_exit(leg, nxt, *resolved, points)
        t += paper.HOUR_MS
    sig["shadow"] = leg
    return leg


def _manage_open(sig, bar, hour, events, points):
    """Stops, targets and the time exit on a bar after the fill."""
    leg = sig.get("position")
    if leg and not leg.get("exit_reason") and leg["entry_t"] < hour:
        resolved = resolve_bar(leg, bar)
        if resolved:
            _apply_exit(leg, bar, *resolved, points)
            events.append(_event("exit", sig, leg, exit_reason=leg["exit_reason"]))
    shadow = sig.get("shadow")
    if shadow and not shadow.get("exit_reason") and shadow["entry_t"] < hour:
        resolved = resolve_bar(shadow, bar)
        if resolved:
            _apply_exit(shadow, bar, *resolved, points)


def _expire(sig, series, events, points):
    sig["window_open"] = False
    if sig.get("position"):
        return
    shadow = _replay_shadow(sig, series, points)
    event = _event(
        "exit", sig, None, exit_reason="expired-unfilled",
        r=shadow.get("r") if shadow and shadow.get("exit_reason") else None,
    )
    if shadow:
        event["hypothetical_entry"] = paper.jnum(shadow.get("entry"))
        event["hypothetical_exit_reason"] = shadow.get("exit_reason")
        event["hypothetical_r"] = paper.jnum(shadow.get("r")) if shadow.get("exit_reason") else None
    events.append(event)


def _try_fill(sig, bar, events, points):
    if sig.get("position"):
        return
    if not traded_through_above(bar["h"], sig["limit"], sig["rules"]["trade_through"]):
        return
    leg = _open_on_bar(sig, bar, "maker", points)
    if leg is None:
        return
    sig["position"] = leg
    events.append(_event("fill", sig, leg))
    if leg.get("exit_reason"):
        events.append(_event("exit", sig, leg, exit_reason=leg["exit_reason"]))


def _catch_up(sig, series, t, events, funding):
    """Walk hours the signal has not seen yet, including hours a failed read skipped."""
    times = (series.get(sig["coin"]) or {}).get("by_t") or {}
    if not times:
        return
    points = _funding_points(funding, sig["coin"])
    cursor = sig.get("checked_t", sig["signal_t"])
    for hour in _hours_after(cursor, t):
        if hour not in times:
            if any(ts > hour for ts in times):
                sig["checked_t"] = hour
                if sig.get("window_open") and hour >= sig["expires_t"]:
                    _expire(sig, series, events, points)
                continue
            return
        bar = _bar(series, sig["coin"], hour)
        sig["checked_t"] = hour
        if bar is not None:
            _manage_open(sig, bar, hour, events, points)
            if sig.get("window_open") and sig["signal_t"] < hour <= sig["expires_t"]:
                _try_fill(sig, bar, events, points)
        if sig.get("window_open") and hour >= sig["expires_t"]:
            _expire(sig, series, events, points)


def _too_soon(last_ms, t, gap_bars):
    if last_ms is None:
        return False
    return 0 <= int(t) - int(last_ms) < int(gap_bars) * paper.HOUR_MS


def _open_signals(book, series, t, meta, active, rows, events, cfg):
    signals = book["signals"]
    last_map = book.setdefault("last_signal_t", {})
    occupied = {sig["coin"] for sig in signals if _occupies(sig)}
    slots = len(occupied)
    gap = int(cfg.get("signal_gap", SIGNAL_GAP))
    cap = int(cfg.get("max_positions", MAX_POSITIONS))
    for coin in sorted(active):
        if coin in occupied:
            continue
        if _too_soon(last_map.get(coin), t, gap):
            continue
        prep = series.get(coin) or {}
        candles = prep.get("candles") or []
        i = (prep.get("by_t") or {}).get(t)
        if i is None:
            continue
        atrs = prep.get("atr") or []
        atr = atrs[i] if i < len(atrs) else None
        found = detect(candles, i, atr, cfg)
        if not found:
            continue
        volume = _day_volume(meta, coin)
        last_map[coin] = t
        if slots >= cap:
            rows.append(_skipped_row(coin, found, volume, cfg))
            continue
        sig = _new_signal(coin, found, volume, cfg)
        signals.append(sig)
        occupied.add(coin)
        slots += 1
        events.append(_event("order", sig))


def _finish(book, rows):
    keep = []
    for sig in book["signals"]:
        if not _done(sig):
            keep.append(sig)
            continue
        status = "closed" if sig.get("position") else "unfilled"
        rows.append(_row_from(sig, status))
    book["signals"] = keep


def step(book, series, *, process_from, latest_closed, meta, active, cfg=None, funding=None):
    """Advance the book from `process_from` through `latest_closed`.

    `cfg` selects the variant (default A). `funding` is a coin to points
    map, a callable, or None for a known empty history. The input book is
    not mutated.
    """
    import copy
    cfg = cfg or variant()
    book = copy.deepcopy(book)
    if not isinstance(book.get("signals"), list):
        book["signals"] = []
    if not isinstance(book.get("last_signal_t"), dict):
        book["last_signal_t"] = {}
    book["variant"] = cfg["name"]
    rows = []
    events = []
    if (
        process_from is None or latest_closed is None
        or int(process_from) > int(latest_closed)
    ):
        _mirror(book)
        return book, rows, events
    active = set(active or [])
    meta = meta or {}
    t = int(process_from)
    last = int(latest_closed)
    while t <= last:
        for sig in list(book["signals"]):
            _catch_up(sig, series, t, events, funding)
        _finish(book, rows)
        _open_signals(book, series, t, meta, active, rows, events, cfg)
        t += paper.HOUR_MS
    _mirror(book)
    return book, rows, events


def _mirror(book):
    """Fill the shared position lists from the signal state."""
    positions = []
    pending = []
    shadows = []
    for sig in book.get("signals") or []:
        if sig.get("window_open"):
            pending.append({
                "coin": sig["coin"],
                "side": "short",
                "limit": sig["limit"],
                "signal_t": sig["signal_t"],
                "expires_t": sig["expires_t"],
                "atr": sig["atr"],
                "sl": sig["sl"],
                "tp": sig["tp"],
                "ta_extreme": bool(sig.get("ta_extreme")),
                "day_ntl_vlm": sig.get("day_ntl_vlm"),
                "best_ask": sig.get("best_ask"),
                "ask_depth_ntl": sig.get("ask_depth_ntl"),
                "limit_vs_spread": sig.get("limit_vs_spread"),
                "variant": sig.get("variant"),
            })
        leg = sig.get("position")
        if leg and not leg.get("exit_reason"):
            positions.append({
                "coin": sig["coin"],
                "side": "short",
                "entry": leg["entry"],
                "stop": leg["sl"],
                "tp": leg["tp"],
                "atr": sig["atr"],
                "signal_t": sig["signal_t"],
                "entry_t": leg["entry_t"],
                "size": leg["size"],
                "risk_usd": leg["risk_usd"],
                "fill_model": leg["model"],
                "ta_extreme": bool(sig.get("ta_extreme")),
                "variant": sig.get("variant"),
            })
        shadow = sig.get("shadow")
        if shadow and not shadow.get("exit_reason") and not leg:
            shadows.append({
                "coin": sig["coin"],
                "side": "short",
                "entry": shadow["entry"],
                "signal_t": sig["signal_t"],
                "entry_t": shadow["entry_t"],
                "hypothetical": True,
            })
    book["positions"] = positions
    book["pending_entries"] = pending
    book["pending_exits"] = []
    book["shadows"] = shadows


def record_l2(book, fetch_book, now_ms):
    """Append one hourly book snap on each resting limit. Errors stay per coin."""
    if fetch_book is None:
        return False
    changed = False
    hour = paper.iso(int(now_ms) - (int(now_ms) % paper.HOUR_MS))
    for sig in book.get("signals") or []:
        if not sig.get("window_open"):
            continue
        snaps = sig.setdefault("l2_snaps", [])
        if snaps and snaps[-1].get("ts") == hour:
            continue
        parsed = None
        try:
            parsed = fetch_book(sig["coin"])
        except Exception as exc:
            paper.log(f"c1_maker l2 {sig['coin']}: {exc}")
        snap = book_snapshot(sig.get("limit"), parsed, now_ms)
        snaps.append(snap)
        sig["best_bid"] = snap["best_bid"]
        sig["best_ask"] = snap["best_ask"]
        sig["ask_depth_ntl"] = snap["ask_depth_ntl"]
        sig["ask_depth_complete"] = snap["ask_depth_complete"]
        sig["limit_vs_spread"] = snap["limit_vs_spread"]
        changed = True
    return changed


def load():
    book = paper.load_book(BOOK)
    if not isinstance(book.get("signals"), list):
        book["signals"] = []
    if not isinstance(book.get("last_signal_t"), dict):
        book["last_signal_t"] = {}
    book["variant"] = LIVE_VARIANT
    book["fee_maker"] = FEE_MAKER
    book["fee_taker"] = FEE_TAKER
    book["slippage"] = SLIPPAGE
    return book


def save(book):
    book = dict(book)
    book["variant"] = LIVE_VARIANT
    book["fee_maker"] = FEE_MAKER
    book["fee_taker"] = FEE_TAKER
    book["slippage"] = SLIPPAGE
    if not isinstance(book.get("signals"), list):
        book["signals"] = []
    if not isinstance(book.get("last_signal_t"), dict):
        book["last_signal_t"] = {}
    _mirror(book)
    paper.save_book(BOOK, book)


def _latest(series, expected):
    if any(expected in (prep.get("by_t") or {}) for prep in series.values()):
        return expected
    found = []
    for prep in series.values():
        prior = [t for t in (prep.get("by_t") or {}) if t <= expected]
        if prior:
            found.append(max(prior))
    return max(found) if found else None


def _fetch_closed(coins, now_ms):
    start = int(now_ms) - FETCH_BARS * paper.HOUR_MS
    closed = {}
    for coin in coins:
        try:
            raw = paper.fetch_candles(coin, "1h", start, now_ms)
            norm = paper.normalize_candles(raw, paper.HOUR_MS)
        except Exception as exc:
            paper.log(f"c1_maker velas {coin}: {exc}")
            continue
        done, _forming = paper.split_candles(norm, now_ms, paper.HOUR_MS)
        if done:
            closed[coin] = done
    return closed


def _live_funding(book, now_ms):
    """Lazy funding reads. Only a coin that exits pays for the request."""
    cache = {}

    def funding(coin):
        if coin in cache:
            return cache[coin]
        starts = [int(now_ms) - 48 * paper.HOUR_MS]
        for sig in book.get("signals") or []:
            if sig.get("coin") != coin:
                continue
            if sig.get("signal_t"):
                starts.append(int(sig["signal_t"]))
            leg = sig.get("position") or sig.get("shadow")
            if leg and leg.get("entry_t"):
                starts.append(int(leg["entry_t"]))
        try:
            cache[coin] = paper.fetch_funding(coin, min(starts), now_ms)
        except Exception as exc:
            paper.log(f"c1_maker funding {coin}: {exc}")
            cache[coin] = None
        return cache[coin]

    return funding


def run(assets, now_ms, fetch_book=None, closed=None, persist=True, cfg=None):
    """One hourly pass of variant A. Returns events, whether the book changed, and a log line.

    Raises only on a programming failure of the pass itself. A missing coin
    or a single l2Book or funding error is logged and skipped. `persist`
    False keeps the pass in memory, for tests. The live book is always
    variant A; `cfg` is only for a caller that already knows that.
    """
    cfg = cfg or variant(LIVE_VARIANT)
    active, meta = main_universe(assets)
    book = load() if persist else {
        "signals": [],
        "positions": [],
        "pending_entries": [],
        "pending_exits": [],
        "last_signal_t": {},
        "last_processed_open_ms": None,
    }
    needed = set(active)
    for sig in book.get("signals") or []:
        needed.add(sig["coin"])
    if closed is None:
        closed = _fetch_closed(sorted(needed), now_ms)
    series = {coin: prepare(candles) for coin, candles in closed.items() if candles}
    expected = paper.latest_closed_open(now_ms, paper.HOUR_MS)
    latest = _latest(series, expected) if series else None
    resting = any(sig.get("window_open") for sig in book.get("signals") or [])
    working = any(
        _occupies(sig) or (sig.get("shadow") and not sig["shadow"].get("exit_reason"))
        for sig in book.get("signals") or []
    )
    if latest is None and not working:
        book["last_run"] = paper.iso(now_ms)
        if persist:
            save(book)
        return [], True, "c1_maker: sin velas cerradas"
    last = book.get("last_processed_open_ms")
    if last is None:
        process_from = latest
        new_bars = 1 if latest is not None else 0
    else:
        process_from = int(last) + paper.HOUR_MS
        if latest is None or process_from > latest:
            new_bars = 0
        else:
            new_bars = int((latest - process_from) / paper.HOUR_MS) + 1
    if new_bars <= 0 and not resting and not working:
        return [], False, f"c1_maker: al dia ({paper.iso(latest)})"
    if last is not None and series and process_from is not None and new_bars > 0:
        earliest = min(min(prep["by_t"]) for prep in series.values() if prep.get("by_t"))
        if process_from < earliest:
            paper.log(
                f"c1_maker: hueco de velas entre {paper.iso(process_from)} y "
                f"{paper.iso(earliest)}; solo se revisan las velas que hay"
            )
            process_from = earliest
            if latest is not None and process_from <= latest:
                new_bars = int((latest - process_from) / paper.HOUR_MS) + 1
    snapped = record_l2(book, fetch_book, now_ms)
    funding = _live_funding(book, now_ms) if persist or fetch_book is not None else None
    # A test pass with persist False and no book feed still settles on fees.
    if not persist and fetch_book is None:
        funding = None
    stepped, rows, events = step(
        book, series,
        process_from=process_from if new_bars > 0 else None,
        latest_closed=latest if new_bars > 0 else None,
        meta=meta, active=active, cfg=cfg, funding=funding,
    )
    snapped = record_l2(stepped, fetch_book, now_ms) or snapped
    if new_bars > 0 and latest is not None:
        stepped["last_processed_open_ms"] = latest
    else:
        stepped["last_processed_open_ms"] = book.get("last_processed_open_ms")
    stepped["last_run"] = paper.iso(now_ms)
    if persist:
        save(stepped)
        if rows:
            paper.write_trades(BOOK, rows, TRADE_FIELDS)
    closed_n = sum(1 for row in rows if row["status"] == "closed")
    unfilled_n = sum(1 for row in rows if row["status"] == "unfilled")
    skipped_n = sum(1 for row in rows if row["status"] == "skipped_cap")
    opens = len({sig["coin"] for sig in stepped.get("signals") or [] if _occupies(sig)})
    line = (
        f"c1_maker vela={paper.iso(latest)} abiertas={opens} "
        f"cierres={closed_n} unfilled={unfilled_n} skipped_cap={skipped_n} "
        f"eventos={len(events)}"
    )
    paper.log(line)
    changed = bool(new_bars or snapped or rows or events)
    return events, changed, line


def _values(rows, key, pred=None):
    out = []
    for row in rows:
        if pred and not pred(row):
            continue
        raw = row.get(key)
        if raw in (None, ""):
            continue
        try:
            out.append(float(raw))
        except (TypeError, ValueError):
            continue
    return out


def _filled(row, key="filled"):
    return str(row.get(key) or "").strip() in {"1", "true", "True"}


def profit_factor(values):
    """Sum of winning R over the absolute sum of losing R. None if no losses."""
    wins = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v < 0)
    if not values or losses <= 0:
        return None
    return wins / losses


def summarize_rows(rows, open_signals=None, days=None):
    """Signals, fill rate, average R of fills, profit factor, and unfilled taker R.

    A limit still inside its three bars is `still_pending` and stays out of
    the fill rate. Average R and profit factor use fills that have exited.
    """
    rows = list(rows or [])
    placed = [row for row in rows if row.get("status") in {"closed", "unfilled"}]
    skipped = [row for row in rows if row.get("status") == "skipped_cap"]
    filled_rs = _values(placed, "r", lambda row: _filled(row))
    hypo_rs = _values(
        [row for row in placed if row.get("status") == "unfilled"],
        "hypothetical_r",
    )
    fills = sum(1 for row in placed if _filled(row))
    unfilled = sum(1 for row in placed if row.get("status") == "unfilled")
    open_trades = 0
    still_pending = 0
    for sig in open_signals or []:
        leg = sig.get("position")
        if sig.get("window_open") and not leg:
            still_pending += 1
        elif not leg:
            unfilled += 1
            shadow = sig.get("shadow")
            if shadow and shadow.get("exit_reason") and shadow.get("r") is not None:
                hypo_rs.append(float(shadow["r"]))
        if leg:
            fills += 1
            if not leg.get("exit_reason"):
                open_trades += 1
            elif leg.get("r") is not None:
                filled_rs.append(float(leg["r"]))
    denom = fills + unfilled
    signals = len(skipped) + fills + unfilled + still_pending

    def avg(values):
        if not values:
            return None
        return sum(values) / len(values)

    per_day = None
    if days:
        per_day = signals / float(days)
    return {
        "signals": signals,
        "signals_per_day": per_day,
        "skipped_cap": len(skipped),
        "trades": len(filled_rs),
        "fills": fills,
        "unfilled": unfilled,
        "open_trades": open_trades,
        "still_pending": still_pending,
        "fill_rate": (fills / denom) if denom else None,
        "avg_r": avg(filled_rs),
        "pf": profit_factor(filled_rs),
        "avg_taker_r_unfilled": avg(hypo_rs),
        "unfilled_with_r": len(hypo_rs),
    }
