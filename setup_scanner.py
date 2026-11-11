#!/usr/bin/env python3
"""Tradable entry setups. Documented rules, not a new indicator.

Trend continuation
  Turtle / Donchian: a close beyond the prior 20-bar high or low
  (Richard Dennis, the Turtle 20-day breakout, here on 1h candles),
  with the 20-period EMA on the same side of the 50-period EMA and
  volume at least 1.5 times the prior 20-bar average.
  Or a pullback that touches the 20 EMA inside that trend, with higher
  swing lows (uptrend) or lower swing highs (downtrend).

  Initial stop distance is the larger of 2 ATR and the distance to the
  last swing beyond price, so the stop is never tighter than 2 ATR and
  still sits beyond that swing. ATR is Wilder's, the one the Turtles and
  the Chandelier Exit use. The trail is Charles LeBeau's Chandelier Exit,
  3 ATR from the best price since entry, and it stays off until the trade
  is up one R.

Support retest (long only)
  Price within 0.3% of an earlier swing low, and the completed 4h lows
  are rising. A sequence of lower 1h highs whose latest high is still
  below TP1 vetoes the long: the path to the target is already failing.

A trend trade has no fixed structural target. It exits at the initial
stop, then at the Chandelier once the trade is up one R. Half can come
off at 2R. The only ceiling is a major high or low: the 24h or 7d
extreme of the completed 4h candles, and only when that level sits
within one R of the entry.

A support retest still uses the entry_ready rule: TP1 is the first
structural level, and the alert exists only when that level pays 2R.
"""
import watch as watchmod

FEE_PCT_PER_SIDE = 0.045  # Hyperliquid taker, percent
TRAIL_RULE = "Chandelier 3 ATR (LeBeau) desde el mejor precio, solo tras +1R; antes manda el stop inicial"


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def closes(cs):
    out = []
    for c in cs:
        v = _f(c.get("c"))
        if v is None:
            return None
        out.append(v)
    return out


def ema_series(values, period):
    """EMA seeded with the SMA of the first `period` values. None before that."""
    period = int(period)
    if values is None or len(values) < period or period < 1:
        return None
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out = [None] * (period - 1) + [seed]
    prev = seed
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def wilder_atr(cs, period):
    """Wilder ATR. Index 0 of the result aligns with the candles."""
    period = int(period)
    if not cs or len(cs) < period + 1:
        return None
    trs = []
    prev_close = _f(cs[0].get("c"))
    if prev_close is None:
        return None
    trs.append(max(_f(cs[0].get("h")) - _f(cs[0].get("l")), 0))
    for c in cs[1:]:
        h, l, cl = _f(c.get("h")), _f(c.get("l")), _f(c.get("c"))
        if None in (h, l, cl):
            return None
        trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
        prev_close = cl
    atr = [None] * len(cs)
    first = sum(trs[1:period + 1]) / period
    atr[period] = first
    prev = first
    for i in range(period + 1, len(cs)):
        prev = (prev * (period - 1) + trs[i]) / period
        atr[i] = prev
    return atr


def bar_notional(c):
    h, l, cl, v = _f(c.get("h")), _f(c.get("l")), _f(c.get("c")), _f(c.get("v"))
    if None in (h, l, cl, v):
        return None
    return v * (h + l + cl) / 3.0


def _swing_indexes(values, wing, kind):
    """Indexes of Williams fractals. kind is 'high' or 'low'."""
    found = []
    for i in range(wing, len(values) - wing):
        v = values[i]
        if v is None:
            continue
        ok = True
        for j in range(i - wing, i + wing + 1):
            if j == i or values[j] is None:
                continue
            if kind == "high" and not v > values[j]:
                ok = False
                break
            if kind == "low" and not v < values[j]:
                ok = False
                break
        if ok:
            found.append(i)
    return found


def last_swing(cs, end, wing, kind):
    """Latest confirmed fractal at or before `end`."""
    vals = []
    for c in cs[:end + 1]:
        vals.append(_f(c.get("h") if kind == "high" else c.get("l")))
    # A fractal needs `wing` bars to its right, so it cannot sit on `end`.
    idxs = _swing_indexes(vals, wing, kind)
    if not idxs:
        return None
    return vals[idxs[-1]]


def four_hour_lows(cs, end):
    """Completed 4h lows up to and including the bucket that contains `end` only if full."""
    buckets = {}
    for c in cs[:end + 1]:
        t = c.get("t") or 0
        h = _f(c.get("l"))
        if h is None:
            continue
        buckets.setdefault(t // (4 * 3_600_000), []).append(h)
    lows = []
    for key in sorted(buckets):
        bars = buckets[key]
        if len(bars) < 4:
            continue
        lows.append(min(bars))
    return lows


def rising(seq, n=3):
    if len(seq) < n:
        return False
    tail = seq[-n:]
    return all(tail[i] > tail[i - 1] for i in range(1, n))


def lower_high_veto(cs, end, tp1):
    """Three closed hourly highs stepping down, and the last of them below TP1.

    The last bar of `cs` is treated as still forming when `end` is before it.
    `end` is the closed bar being judged, so the sequence is that bar and the
    two before it.
    """
    tp1 = _f(tp1)
    if tp1 is None or end < 2:
        return False
    highs = []
    for i in (end - 2, end - 1, end):
        h = _f(cs[i].get("h"))
        if h is None:
            return False
        highs.append(h)
    return highs[0] > highs[1] > highs[2] and highs[2] < tp1


def _volume_ok(cs, end, mult, lookback):
    cur = bar_notional(cs[end])
    if cur is None or end < lookback:
        return False
    prior = [bar_notional(cs[i]) for i in range(end - lookback, end)]
    if any(p is None for p in prior):
        return False
    avg = sum(prior) / lookback
    return avg > 0 and cur >= float(mult) * avg


def _donchian(cs, end, period, kind):
    if end < period:
        return None
    vals = []
    for c in cs[end - period:end]:
        v = _f(c.get("h") if kind == "high" else c.get("l"))
        if v is None:
            return None
        vals.append(v)
    return max(vals) if kind == "high" else min(vals)


def _stop(side, price, atr, swing, atr_mult):
    if atr is None or atr <= 0 or price is None:
        return None
    atr_dist = float(atr_mult) * atr
    if side == "long":
        dist = atr_dist
        if swing is not None and swing < price:
            dist = max(dist, price - swing)
        return price - dist
    if side == "short":
        dist = atr_dist
        if swing is not None and swing > price:
            dist = max(dist, swing - price)
        return price + dist
    return None


def _confidence(asset, cfg):
    """0, or -1 when funding or the premium is already stretched."""
    funding = _f(asset.get("funding_pct")) if asset else None
    premium = _f(asset.get("premium_pct")) if asset else None
    hot_f = float(cfg["setup_funding_hot_pct"])
    hot_p = float(cfg["setup_premium_hot_pct"])
    reasons = []
    if funding is not None and funding > hot_f:
        reasons.append("funding")
    if premium is not None and premium > hot_p:
        reasons.append("premium")
    return (-1 if reasons else 0), reasons


def _passes_book(asset, side, cfg):
    if not asset:
        return False
    day = _f(asset.get("dayNtlVlm")) or 0
    if side == "long":
        return day >= float(cfg["tradable_min_day_ntl_usd"])
    return _short_ok(asset, cfg)


def _short_ok(asset, cfg):
    fake = {
        "side": "short",
        "day_ntl": asset.get("dayNtlVlm"),
        "oi_usd": asset.get("oi_usd"),
        "dex": asset.get("dex") or "main",
        "funding_pct": asset.get("funding_pct"),
    }
    # Imported lazily so a unit test of the pure rules does not need detector.
    import detector
    ok, _why = detector.short_entry_allowed(fake, cfg)
    return ok


def completed_four_hour(cs, end):
    """4h highs and lows from the hourly bars. The bucket that holds `end` is dropped if it is short."""
    buckets = {}
    for c in cs[:end + 1]:
        t = c.get("t") or 0
        key = t // (4 * 3_600_000)
        row = buckets.setdefault(key, {"h": None, "l": None, "n": 0})
        h, lo = _f(c.get("h")), _f(c.get("l"))
        if h is not None:
            row["h"] = h if row["h"] is None else max(row["h"], h)
        if lo is not None:
            row["l"] = lo if row["l"] is None else min(row["l"], lo)
        row["n"] += 1
    full = [buckets[k] for k in sorted(buckets) if buckets[k]["n"] >= 4]
    return full


def major_level_within_1r(cs, end, side, price, risk):
    """24h or 7d extreme of completed 4h candles, in front of the trade and closer than 1R.

    Returns (blocked, detail). A breakout that has already cleared that extreme is not blocked.
    """
    price, risk = _f(price), _f(risk)
    if price is None or risk is None or risk <= 0 or side not in ("long", "short"):
        return False, None
    bars = completed_four_hour(cs, end)
    if not bars:
        return False, None
    kind = "h" if side == "long" else "l"
    for name, n in (("24h", 6), ("7d", 42)):
        window = bars[-n:]
        vals = [b[kind] for b in window if b[kind] is not None]
        if not vals:
            continue
        level = max(vals) if side == "long" else min(vals)
        gap = (level - price) if side == "long" else (price - level)
        if gap > 0 and gap < risk:
            return True, {"window": name, "level": level, "gap_r": gap / risk}
    return False, None


def _payload(cs, end, side, kind, asset, cfg, price, sl, atr, tp1, tp2, rr, managed):
    conf, reasons = _confidence(asset, cfg)
    risk = abs(price - sl)
    partial = None
    if managed == "chandelier" and risk > 0:
        partial = price + 2 * risk if side == "long" else price - 2 * risk
    return {
        "type": "setup",
        "setup": kind,
        "managed": managed,
        "coin": (asset or {}).get("coin"),
        "dex": (asset or {}).get("dex") or "main",
        "side": side,
        "side_label": "largo" if side == "long" else "corto",
        "entry": price,
        "sl": sl,
        "initial_sl": sl,
        "tp1": partial if managed == "chandelier" else tp1,
        "tp2": tp2 if tp2 is not None else (partial if managed == "chandelier" else tp1),
        "partial_tp": partial,
        "rr": 2.0 if managed == "chandelier" else round(rr, 2),
        "trigger": kind,
        "trail_rule": TRAIL_RULE,
        "confidence": conf,
        "confidence_notes": reasons,
        "funding_pct": (asset or {}).get("funding_pct"),
        "premium_pct": (asset or {}).get("premium_pct"),
        "atr": atr,
    }


def _build(cs, end, side, kind, asset, cfg):
    price = _f(cs[end].get("c"))
    atrs = wilder_atr(cs[:end + 1], int(cfg["atr_period"]))
    if price is None or not atrs or atrs[end] is None:
        return None, "atr"
    wing = int(cfg["swing_wing"])
    swing = last_swing(cs, end, wing, "low" if side == "long" else "high")
    sl = _stop(side, price, atrs[end], swing, cfg["setup_atr_mult"])
    if sl is None:
        return None, "atr"
    risk = abs(price - sl)
    if risk <= 0:
        return None, "atr"
    if kind in ("trend_breakout", "trend_pullback"):
        blocked, _detail = major_level_within_1r(cs, end, side, price, risk)
        if blocked:
            return None, "major_level"
        return _payload(cs, end, side, kind, asset, cfg, price, sl, atrs[end], None, None, None, "chandelier"), None
    tp1, tp2 = watchmod.structural_targets(
        cs[:end + 1], side, price, wing=wing, day_bars=int(cfg["avg_window_hours"]),
    )
    if tp1 is None:
        return None, "retest_no_level"
    if lower_high_veto(cs, end, tp1):
        return None, "retest_lower_highs"
    rr = watchmod.reward_risk(side, price, sl, tp1)
    if rr is None or rr < float(cfg["watch_rr_min"]):
        return None, "retest_rr"
    return _payload(cs, end, side, kind, asset, cfg, price, sl, atrs[end], tp1, tp2, rr, "structural"), None


def trend_breakout(cs, end, asset, cfg):
    got, _reason = inspect_breakout(cs, end, asset, cfg)
    return got


def inspect_breakout(cs, end, asset, cfg):
    """Return (signal or None, filter). `quiet` means the bar is not a channel break."""
    period = int(cfg["setup_donchian"])
    fast_n, slow_n = int(cfg["setup_ema_fast"]), int(cfg["setup_ema_slow"])
    cl = closes(cs[:end + 1])
    if cl is None or end < max(period, slow_n):
        return None, "short_history"
    fast, slow = ema_series(cl, fast_n), ema_series(cl, slow_n)
    if not fast or not slow or fast[end] is None or slow[end] is None:
        return None, "short_history"
    close = cl[end]
    hi = _donchian(cs, end, period, "high")
    lo = _donchian(cs, end, period, "low")
    long_break = hi is not None and close > hi and fast[end] > slow[end]
    short_break = lo is not None and close < lo and fast[end] < slow[end]
    # A close beyond the channel with the EMA on the wrong side is its own bucket.
    long_raw = hi is not None and close > hi
    short_raw = lo is not None and close < lo
    if not long_raw and not short_raw:
        return None, "quiet"
    side = "long" if long_raw and not short_raw else "short" if short_raw and not long_raw else ("long" if long_break else "short")
    if (side == "long" and not long_break) or (side == "short" and not short_break):
        return None, "ema"
    if not _volume_ok(cs, end, cfg["setup_vol_mult"], period):
        return None, "volume"
    if not _passes_book(asset, side, cfg):
        return None, "book_5m" if side == "short" else "book_1m"
    got, reason = _build(cs, end, side, "trend_breakout", asset, cfg)
    if got:
        return got, "taken"
    return None, reason or "atr"


def trend_pullback(cs, end, asset, cfg):
    fast_n, slow_n = int(cfg["setup_ema_fast"]), int(cfg["setup_ema_slow"])
    cl = closes(cs[:end + 1])
    if cl is None or end < slow_n:
        return None
    fast, slow = ema_series(cl, fast_n), ema_series(cl, slow_n)
    if not fast or not slow or fast[end] is None or slow[end] is None:
        return None
    wing = int(cfg["swing_wing"])
    high = _f(cs[end].get("h"))
    low = _f(cs[end].get("l"))
    if high is None or low is None:
        return None
    ema = fast[end]
    touched = low <= ema <= high
    if not touched:
        return None
    vals_lo = [_f(c.get("l")) for c in cs[:end + 1]]
    vals_hi = [_f(c.get("h")) for c in cs[:end + 1]]
    if fast[end] > slow[end]:
        lows = _swing_indexes(vals_lo, wing, "low")
        if not (len(lows) >= 2 and vals_lo[lows[-1]] > vals_lo[lows[-2]]):
            return None
        if not _passes_book(asset, "long", cfg):
            return None
        got, _reason = _build(cs, end, "long", "trend_pullback", asset, cfg)
        return got
    if fast[end] < slow[end]:
        highs = _swing_indexes(vals_hi, wing, "high")
        if not (len(highs) >= 2 and vals_hi[highs[-1]] < vals_hi[highs[-2]]):
            return None
        if not _passes_book(asset, "short", cfg):
            return None
        got, _reason = _build(cs, end, "short", "trend_pullback", asset, cfg)
        return got
    return None


def support_retest(cs, end, asset, cfg):
    """Long only. Near a prior swing low, with rising 4h lows, unless lower highs veto."""
    if not _passes_book(asset, "long", cfg):
        return None
    price = _f(cs[end].get("c"))
    if price is None:
        return None
    wing = int(cfg["swing_wing"])
    lows = [_f(c.get("l")) for c in cs[:end + 1]]
    idxs = [i for i in _swing_indexes(lows, wing, "low") if i <= end - wing]
    tol = float(cfg["setup_support_pct"]) / 100.0
    near = None
    for i in idxs:
        level = lows[i]
        if level and abs(price - level) / level <= tol:
            if near is None or abs(price - level) < abs(price - near):
                near = level
    if near is None:
        return None
    if not rising(four_hour_lows(cs, end), 3):
        return None
    # TP1 is decided inside _build; the lower-high veto and the 2R test run there.
    got, _reason = _build(cs, end, "long", "support_retest", asset, cfg)
    return got


def evaluate(cs, end, asset, cfg):
    """One closed bar. Breakout wins over a pullback, which wins over a retest."""
    for fn in (trend_breakout, trend_pullback, support_retest):
        got = fn(cs, end, asset, cfg)
        if got:
            return got
    return None


def net_trade(entry, exit_price, risk, side, fee_pct_per_side=FEE_PCT_PER_SIDE):
    """PnL after taker fees on both sides. Returns (net_R, net_pct)."""
    entry, exit_price, risk = _f(entry), _f(exit_price), _f(risk)
    if not entry or not exit_price or not risk or risk <= 0 or side not in ("long", "short"):
        return None
    sign = 1 if side == "long" else -1
    gross_pct = (exit_price / entry - 1) * 100 * sign
    gross_r = (exit_price - entry) / risk * sign
    fee_pct = 2 * float(fee_pct_per_side)
    fee_r = (fee_pct / 100.0) * entry / risk
    return gross_r - fee_r, gross_pct - fee_pct


def simulate(cs, entry_index, side, stop, atr_mult_trail, risk, atr_at):
    """Walk forward. Stop is checked before the trail updates.

    Returns (exit_index, exit_price, partial_price or None). Half the
    position is marked at +2R when that price trades, and the other half
    follows the Chandelier. The trail uses ATR frozen at entry. It replaces
    the initial stop only after +1R, and it never loosens. A bar that hits
    the stop and the target is treated as a stop.
    """
    entry = _f(cs[entry_index].get("c"))
    if entry is None or risk is None or risk <= 0:
        return None
    best = entry
    stop_now = stop
    armed = False
    partial = None
    trail_width = float(atr_mult_trail) * atr_at
    target = entry + 2 * risk if side == "long" else entry - 2 * risk
    for i in range(entry_index + 1, len(cs)):
        h, l, o = _f(cs[i].get("h")), _f(cs[i].get("l")), _f(cs[i].get("o"))
        if None in (h, l, o):
            continue
        if side == "long" and l <= stop_now:
            return i, stop_now if o >= stop_now else o, partial
        if side == "short" and h >= stop_now:
            return i, stop_now if o <= stop_now else o, partial
        if side == "long":
            if partial is None and h >= target:
                partial = target
            best = max(best, h)
            if best - entry >= risk:
                armed = True
            if armed:
                stop_now = max(stop_now, best - trail_width)
        else:
            if partial is None and l <= target:
                partial = target
            best = min(best, l)
            if entry - best >= risk:
                armed = True
            if armed:
                stop_now = min(stop_now, best + trail_width)
    last = _f(cs[-1].get("c"))
    return len(cs) - 1, last, partial


def net_managed(entry, exit_price, risk, side, partial=None, fee_pct_per_side=FEE_PCT_PER_SIDE):
    """Net R and percent. A partial books half at +2R and half at the trail exit. Fees once."""
    entry, exit_price, risk = _f(entry), _f(exit_price), _f(risk)
    if not entry or exit_price is None or not risk or risk <= 0 or side not in ("long", "short"):
        return None
    sign = 1 if side == "long" else -1
    if partial is None:
        gross_r = (exit_price - entry) / risk * sign
        gross_pct = (exit_price / entry - 1) * 100 * sign
    else:
        gross_r = 0.5 * ((partial - entry) / risk * sign) + 0.5 * ((exit_price - entry) / risk * sign)
        gross_pct = 0.5 * ((partial / entry - 1) * 100 * sign) + 0.5 * ((exit_price / entry - 1) * 100 * sign)
    fee_pct = 2 * float(fee_pct_per_side)
    fee_r = (fee_pct / 100.0) * entry / risk
    return gross_r - fee_r, gross_pct - fee_pct
