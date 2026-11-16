#!/usr/bin/env python3
"""1h long when a crowded, soft tape squeezes. Paper and alerts only.

The book stays inactive until the hourly market log spans 7 days. OI is the
contract open interest in that log (not a backfill). A name needs OI up at
least 20% versus 168h earlier, a 7-day price change between -15% and +5%,
and a negative premium. The closed 1h candle must rise at least 3% from its
open on at least 3x the prior 20-candle average base volume.

Exit: stop entry-2*ATR, trail highest high-4*ATR, max hold 120 candles.
Never places an order.
"""
import paper

OI_MIN = 0.20
PRICE_MIN = -0.15
PRICE_MAX = 0.05
RET_MIN = 0.03
VOL_MULT = 3.0
VOL_BARS = 20
LOOKBACK_H = 168
FETCH_BARS = 400

CFG = {
    "book": "trapped_shorts1h",
    "interval": paper.HOUR_MS,
    "max_hold": 120,
    "max_positions": 5,
    "k": 2.0,
    "t_mult": 4.0,
    "risk_usd": paper.RISK_USD,
}


def prepare(candles):
    return paper.index_series(candles, paper.wilder_atr(candles, 14))


def price_change(candles, i, lookback=LOOKBACK_H):
    """Close-to-close change over `lookback` hourly bars, or None if gapped."""
    if i < lookback or i >= len(candles):
        return None
    now = candles[i]
    prev = candles[i - lookback]
    if now["t"] - prev["t"] != lookback * paper.HOUR_MS or prev["c"] <= 0:
        return None
    return now["c"] / prev["c"] - 1.0


def trigger(candles, i, oi_now, oi_then, premium, oi_min=OI_MIN, ret_min=RET_MIN, vol_mult=VOL_MULT):
    """True when candle i matches the trapped-shorts long."""
    if i < VOL_BARS or i >= len(candles):
        return False
    if oi_now is None or oi_then is None or oi_then <= 0:
        return False
    # A hair of tolerance so an exact +20% is not rejected by binary rounding.
    if (oi_now - oi_then) / oi_then + 1e-12 < oi_min:
        return False
    change = price_change(candles, i)
    if change is None or change + 1e-12 < PRICE_MIN or change - 1e-12 > PRICE_MAX:
        return False
    if premium is None or premium >= 0:
        return False
    bar = candles[i]
    if bar["o"] <= 0 or bar["c"] / bar["o"] - 1.0 + 1e-12 < ret_min:
        return False
    window = candles[i - VOL_BARS:i]
    avg = sum(c["v"] for c in window) / float(VOL_BARS)
    if avg <= 0 or bar["v"] + 1e-12 < vol_mult * avg:
        return False
    return True


def make_signal(series, info_by_coin, latest_t, enabled):
    """Signals only on `latest_t`, and only after the OI log has warmed up."""

    def signal(coin, i):
        if not enabled:
            return None
        prep = series.get(coin) or {}
        candles = prep.get("candles") or []
        if i >= len(candles) or candles[i]["t"] != latest_t:
            return None
        info = info_by_coin.get(coin) or {}
        if not trigger(
            candles, i, info.get("oi_now"), info.get("oi_then"), info.get("premium"),
        ):
            return None
        return {"side": "long"}

    return signal
