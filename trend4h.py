#!/usr/bin/env python3
"""Symmetric slow trend on Hyperliquid main-dex perps. Paper and alerts only.

Closed 4h candles. EMA100 crossing EMA200 is the signal. The paper fill is
the open of the next 4h candle. Exit on the opposite cross (stop-and-reverse),
on a 3*ATR stop, or 180 candles after entry. Never places an order.
"""
import paper

UNIVERSE = (
    "AAVE", "ADA", "ASTER", "AVAX", "BNB", "BTC", "CRV", "DOGE", "ENA", "ETH",
    "FARTCOIN", "HYPE", "LAUNCHCOIN", "LINK", "LIT", "MKR", "PAXG", "PUMP",
    "SOL", "SUI", "TAO", "XPL", "XRP", "ZEC", "kPEPE",
)
WARMUP = 600
EMA_FAST = 100
EMA_SLOW = 200
ATR_PERIOD = 14
FETCH_BARS = 820

CFG = {
    "book": "trend4h",
    "interval": paper.FOUR_H_MS,
    "max_hold": 180,
    "max_positions": 10,
    "k": 3.0,
    "t_mult": None,
    "risk_usd": paper.RISK_USD,
}


def prepare(candles, ema_fast=EMA_FAST, ema_slow=EMA_SLOW, atr_period=ATR_PERIOD):
    closes = [c["c"] for c in candles]
    series = paper.index_series(candles, paper.wilder_atr(candles, atr_period))
    series["ema_fast"] = paper.ema_series(closes, ema_fast)
    series["ema_slow"] = paper.ema_series(closes, ema_slow)
    return series


def make_signal(series, warmup=WARMUP):
    """Cross detector. Indexes below `warmup` cannot signal."""

    def signal(coin, i):
        prep = series.get(coin) or {}
        if i < int(warmup) - 1:
            return None
        side = paper.cross_side(prep.get("ema_fast"), prep.get("ema_slow"), i)
        if not side:
            return None
        fast = prep.get("ema_fast") or []
        slow = prep.get("ema_slow") or []
        return {
            "side": side,
            "ema100": fast[i] if i < len(fast) else None,
            "ema200": slow[i] if i < len(slow) else None,
        }

    return signal
