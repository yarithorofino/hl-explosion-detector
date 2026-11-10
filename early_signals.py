#!/usr/bin/env python3
"""Early phase-1 components. Each one copies a documented method.

Nothing here is a trade by itself. ``confluence`` only fires when the
weighted score reaches the config threshold, so one weak clue cannot alert.
"""
import math


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _closes(cs):
    out = []
    for c in cs:
        v = _f(c.get("c"))
        if v is None:
            return None
        out.append(v)
    return out


def true_ranges(cs):
    """Wilder's true range. Needs the previous close, so index 0 is just the bar range."""
    trs = []
    prev = None
    for c in cs:
        h, l, cl = _f(c.get("h")), _f(c.get("l")), _f(c.get("c"))
        if h is None or l is None or cl is None:
            return None
        if prev is None:
            trs.append(h - l)
        else:
            trs.append(max(h - l, abs(h - prev), abs(l - prev)))
        prev = cl
    return trs


def sma(values, end, period):
    if end < period - 1 or end >= len(values):
        return None
    window = values[end - period + 1:end + 1]
    if any(v is None for v in window):
        return None
    return sum(window) / period


def stdev(values, end, period):
    if end < period - 1 or end >= len(values):
        return None
    window = values[end - period + 1:end + 1]
    mid = sum(window) / period
    var = sum((v - mid) ** 2 for v in window) / period
    return math.sqrt(var)


def bollinger(closes, end, period, mult):
    """Bollinger bands. Population stdev, as Bollinger specifies."""
    mid = sma(closes, end, period)
    sd = stdev(closes, end, period)
    if mid is None or sd is None or mid == 0:
        return None
    upper, lower = mid + mult * sd, mid - mult * sd
    return {"mid": mid, "upper": upper, "lower": lower, "bandwidth": (upper - lower) / mid}


def keltner(cs, end, period, mult):
    """Keltner channel: SMA mid ± mult × SMA(true range). Carter's TTM uses this family."""
    trs = true_ranges(cs)
    if not trs:
        return None
    closes = _closes(cs)
    if closes is None:
        return None
    mid = sma(closes, end, period)
    atr = sma(trs, end, period)
    if mid is None or atr is None:
        return None
    return {"mid": mid, "upper": mid + mult * atr, "lower": mid - mult * atr}


def squeeze_breakout(cs, end, cfg):
    """TTM-style squeeze then a close outside the Bollinger band with rising volume.

    John Carter, Mastering the Trade: a squeeze is Bollinger bands inside the
    Keltner channel. John Bollinger asks for a volume expansion on the release.
    Direction is the side of the close.
    """
    period = int(cfg["squeeze_bb_period"])
    need = int(cfg["squeeze_min_bars_inside"])
    if end < period + need:
        return None
    inside = 0
    for j in range(end - need, end):
        bb = bollinger(_closes(cs), j, period, float(cfg["squeeze_bb_mult"]))
        kc = keltner(cs, j, period, float(cfg["squeeze_kc_mult"]))
        if not bb or not kc:
            return None
        if bb["upper"] < kc["upper"] and bb["lower"] > kc["lower"]:
            inside += 1
    if inside < need:
        return {"hit": False}
    bb = bollinger(_closes(cs), end, period, float(cfg["squeeze_bb_mult"]))
    if not bb:
        return None
    close = _f(cs[end].get("c"))
    if close is None:
        return None
    side = None
    if close > bb["upper"]:
        side = "long"
    elif close < bb["lower"]:
        side = "short"
    if side is None:
        return {"hit": False}
    # Volume confirmation: current notional vs the mean of the prior `period` bars.
    def ntl(c):
        h, l, cl, v = _f(c.get("h")), _f(c.get("l")), _f(c.get("c")), _f(c.get("v"))
        if None in (h, l, cl, v):
            return None
        return v * (h + l + cl) / 3
    cur = ntl(cs[end])
    prior = [ntl(cs[k]) for k in range(end - period, end)]
    if cur is None or any(p is None for p in prior) or not prior:
        return None
    avg = sum(prior) / len(prior)
    mult = (cur / avg) if avg > 0 else None
    hit = mult is not None and mult >= float(cfg["squeeze_vol_mult"])
    return {"hit": hit, "side": side, "vol_mult": mult, "bandwidth": bb["bandwidth"]}


def bar_notional(c):
    h, l, cl, v = _f(c.get("h")), _f(c.get("l")), _f(c.get("c")), _f(c.get("v"))
    if None in (h, l, cl, v):
        return None
    return v * (h + l + cl) / 3


def median(values):
    vals = sorted(values)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2


def close_location(c):
    """Where the close sits in the bar. +1 is the high, -1 is the low.

    This is the close-location value used by Larry Williams and by Arms:
    (2*close - high - low) / (high - low).
    """
    h, l, cl = _f(c.get("h")), _f(c.get("l")), _f(c.get("c"))
    if h is None or l is None or cl is None:
        return 0.0
    span = h - l
    if span <= 0:
        return 0.0
    return (2 * cl - h - l) / span


def _absorption_bar(cs, end, cfg):
    """One hourly bar versus the median of the prior 24–48h.

    Wyckoff's effort versus result: volume is the effort, the spread is the
    result. RVOL here is this bar's notional divided by the median notional
    of the prior window (the median, not the mean, so one climax does not
    redefine what is normal). The 8× multiple is the SOPH tape the owner
    measured (about 8–10× and about 20×), which is the climactic end of
    relative volume rather than the usual 2× 'unusual volume' screen.
    """
    period = int(cfg["absorption_median_bars"])
    if end < period:
        return {"hit": False}
    cur = bar_notional(cs[end])
    o = _f(cs[end].get("o"))
    h, l = _f(cs[end].get("h")), _f(cs[end].get("l"))
    cl = _f(cs[end].get("c"))
    if cur is None or o is None or not o or h is None or l is None or cl is None:
        return {"hit": False}
    ret = abs(cl / o - 1) * 100
    rng = (h - l) / o * 100
    if ret > float(cfg["absorption_max_return_pct"]) or rng > float(cfg["absorption_max_range_pct"]):
        return {"hit": False, "return_pct": ret, "range_pct": rng}
    prior = [bar_notional(cs[k]) for k in range(end - period, end)]
    if any(p is None for p in prior):
        return {"hit": False}
    base = median(prior)
    avg = sum(prior) / len(prior)
    if not base or base <= 0 or not avg or avg <= 0:
        return {"hit": False}
    rvol = cur / base
    # The median of a lumpy day sits in the quiet hours, so 8× the median
    # also flags an ordinary busy hour. Standard relative volume divides by
    # the average. Both have to clear the same multiple.
    rvol_mean = cur / avg
    hit = rvol >= float(cfg["absorption_rvol_min"]) and rvol_mean >= float(cfg["absorption_mean_mult"])
    return {
        "hit": hit,
        "rvol": rvol,
        "rvol_mean": rvol_mean,
        "return_pct": ret,
        "range_pct": rng,
    }


def absorption(cs, end, cfg, delta_side=None):
    """Climactic hourly volume inside a small range. Repeats strengthen it.

    A second (or later) spike inside the same 24–48h window is the campaign
    Wyckoff describes: one effort can be noise, a repeated effort against a
    stuck price is absorption. Direction is the taker imbalance when that
    sample exists, otherwise the close location of the bar.
    """
    got = _absorption_bar(cs, end, cfg)
    if not got.get("hit"):
        return {"hit": False, "repeated": False, "repeats": 0}
    window = int(cfg["absorption_repeat_bars"])
    repeats = 0
    first = None
    for j in range(end - window + 1, end + 1):
        bar = _absorption_bar(cs, j, cfg)
        if bar.get("hit"):
            repeats += 1
            if first is None:
                first = j
    lo = hi = None
    if first is not None:
        for j in range(first, end + 1):
            l, h = _f(cs[j].get("l")), _f(cs[j].get("h"))
            if l is None or h is None:
                continue
            lo = l if lo is None else min(lo, l)
            hi = h if hi is None else max(hi, h)
    side = None
    if delta_side in ("long", "short"):
        side = delta_side
    else:
        clv = close_location(cs[end])
        if clv > 0:
            side = "long"
        elif clv < 0:
            side = "short"
        else:
            o, cl = _f(cs[end].get("o")), _f(cs[end].get("c"))
            if o is not None and cl is not None:
                if cl > o:
                    side = "long"
                elif cl < o:
                    side = "short"
    return {
        "hit": True,
        "rvol": got.get("rvol"),
        "return_pct": got.get("return_pct"),
        "range_pct": got.get("range_pct"),
        "repeats": repeats,
        "repeated": repeats >= int(cfg["absorption_min_repeats"]),
        "side": side,
        "range_low": lo,
        "range_high": hi,
    }


def vol_accel(cs, end, cfg, min_notional):
    """Relative volume while price has not extended.

    RVOL = bar notional / average notional. Wyckoff: high effort, small result
    means supply or demand is being absorbed before the price leaves the range.
    """
    period = int(cfg["vol_accel_avg_bars"])
    if end < period:
        return {"hit": False}
    def ntl(c):
        h, l, cl, v = _f(c.get("h")), _f(c.get("l")), _f(c.get("c")), _f(c.get("v"))
        if None in (h, l, cl, v):
            return None
        return v * (h + l + cl) / 3
    o, cl = _f(cs[end].get("o")), _f(cs[end].get("c"))
    cur = ntl(cs[end])
    if o is None or cl is None or not o or cur is None:
        return {"hit": False}
    pct = abs(cl / o - 1) * 100
    prior = [ntl(cs[k]) for k in range(end - period, end)]
    if any(p is None for p in prior):
        return {"hit": False}
    avg = sum(prior) / period
    mult = (cur / avg) if avg > 0 else None
    hit = (
        pct < float(cfg["vol_accel_max_price_pct"])
        and mult is not None
        and mult >= float(cfg["vol_accel_mult"])
        and cur >= float(min_notional)
    )
    return {"hit": hit, "vol_mult": mult, "pct": pct}


def oi_buildup(oi_now, oi_then, price_now, price_then, cfg):
    """Open interest up while price is flat.

    Futures textbooks (CME open-interest readings): OI rising and price unchanged
    means new positions are being opened, not an old position being closed.
    Direction is unknown until price leaves the range, so this component has no side.
    """
    if not oi_now or not oi_then or not price_now or not price_then:
        return {"hit": False}
    oi_pct = (oi_now / oi_then - 1) * 100
    px_pct = abs(price_now / price_then - 1) * 100
    hit = oi_pct >= float(cfg["oi_buildup_pct"]) and px_pct < float(cfg["oi_buildup_max_price_pct"])
    return {"hit": hit, "oi_pct": oi_pct, "px_pct": px_pct}


def funding_shift(now, prev, cfg):
    """Hourly funding percent jumped. Crowded positioning, low weight.

    Hyperliquid's funding is the hourly premium. A sharp change means the
    crowd's paid bias just moved. It never supplies a trade direction alone.
    """
    if now is None or prev is None:
        return {"hit": False}
    delta = now - prev
    hit = abs(delta) >= float(cfg["funding_shift_abs"])
    # Positive funding = longs pay shorts = crowded long.
    bias = "short" if delta > 0 else "long"
    return {"hit": hit, "delta": delta, "bias": bias if hit else None}


def cvd_from_trades(trades):
    """Taker-signed notional. Hyperliquid side B is the buying aggressor, A the seller."""
    buy = sell = 0.0
    for t in trades:
        px, sz = _f(t.get("px")), _f(t.get("sz"))
        if px is None or sz is None:
            continue
        ntl = px * sz
        if t.get("side") == "B":
            buy += ntl
        elif t.get("side") == "A":
            sell += ntl
    total = buy + sell
    return {
        "buy": buy,
        "sell": sell,
        "cvd": buy - sell,
        "imbalance": ((buy - sell) / total) if total else 0.0,
        "notional": total,
    }


def cvd_absorption(stats, price_pct, cfg):
    """Aggressive one-sided flow while price is still inside a small range.

    Order-flow practice: a positive delta that does not move price is absorption
    by passive sellers (or the mirror). The side is the aggressor side.
    """
    if not stats or stats.get("notional", 0) < float(cfg["cvd_min_notional_usd"]):
        return {"hit": False}
    if price_pct is None or abs(price_pct) >= float(cfg["cvd_max_price_pct"]):
        return {"hit": False}
    imb = stats.get("imbalance") or 0.0
    if imb >= float(cfg["cvd_imbalance_min"]):
        return {"hit": True, "side": "long", "imbalance": imb}
    if imb <= -float(cfg["cvd_imbalance_min"]):
        return {"hit": True, "side": "short", "imbalance": imb}
    return {"hit": False, "imbalance": imb}


def cvd_divergence(stats, price_pct, cfg):
    """Price and aggressive flow disagree. Classic delta divergence: weak push."""
    if not stats or stats.get("notional", 0) < float(cfg["cvd_min_notional_usd"]):
        return False
    if price_pct is None or abs(price_pct) < float(cfg["cvd_max_price_pct"]):
        return False
    imb = stats.get("imbalance") or 0.0
    if price_pct > 0 and imb <= -float(cfg["cvd_imbalance_min"]):
        return True
    if price_pct < 0 and imb >= float(cfg["cvd_imbalance_min"]):
        return True
    return False


def regime_delta(side, btc_up, fear_greed):
    """Small context tilt. Cannot create an alert by itself (max ±2).

    BTC trend: trade with the leader, a standard regime filter.
    Crypto Fear & Greed (alternative.me): extremes mark a crowded mood.
    """
    if side not in ("long", "short"):
        return 0
    delta = 0
    if btc_up is True:
        delta += 1 if side == "long" else -1
    elif btc_up is False:
        delta += 1 if side == "short" else -1
    if fear_greed is not None:
        if fear_greed <= 25:
            delta += 1 if side == "long" else -1
        elif fear_greed >= 75:
            delta += 1 if side == "short" else -1
    return delta


def confluence(parts, cfg, side, btc_up=None, fear_greed=None):
    """Weighted sum. Weights live in config and are justified in the replay report."""
    score = 0
    fired = []
    # Absorption is the strict form of the same effort-versus-result idea as
    # vol_accel. Counting both would let one climax bar vote twice.
    if parts.get("absorption_repeat"):
        score += int(cfg["early_weight_absorption_repeat"])
        fired.append("absorption")
    elif parts.get("absorption"):
        score += int(cfg["early_weight_absorption"])
        fired.append("absorption")
    elif parts.get("vol_accel"):
        score += int(cfg["early_weight_vol"])
        fired.append("vol_accel")
    if parts.get("squeeze"):
        score += int(cfg["early_weight_squeeze"])
        fired.append("squeeze_breakout")
    if parts.get("oi_buildup"):
        score += int(cfg["early_weight_oi"])
        fired.append("oi_buildup")
    if parts.get("cvd"):
        score += int(cfg["early_weight_cvd"])
        fired.append("cvd_absorption")
    if parts.get("funding"):
        score += int(cfg["early_weight_funding"])
        fired.append("funding_shift")
    if parts.get("divergence"):
        score -= int(cfg["early_weight_divergence"])
        fired.append("cvd_divergence")
    score += regime_delta(side, btc_up, fear_greed)
    return {
        "score": score,
        "triggers": [n for n in fired if n != "cvd_divergence"],
        "hit": score >= int(cfg["early_score_min"]) and side in ("long", "short") and bool(
            parts.get("vol_accel") or parts.get("squeeze") or parts.get("cvd")
            or parts.get("oi_buildup") or parts.get("absorption") or parts.get("absorption_repeat")
        ),
        "side": side,
    }
