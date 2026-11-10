#!/usr/bin/env python3
"""Phase-2 watch: a retest zone after a phase-1 alert, then entry_ready.

The zone is the 38.2–50% retrace of the impulse (Fibonacci, the usual
retest band). Invalidation is a trade through the impulse origin.
TP1 is one measured move beyond the impulse extreme (Edwards & Magee /
Bulkowski: add the swing to the breakout). TP2 is two measured moves.
"""


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def impulse_from_candles(candles, side):
    if not candles:
        return None
    lows, highs = [], []
    for c in candles:
        l, h = _f(c.get("l")), _f(c.get("h"))
        if l is None or h is None:
            return None
        lows.append(l)
        highs.append(h)
    return {"low": min(lows), "high": max(highs), "side": side}


def build_watch(coin, dex, side, trigger, candles, now, cfg):
    """Entry zone is the deep half of the retrace, so R:R to the measured move can clear 2."""
    window = list(candles[-int(cfg.get("watch_impulse_bars", 3)):])
    box = impulse_from_candles(window, side)
    if not box:
        return None
    span = box["high"] - box["low"]
    if span <= 0:
        return None
    shallow = float(cfg["watch_fib_shallow"])
    deep = float(cfg["watch_fib_deep"])
    if side == "long":
        # Pullback from the high: 38.2% is closer to the high, 50% is deeper.
        entry_high = box["high"] - shallow * span
        entry_low = box["high"] - deep * span
        sl = box["low"]
        tp1 = box["high"] + span
        tp2 = box["high"] + 2 * span
    elif side == "short":
        entry_low = box["low"] + shallow * span
        entry_high = box["low"] + deep * span
        sl = box["high"]
        tp1 = box["low"] - span
        tp2 = box["low"] - 2 * span
    else:
        return None
    if entry_low > entry_high:
        entry_low, entry_high = entry_high, entry_low
    hours = float(cfg["watch_expiry_hours"])
    return {
        "coin": coin,
        "dex": dex,
        "side": side,
        "trigger": trigger,
        "created_at": now,
        "expires_at": now + hours * 3600,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "impulse_low": box["low"],
        "impulse_high": box["high"],
        "status": "open",
    }


def reward_risk(side, price, sl, tp1):
    if price is None or sl is None or tp1 is None:
        return None
    if side == "long":
        risk, reward = price - sl, tp1 - price
    elif side == "short":
        risk, reward = sl - price, price - tp1
    else:
        return None
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


def build_absorption_watch(coin, dex, side, trigger, range_low, range_high, now, cfg):
    """A lower-urgency watch: wait for the range to break, do not fade it.

    The stop is the far side of the absorbed range (the effort failed).
    TP1 is two measured-move heights past the breakout (Edwards & Magee /
    Bulkowski add the range once; the second height is what makes a fill at
    the edge pay the phase-2 minimum of 2R). TP2 is three heights.
    """
    lo, hi = _f(range_low), _f(range_high)
    if side not in ("long", "short") or lo is None or hi is None or hi <= lo:
        return None
    span = hi - lo
    hours = float(cfg["watch_expiry_hours"])
    if side == "long":
        level, sl = hi, lo
        tp1, tp2 = hi + 2 * span, hi + 3 * span
    else:
        level, sl = lo, hi
        tp1, tp2 = lo - 2 * span, lo - 3 * span
    return {
        "coin": coin,
        "dex": dex,
        "side": side,
        "trigger": trigger,
        "kind": "absorption",
        "urgency": "baja",
        "created_at": now,
        "expires_at": now + hours * 3600,
        "entry_low": level,
        "entry_high": level,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "impulse_low": lo,
        "impulse_high": hi,
        "status": "open",
    }


def _advance_absorption(watch, high, low, close, now, cfg, allow_entry):
    """Breakout of the absorbed range. A fill at the edge is 2R by construction.

    If the bar gaps past the edge, the fill is the near side of that bar and
    the R:R test can reject it: the watch stays open instead of chasing.
    """
    if watch.get("status") != "open":
        return watch, None
    if now >= watch["expires_at"]:
        watch["status"] = "expired"
        return watch, "expired"
    side = watch["side"]
    sl = watch["sl"]
    level = watch["entry_high"]
    # Before anyone is in the trade, a wick through the far side is a Wyckoff
    # spring, not an invalidation. The setup ends when the close holds beyond
    # that side. Once filled, outcome_after_entry still stops out on a wick.
    if side == "long" and close is not None and close <= sl:
        watch["status"] = "invalidated"
        return watch, "invalidated"
    if side == "short" and close is not None and close >= sl:
        watch["status"] = "invalidated"
        return watch, "invalidated"
    if side == "long":
        held = close is not None and high is not None and close > level and high > level
    else:
        held = close is not None and low is not None and close < level and low < level
    if not held:
        return watch, None
    if side == "long":
        fill = level if low is not None and low <= level else low
    else:
        fill = level if high is not None and high >= level else high
    rr = reward_risk(side, fill, sl, watch["tp1"])
    need = float(cfg["watch_rr_min"])
    if rr is None or rr < need:
        return watch, None
    if not allow_entry:
        return watch, "quiet"
    watch["status"] = "entry_sent"
    watch["fill"] = fill
    watch["rr"] = rr
    watch["entry_sent_at"] = now
    return watch, "entry_ready"


def advance_watch(watch, high, low, close, now, cfg, allow_entry=True):
    """Move one watch forward. Returns (watch, event or None).

    event is entry_ready, invalidated, expired, or quiet. A second touch after
    entry_sent does not emit another entry_ready (dedupe per pair+side).
    An absorption watch breaks out of its range instead of waiting for a retest.
    """
    if watch.get("kind") == "absorption":
        return _advance_absorption(watch, high, low, close, now, cfg, allow_entry)
    if watch.get("status") != "open":
        return watch, None
    if now >= watch["expires_at"]:
        watch["status"] = "expired"
        return watch, "expired"
    side = watch["side"]
    sl = watch["sl"]
    # Invalidation uses the wick. The same bar can both tag the zone and the stop;
    # the stop wins, otherwise a blow-through would look like a filled retest.
    if side == "long" and low is not None and low <= sl:
        watch["status"] = "invalidated"
        return watch, "invalidated"
    if side == "short" and high is not None and high >= sl:
        watch["status"] = "invalidated"
        return watch, "invalidated"
    touched = False
    if high is not None and low is not None:
        touched = low <= watch["entry_high"] and high >= watch["entry_low"]
    if not touched and close is not None:
        touched = watch["entry_low"] <= close <= watch["entry_high"]
    if not touched:
        return watch, None
    # Fill at the close if it sits in the zone, else at the nearest edge.
    if close is not None and watch["entry_low"] <= close <= watch["entry_high"]:
        fill = close
    elif side == "long":
        fill = watch["entry_high"]
    else:
        fill = watch["entry_low"]
    rr = reward_risk(side, fill, sl, watch["tp1"])
    need = float(cfg["watch_rr_min"])
    if rr is None or rr < need:
        return watch, None
    if not allow_entry:
        return watch, "quiet"
    watch["status"] = "entry_sent"
    watch["fill"] = fill
    watch["rr"] = rr
    watch["entry_sent_at"] = now
    return watch, "entry_ready"


def outcome_after_entry(watch, later_candles):
    """Which structural level is touched first after the fill. Same-bar stop wins."""
    side = watch["side"]
    for c in later_candles:
        h, l = _f(c.get("h")), _f(c.get("l"))
        if h is None or l is None:
            continue
        if side == "long":
            if l <= watch["sl"]:
                return "sl"
            if h >= watch["tp2"]:
                return "tp2"
            if h >= watch["tp1"]:
                return "tp1"
        else:
            if h >= watch["sl"]:
                return "sl"
            if l <= watch["tp2"]:
                return "tp2"
            if l <= watch["tp1"]:
                return "tp1"
    return "open"
