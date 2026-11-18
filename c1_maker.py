#!/usr/bin/env python3
"""C1 pump-fade short with a maker limit. Paper and alerts only.

Evaluated on closed 1h candles of every main-dex Hyperliquid perp. No
liquidity filter and no exchange keys. This module never places an order.

Signal on a closed 1h bar, over the 48 hourly bars that end on that bar
(contiguous; a gap invalidates the window):

- max high >= 1.40 x min low (a pump of at least 40%)
- the close is 3% to 6% under that high, inclusive
- the bar return close/open - 1 is -3% or worse

A pump of 100% or more (high >= 2 x low) sets `ta_extreme` on the alert.
That flag does not change the order.

The order is a sell limit at close + 0.25 * ATR(14), live for the next 3
bars. A naive fill is a later bar whose high reaches the limit. A
conservative fill needs the high to clear the limit by at least 0.05%
(the stand-in for one tick). Both fill at the limit and pay the maker fee.
The same signal can record both outcomes. They share one coin slot.

After a fill, SL = entry + 1.75 * ATR (taker, 0.045%) and TP = entry - 1R
(maker, 0.015%), where 1R is that same ATR distance. Max hold is 48 bars,
then the exit is the close (taker). On the maker fill bar only the stop is
eligible: the bar's low can print before price trades up to the limit. If a
later bar trades both the stop and the target, the stop wins. One position
per coin, at most 10 coins, each risking 0.25% of 1,000 USDC (2.50 USDC).

An order that never fills is logged too. The counterfactual is a taker
entry at the next bar's open, with the same stop, target and hold, so the
fill can be compared with the trade the limit refused.

Each hourly run records the public l2Book for orders that are still
resting: best ask, ask-side notional at or inside the limit, and whether
the limit sits inside the spread, beyond the best ask, or through the bid.
"""
import json

import paper

BOOK = "c1_maker"
WINDOW = 48
PUMP_MIN = 1.40
PUMP_EXTREME = 2.0
CLOSE_OFF_MIN = 0.03
CLOSE_OFF_MAX = 0.06
BAR_DROP = -0.03
LIMIT_ATR = 0.25
ATR_PERIOD = 14
VALID_BARS = 3
SL_ATR = 1.75
MAX_HOLD = 48
MAX_POSITIONS = 10
EQUITY_USDC = 1000.0
RISK_PCT = 0.0025
RISK_USD = EQUITY_USDC * RISK_PCT
FEE_MAKER = 0.00015
FEE_TAKER = 0.00045
# High must clear the limit by this fraction. 0.05% stands in for one tick.
CONSERVATIVE_FRAC = 0.0005
BOOK_LEVELS = 20
FETCH_BARS = 400

FEE_RATE = {"maker": FEE_MAKER, "taker": FEE_TAKER}

TRADE_FIELDS = (
    "status", "coin", "side", "signal_time", "entry_time", "exit_time",
    "limit_px", "entry", "exit", "sl", "tp", "atr",
    "pump_high", "pump_low", "pump_ratio", "close_off_high", "bar_return",
    "ta_extreme", "day_ntl_vlm",
    "size_coins", "notional", "risk_usd",
    "gross_pnl", "fees", "net_pnl", "r",
    "exit_reason",
    "naive_filled", "conservative_filled",
    "naive_entry_time", "naive_exit_time", "naive_exit_reason", "naive_r",
    "conservative_entry_time", "conservative_exit_time", "conservative_exit_reason", "conservative_r",
    "hypothetical_entry", "hypothetical_exit", "hypothetical_exit_reason", "hypothetical_r",
    "best_ask", "ask_depth_ntl", "ask_depth_complete", "limit_vs_spread", "l2_snaps",
)


def prepare(candles):
    return paper.index_series(candles, paper.wilder_atr(candles, ATR_PERIOD))


def window_extremes(candles, i, window=WINDOW):
    """Max high and min low of the `window` hourly bars ending at i, or None."""
    window = int(window)
    if not candles or i < window - 1 or i >= len(candles):
        return None
    chunk = candles[i - window + 1:i + 1]
    if len(chunk) < window:
        return None
    for prev, nxt in zip(chunk, chunk[1:]):
        if nxt["t"] - prev["t"] != paper.HOUR_MS:
            return None
    hi = max(bar["h"] for bar in chunk)
    lo = min(bar["l"] for bar in chunk)
    if hi <= 0 or lo <= 0:
        return None
    return hi, lo


def detect(candles, i, atr, window=WINDOW):
    """Signal dict for candle i, or None. `atr` is ATR(14) on that candle."""
    if atr is None or atr <= 0:
        return None
    extremes = window_extremes(candles, i, window)
    if extremes is None:
        return None
    hi, lo = extremes
    if hi + 1e-12 < PUMP_MIN * lo:
        return None
    bar = candles[i]
    if bar["o"] <= 0 or bar["c"] <= 0 or hi <= 0:
        return None
    off = (hi - bar["c"]) / hi
    if off + 1e-12 < CLOSE_OFF_MIN or off - 1e-12 > CLOSE_OFF_MAX:
        return None
    ret = bar["c"] / bar["o"] - 1.0
    if ret - 1e-12 > BAR_DROP:
        return None
    ratio = hi / lo
    limit = bar["c"] + LIMIT_ATR * float(atr)
    if limit <= 0:
        return None
    return {
        "side": "short",
        "pump_high": hi,
        "pump_low": lo,
        "pump_ratio": ratio,
        "close_off_high": off,
        "bar_return": ret,
        "atr": float(atr),
        "limit": limit,
        "ta_extreme": ratio + 1e-12 >= PUMP_EXTREME,
        "signal_open": bar["o"],
        "signal_close": bar["c"],
        "signal_t": bar["t"],
    }


def naive_hit(high, limit):
    """True when the bar high trades the sell limit."""
    if high is None or limit is None or limit <= 0:
        return False
    return high + 1e-12 >= limit


def conservative_hit(high, limit, extra=CONSERVATIVE_FRAC):
    """True when the high clears the limit by at least `extra` (0.05%)."""
    if high is None or limit is None or limit <= 0:
        return False
    return (high - limit) / limit + 1e-12 >= float(extra)


def planned_levels(entry, atr):
    """Stop above entry and a target one R under it. R = 1.75 * ATR."""
    dist = SL_ATR * float(atr)
    return entry + dist, entry - dist


def size_coins(entry, sl, risk_usd=RISK_USD):
    dist = sl - entry
    if dist <= 0 or risk_usd <= 0:
        return None
    return float(risk_usd) / dist


def settle(entry, exit_px, size, entry_kind, exit_kind, risk_usd=RISK_USD):
    """Short PnL. Entry and exit kinds are 'maker' or 'taker'."""
    gross = (entry - exit_px) * size
    fees = (
        FEE_RATE[entry_kind] * size * entry
        + FEE_RATE[exit_kind] * size * exit_px
    )
    net = gross - fees
    risk = float(risk_usd) if risk_usd else 0.0
    r = net / risk if risk else None
    return {
        "gross_pnl": gross,
        "fees": fees,
        "net_pnl": net,
        "r": r,
    }


def resolve_bar(leg, bar, max_hold=MAX_HOLD):
    """Exit of an open short on `bar`, or None.

    Returns (price, reason, fee kind). Maker legs do not take profit on the
    fill bar. A bar that reaches both the stop and the target stops out.
    """
    if not leg or not bar or leg.get("exit_reason"):
        return None
    if bar["t"] < leg["entry_t"]:
        return None
    sl = leg["sl"]
    tp = leg["tp"]
    entry_bar = bar["t"] == leg["entry_t"]
    maker = leg.get("entry_fee") == "maker"
    o, h, l, c = bar["o"], bar["h"], bar["l"], bar["c"]

    def sl_price():
        return o if o >= sl else sl

    if maker and entry_bar:
        if o >= sl or h >= sl:
            return sl_price(), "sl", "taker"
    else:
        if o >= sl:
            return o, "sl", "taker"
        if o <= tp:
            return o, "tp", "taker"
        if h >= sl:
            return sl, "sl", "taker"
        if l <= tp:
            return tp, "tp", "maker"
    held = int((bar["t"] - leg["entry_t"]) // paper.HOUR_MS) + 1
    if held >= int(max_hold):
        return c, "time", "taker"
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


def _make_leg(entry, atr, entry_t, model, entry_fee, risk_usd=RISK_USD):
    sl, tp = planned_levels(entry, atr)
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
        "exit": None,
        "exit_t": None,
        "exit_reason": None,
        "exit_fee": None,
        "gross_pnl": None,
        "fees": None,
        "net_pnl": None,
        "r": None,
    }


def _apply_exit(leg, bar, price, reason, fee_kind):
    acct = settle(leg["entry"], price, leg["size"], leg["entry_fee"], fee_kind, leg["risk_usd"])
    leg["exit"] = price
    leg["exit_t"] = bar["t"] + paper.HOUR_MS
    leg["exit_reason"] = reason
    leg["exit_fee"] = fee_kind
    leg.update(acct)
    return leg


def _open_on_bar(sig, bar, model):
    """Open a maker leg at the limit, or a taker leg at the bar open."""
    if model == "hypothetical":
        entry = bar["o"]
        fee = "taker"
    else:
        entry = sig["limit"]
        fee = "maker"
    leg = _make_leg(entry, sig["atr"], bar["t"], model, fee)
    if leg is None:
        return None
    resolved = resolve_bar(leg, bar)
    if resolved:
        _apply_exit(leg, bar, *resolved)
    return leg


def _occupies(sig):
    if sig.get("window_open"):
        return True
    for key in ("naive", "conservative"):
        leg = sig.get(key)
        if leg and not leg.get("exit_reason"):
            return True
    return False


def _done(sig):
    if sig.get("window_open"):
        return False
    for key in ("naive", "conservative", "shadow"):
        leg = sig.get(key)
        if leg and not leg.get("exit_reason"):
            return False
    return True


def _csv_flag(value):
    return "1" if value else "0"


def _leg_cols(prefix, leg):
    if not leg:
        return {
            f"{prefix}_entry_time": "",
            f"{prefix}_exit_time": "",
            f"{prefix}_exit_reason": "",
            f"{prefix}_r": "",
        }
    return {
        f"{prefix}_entry_time": paper.iso(leg.get("entry_t")),
        f"{prefix}_exit_time": paper.iso(leg.get("exit_t")),
        f"{prefix}_exit_reason": leg.get("exit_reason") or "",
        f"{prefix}_r": paper.csv_num(leg.get("r")),
    }


def _row_from(sig, status):
    naive = sig.get("naive")
    cons = sig.get("conservative")
    shadow = sig.get("shadow")
    primary = naive if status == "closed" else None
    row = {key: "" for key in TRADE_FIELDS}
    row.update({
        "status": status,
        "coin": sig["coin"],
        "side": "short",
        "signal_time": paper.iso(sig.get("signal_t")),
        "entry_time": paper.iso(primary.get("entry_t")) if primary else "",
        "exit_time": paper.iso(primary.get("exit_t")) if primary else "",
        "limit_px": paper.csv_num(sig.get("limit")),
        "entry": paper.csv_num(primary.get("entry")) if primary else "",
        "exit": paper.csv_num(primary.get("exit")) if primary else "",
        "sl": paper.csv_num(primary.get("sl")) if primary else paper.csv_num(planned_levels(sig["limit"], sig["atr"])[0]),
        "tp": paper.csv_num(primary.get("tp")) if primary else paper.csv_num(planned_levels(sig["limit"], sig["atr"])[1]),
        "atr": paper.csv_num(sig.get("atr")),
        "pump_high": paper.csv_num(sig.get("pump_high")),
        "pump_low": paper.csv_num(sig.get("pump_low")),
        "pump_ratio": paper.csv_num(sig.get("pump_ratio")),
        "close_off_high": paper.csv_num(sig.get("close_off_high")),
        "bar_return": paper.csv_num(sig.get("bar_return")),
        "ta_extreme": _csv_flag(sig.get("ta_extreme")),
        "day_ntl_vlm": paper.csv_num(sig.get("day_ntl_vlm")),
        "size_coins": paper.csv_num(primary.get("size")) if primary else "",
        "notional": paper.csv_num(primary["size"] * primary["entry"]) if primary else "",
        "risk_usd": paper.csv_num(primary.get("risk_usd") if primary else RISK_USD),
        "gross_pnl": paper.csv_num(primary.get("gross_pnl")) if primary else "",
        "fees": paper.csv_num(primary.get("fees")) if primary else "",
        "net_pnl": paper.csv_num(primary.get("net_pnl")) if primary else "",
        "r": paper.csv_num(primary.get("r")) if primary else "",
        "exit_reason": (primary or {}).get("exit_reason") or ("expired-unfilled" if status == "unfilled" else ""),
        "naive_filled": _csv_flag(bool(naive)),
        "conservative_filled": _csv_flag(bool(cons)),
        "hypothetical_entry": paper.csv_num(shadow.get("entry")) if shadow else "",
        "hypothetical_exit": paper.csv_num(shadow.get("exit")) if shadow else "",
        "hypothetical_exit_reason": (shadow or {}).get("exit_reason") or "",
        "hypothetical_r": paper.csv_num(shadow.get("r")) if shadow else "",
        "best_ask": paper.csv_num(sig.get("best_ask")),
        "ask_depth_ntl": paper.csv_num(sig.get("ask_depth_ntl")),
        "ask_depth_complete": "" if sig.get("ask_depth_complete") is None else str(int(sig.get("ask_depth_complete"))),
        "limit_vs_spread": sig.get("limit_vs_spread") or "",
        "l2_snaps": json.dumps(sig.get("l2_snaps") or [], separators=(",", ":"), sort_keys=True),
    })
    if status == "closed":
        row.update(_leg_cols("naive", naive))
        # _leg_cols uses naive_entry_time; primary columns already hold naive.
        row["naive_entry_time"] = paper.iso(naive.get("entry_t")) if naive else ""
        row["naive_exit_time"] = paper.iso(naive.get("exit_t")) if naive else ""
        row["naive_exit_reason"] = (naive or {}).get("exit_reason") or ""
        row["naive_r"] = paper.csv_num(naive.get("r")) if naive else ""
    else:
        row.update(_leg_cols("naive", naive))
    row.update(_leg_cols("conservative", cons))
    return row


def _skipped_row(coin, sig, volume):
    sl, tp = planned_levels(sig["limit"], sig["atr"])
    row = {key: "" for key in TRADE_FIELDS}
    row.update({
        "status": "skipped_cap",
        "coin": coin,
        "side": "short",
        "signal_time": paper.iso(sig.get("signal_t")),
        "limit_px": paper.csv_num(sig.get("limit")),
        "sl": paper.csv_num(sl),
        "tp": paper.csv_num(tp),
        "atr": paper.csv_num(sig.get("atr")),
        "pump_high": paper.csv_num(sig.get("pump_high")),
        "pump_low": paper.csv_num(sig.get("pump_low")),
        "pump_ratio": paper.csv_num(sig.get("pump_ratio")),
        "close_off_high": paper.csv_num(sig.get("close_off_high")),
        "bar_return": paper.csv_num(sig.get("bar_return")),
        "ta_extreme": _csv_flag(sig.get("ta_extreme")),
        "day_ntl_vlm": paper.csv_num(volume),
        "risk_usd": paper.csv_num(RISK_USD),
        "exit_reason": "skipped_cap",
        "naive_filled": "0",
        "conservative_filled": "0",
        "l2_snaps": "[]",
    })
    return row


def _event(kind, sig, leg=None, exit_reason=None, r=None):
    sl, tp = planned_levels(sig["limit"], sig["atr"])
    if leg:
        sl, tp = leg["sl"], leg["tp"]
    event_id = f"{BOOK}_{kind}:{sig['coin']}:{sig.get('signal_t')}"
    if leg:
        event_id += f":{leg.get('model')}:{leg.get('entry_t')}"
    if exit_reason:
        event_id += f":{exit_reason}:{leg.get('exit_t') if leg else sig.get('expires_t')}"
    body = {
        "type": f"{BOOK}_{kind}",
        "paper": True,
        "book": BOOK,
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
        "dayNtlVlm": paper.jnum(sig.get("day_ntl_vlm")),
        "pump_ratio": paper.jnum(sig.get("pump_ratio")),
        "signal_time": paper.iso(sig.get("signal_t")),
        "entry_time": paper.iso(leg.get("entry_t")) if leg else None,
        "exit_time": paper.iso(leg.get("exit_t")) if leg and leg.get("exit_t") else None,
        "fill_model": leg.get("model") if leg else None,
        "best_ask": sig.get("best_ask"),
        "ask_depth_ntl": sig.get("ask_depth_ntl"),
        "limit_vs_spread": sig.get("limit_vs_spread"),
        "risk_usd": paper.jnum(RISK_USD),
        "event_id": event_id,
    }
    return body


def _new_signal(coin, found, volume):
    sl, tp = planned_levels(found["limit"], found["atr"])
    return {
        "coin": coin,
        "side": "short",
        "signal_t": found["signal_t"],
        "expires_t": found["signal_t"] + VALID_BARS * paper.HOUR_MS,
        "window_open": True,
        "limit": found["limit"],
        "atr": found["atr"],
        "sl": sl,
        "tp": tp,
        "pump_high": found["pump_high"],
        "pump_low": found["pump_low"],
        "pump_ratio": found["pump_ratio"],
        "close_off_high": found["close_off_high"],
        "bar_return": found["bar_return"],
        "ta_extreme": bool(found["ta_extreme"]),
        "day_ntl_vlm": volume,
        "naive": None,
        "conservative": None,
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


def _replay_shadow(sig, series):
    """Taker at the next open, walked through the bars already closed."""
    entry_t = sig["signal_t"] + paper.HOUR_MS
    bar = _bar(series, sig["coin"], entry_t)
    if bar is None or bar["o"] <= 0:
        sig["shadow"] = None
        return None
    leg = _open_on_bar(sig, bar, "hypothetical")
    t = entry_t + paper.HOUR_MS
    while leg and not leg.get("exit_reason") and t <= sig["expires_t"]:
        nxt = _bar(series, sig["coin"], t)
        if nxt is not None:
            resolved = resolve_bar(leg, nxt)
            if resolved:
                _apply_exit(leg, nxt, *resolved)
        t += paper.HOUR_MS
    sig["shadow"] = leg
    return leg


def _manage_open_legs(sig, bar, hour, events):
    """Apply stops, targets and the time exit on a bar after the fill bar."""
    for key in ("naive", "conservative"):
        leg = sig.get(key)
        if not leg or leg.get("exit_reason") or leg["entry_t"] >= hour:
            continue
        resolved = resolve_bar(leg, bar)
        if not resolved:
            continue
        _apply_exit(leg, bar, *resolved)
        events.append(_event("exit", sig, leg, exit_reason=leg["exit_reason"]))
    shadow = sig.get("shadow")
    if shadow and not shadow.get("exit_reason") and shadow["entry_t"] < hour:
        resolved = resolve_bar(shadow, bar)
        if resolved:
            _apply_exit(shadow, bar, *resolved)


def _expire(sig, series, events):
    sig["window_open"] = False
    if sig.get("naive"):
        return
    shadow = _replay_shadow(sig, series)
    event = _event(
        "exit", sig, None, exit_reason="expired-unfilled",
        r=shadow.get("r") if shadow and shadow.get("exit_reason") else None,
    )
    if shadow:
        event["hypothetical_entry"] = paper.jnum(shadow.get("entry"))
        event["hypothetical_exit_reason"] = shadow.get("exit_reason")
        event["hypothetical_r"] = paper.jnum(shadow.get("r")) if shadow.get("exit_reason") else None
    events.append(event)


def _catch_up(sig, series, t, events):
    """Walk hours the signal has not seen yet, including hours a failed read skipped.

    A hole with a later candle is a real gap and is not retried. A hole at the
    live edge waits for the next run, and the limit is not expired until that
    hour has been seen or skipped.
    """
    times = (series.get(sig["coin"]) or {}).get("by_t") or {}
    if not times:
        return
    cursor = sig.get("checked_t", sig["signal_t"])
    for hour in _hours_after(cursor, t):
        if hour not in times:
            if any(ts > hour for ts in times):
                sig["checked_t"] = hour
                if sig.get("window_open") and hour >= sig["expires_t"]:
                    _expire(sig, series, events)
                continue
            return
        bar = _bar(series, sig["coin"], hour)
        sig["checked_t"] = hour
        if bar is not None:
            _manage_open_legs(sig, bar, hour, events)
            if sig.get("window_open") and sig["signal_t"] < hour <= sig["expires_t"]:
                _try_fill(sig, bar, "naive", naive_hit, events)
                _try_fill(sig, bar, "conservative", conservative_hit, events)
        if sig.get("window_open") and hour >= sig["expires_t"]:
            _expire(sig, series, events)


def _try_fill(sig, bar, model, hit, events):
    if sig.get(model) or not hit(bar["h"], sig["limit"]):
        return
    leg = _open_on_bar(sig, bar, model)
    if leg is None:
        return
    sig[model] = leg
    events.append(_event("fill", sig, leg))
    if leg.get("exit_reason"):
        events.append(_event("exit", sig, leg, exit_reason=leg["exit_reason"]))


def _open_signals(book, series, t, meta, active, rows, events):
    signals = book["signals"]
    occupied = {sig["coin"] for sig in signals if _occupies(sig)}
    slots = len(occupied)
    for coin in sorted(active):
        if coin in occupied:
            continue
        prep = series.get(coin) or {}
        candles = prep.get("candles") or []
        i = (prep.get("by_t") or {}).get(t)
        if i is None:
            continue
        atrs = prep.get("atr") or []
        atr = atrs[i] if i < len(atrs) else None
        found = detect(candles, i, atr)
        if not found:
            continue
        volume = _day_volume(meta, coin)
        if slots >= MAX_POSITIONS:
            rows.append(_skipped_row(coin, found, volume))
            continue
        sig = _new_signal(coin, found, volume)
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
        status = "closed" if sig.get("naive") else "unfilled"
        rows.append(_row_from(sig, status))
    book["signals"] = keep


def step(book, series, *, process_from, latest_closed, meta, active):
    """Advance the book from `process_from` through `latest_closed`.

    Returns a new book, trade rows and webhook events. The input is not mutated.
    """
    import copy
    book = copy.deepcopy(book)
    if not isinstance(book.get("signals"), list):
        book["signals"] = []
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
            _catch_up(sig, series, t, events)
        _finish(book, rows)
        _open_signals(book, series, t, meta, active, rows, events)
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
            })
        for key in ("naive", "conservative"):
            leg = sig.get(key)
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
                })
                break
        shadow = sig.get("shadow")
        if shadow and not shadow.get("exit_reason") and not sig.get("naive"):
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
    book["fee_maker"] = FEE_MAKER
    book["fee_taker"] = FEE_TAKER
    return book


def save(book):
    book = dict(book)
    book["fee_maker"] = FEE_MAKER
    book["fee_taker"] = FEE_TAKER
    if not isinstance(book.get("signals"), list):
        book["signals"] = []
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


def run(assets, now_ms, fetch_book=None, closed=None, persist=True):
    """One hourly pass. Returns events, whether the book changed, and a log line.

    Raises only on a programming failure of the pass itself. A missing coin
    or a single l2Book error is logged and skipped. `persist` False keeps the
    pass in memory, for tests.
    """
    active, meta = main_universe(assets)
    book = load() if persist else {
        "signals": [],
        "positions": [],
        "pending_entries": [],
        "pending_exits": [],
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
    working = any(_occupies(sig) or (sig.get("shadow") and not sig["shadow"].get("exit_reason"))
                  for sig in book.get("signals") or [])
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
    stepped, rows, events = step(
        book, series,
        process_from=process_from if new_bars > 0 else None,
        latest_closed=latest if new_bars > 0 else None,
        meta=meta, active=active,
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


def _filled(row, key):
    return str(row.get(key) or "").strip() in {"1", "true", "True"}


def profit_factor(values):
    """Sum of winning R over the absolute sum of losing R. None if no losses."""
    wins = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v < 0)
    if not values or losses <= 0:
        return None
    return wins / losses


def summarize_rows(rows, open_signals=None):
    """Trades, fill rates, average R and profit factor.

    A limit still inside its three bars is `still_pending` and stays out of
    the fill rate. Legs that have exited contribute to average R and profit
    factor, including a naive leg that is waiting on the conservative twin
    before the row is written. Open fills count in the fill rate.
    """
    rows = list(rows or [])
    placed = [row for row in rows if row.get("status") in {"closed", "unfilled"}]
    skipped = [row for row in rows if row.get("status") == "skipped_cap"]
    naive_rs = _values(placed, "naive_r", lambda row: _filled(row, "naive_filled"))
    cons_rs = _values(placed, "conservative_r", lambda row: _filled(row, "conservative_filled"))
    hypo_rs = _values(
        [row for row in placed if row.get("status") == "unfilled"],
        "hypothetical_r",
    )
    naive_fills = sum(1 for row in placed if _filled(row, "naive_filled"))
    cons_fills = sum(1 for row in placed if _filled(row, "conservative_filled"))
    unfilled = sum(1 for row in placed if row.get("status") == "unfilled")
    open_naive = 0
    still_pending = 0
    for sig in open_signals or []:
        naive = sig.get("naive")
        cons = sig.get("conservative")
        if sig.get("window_open") and not naive:
            still_pending += 1
        elif not naive:
            unfilled += 1
            if sig.get("shadow") and sig["shadow"].get("exit_reason") and sig["shadow"].get("r") is not None:
                hypo_rs.append(float(sig["shadow"]["r"]))
        if naive:
            naive_fills += 1
            if not naive.get("exit_reason"):
                open_naive += 1
            elif naive.get("r") is not None:
                naive_rs.append(float(naive["r"]))
        if cons:
            cons_fills += 1
            if cons.get("exit_reason") and cons.get("r") is not None:
                cons_rs.append(float(cons["r"]))
    denom = naive_fills + unfilled

    def avg(values):
        if not values:
            return None
        return sum(values) / len(values)

    def rate(fills):
        if not denom:
            return None
        return fills / denom

    return {
        "signals": len(skipped) + naive_fills + unfilled + still_pending,
        "placed": denom + still_pending,
        "skipped_cap": len(skipped),
        "trades": len(naive_rs),
        "naive_fills": naive_fills,
        "conservative_fills": cons_fills,
        "unfilled": unfilled,
        "open_trades": open_naive,
        "still_pending": still_pending,
        "fill_rate_naive": rate(naive_fills),
        "fill_rate_conservative": rate(cons_fills),
        "avg_r_naive": avg(naive_rs),
        "avg_r_conservative": avg(cons_rs),
        "avg_r_hypothetical_unfilled": avg(hypo_rs),
        "pf_naive": profit_factor(naive_rs),
        "pf_conservative": profit_factor(cons_rs),
        "hypothetical_unfilled": len(hypo_rs),
    }
