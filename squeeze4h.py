#!/usr/bin/env python3
"""4h volatility squeeze then expansion. Long only. Paper and alerts only.

Bandwidth is Bollinger(20, 2) (upper-lower)/mid with a population stdev.
bwpct is that bandwidth's percentile rank inside the last 180 closed 4h
candles. The three candles before the trigger must include one at or below
the 5th percentile. The trigger closes above the prior 20 highs with at
least 1.5x the prior 20-candle average base volume.

Exit is the shared long engine: stop entry-3*ATR, trail highest high-5*ATR
(ratchet up only), max hold 240 candles. Never places an order.
"""
import paper

BB_PERIOD = 20
BB_STD = 2.0
BW_LOOKBACK = 180
BW_MAX = 0.05
SQUEEZE_BARS = 3
DONCHIAN = 20
VOL_MULT = 1.5
FETCH_BARS = 820

CFG = {
    "book": "squeeze4h",
    "interval": paper.FOUR_H_MS,
    "max_hold": 240,
    "max_positions": 10,
    "k": 3.0,
    "t_mult": 5.0,
    "risk_usd": paper.RISK_USD,
}


def prepare(candles, bb_period=BB_PERIOD, lookback=BW_LOOKBACK, nstd=BB_STD):
    closes = [c["c"] for c in candles]
    series = paper.index_series(candles, paper.wilder_atr(candles, 14))
    series["bandwidth"] = paper.bandwidth_series(closes, bb_period, nstd)
    series["bwpct"] = paper.percentile_rank_series(series["bandwidth"], lookback)
    return series


def trigger(prep, i, bw_max=BW_MAX, donchian=DONCHIAN, vol_mult=VOL_MULT, squeeze_bars=SQUEEZE_BARS):
    """True when candle i is a squeeze breakout."""
    candles = prep.get("candles") or []
    bwpct = prep.get("bwpct") or []
    if i < donchian or i < squeeze_bars or i >= len(candles):
        return False
    prior = []
    for step in range(squeeze_bars, 0, -1):
        j = i - step
        if j < 0 or j >= len(bwpct) or bwpct[j] is None:
            return False
        prior.append(bwpct[j])
    if min(prior) > bw_max:
        return False
    window = candles[i - donchian:i]
    if len(window) < donchian:
        return False
    if candles[i]["c"] <= max(c["h"] for c in window):
        return False
    avg = sum(c["v"] for c in window) / float(donchian)
    if avg <= 0 or candles[i]["v"] < vol_mult * avg:
        return False
    return True


def make_signal(series):
    def signal(coin, i):
        prep = series.get(coin)
        if not prep or not trigger(prep, i):
            return None
        return {"side": "long"}

    return signal
