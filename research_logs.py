#!/usr/bin/env python3
"""Load the hourly research logs into pandas.

The CSV files are written on the detector-state branch under data/. This
module does not call Hyperliquid and does not read an exchange key.

    import research_logs
    books = research_logs.load_orderbook_log("data")
    proxy = research_logs.load_liq_proxy_log("data")

`load_liq_trades_log` is the public-trade sample that carried a liquidation
field. It is empty while that payload has no such field. `load_liq_proxy_log`
is the OI / range / volume flag, not a count of liquidations.
"""
import re
from pathlib import Path

import market_research
import paper

DAY_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.csv$")


def _pandas():
    try:
        import pandas as pd
    except ImportError as exc:
        raise ImportError("research_logs necesita pandas") from exc
    return pd


def _coerce(frame, numeric, times):
    pd = _pandas()
    for col in times:
        if col in frame.columns:
            frame[col] = pd.to_datetime(frame[col], utc=True, errors="coerce")
    for col in numeric:
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame


def _empty(columns):
    return _pandas().DataFrame(columns=list(columns))


def _read_daily(directory, columns, numeric, times):
    pd = _pandas()
    directory = Path(directory)
    frames = []
    if directory.exists():
        for path in sorted(directory.glob("*.csv")):
            if not DAY_NAME.match(path.name):
                continue
            frames.append(pd.read_csv(path, dtype=str, keep_default_na=False))
    if not frames:
        return _empty(columns)
    frame = pd.concat(frames, ignore_index=True)
    return _coerce(frame, numeric, times)


def _read_one(path, columns, numeric, times):
    pd = _pandas()
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return _empty(columns)
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    return _coerce(frame, numeric, times)


def load_market_log(root="data"):
    """Hourly mark, OI, funding, premium and 24h volume. OI is in contracts."""
    root = Path(root)
    numeric = ("mark", "oi", "oi_usd", "funding", "premium", "dayNtlVlm")
    return _read_daily(root / "market_log", paper.MARKET_FIELDS, numeric, ("ts",))


def load_orderbook_log(root="data"):
    """Depth, imbalance and spread. complete_* is 1 when the band was fully visible."""
    return _read_daily(
        Path(root) / "orderbook_log",
        market_research.ORDERBOOK_FIELDS,
        market_research.ORDERBOOK_NUMERIC,
        ("ts", "book_time"),
    )


def load_listings_log(root="data"):
    """Listings, delistings and max-leverage changes. ts is the first time this job saw them."""
    return _read_one(
        Path(root) / "listings_log.csv",
        market_research.LISTING_FIELDS,
        market_research.LISTING_NUMERIC,
        ("ts",),
    )


def load_liq_proxy_log(root="data"):
    """Hourly OI-drop / range / volume flag. proxy is 1 only when all three fire.

    This is not a liquidation count. oi is contracts, the same unit as market_log.
    """
    return _read_daily(
        Path(root) / "liq_proxy_log",
        market_research.PROXY_FIELDS,
        market_research.PROXY_NUMERIC,
        ("ts", "candle_t"),
    )


def load_liq_trades_log(root="data"):
    """Public trades whose payload included a liquidation field. Usually empty."""
    return _read_daily(
        Path(root) / "liq_trades_log",
        market_research.TRADE_FIELDS,
        market_research.TRADE_NUMERIC,
        ("ts", "trade_time"),
    )


def load_liq_tape_log(root="data"):
    """One row per hour: how long the public tape was open, and how many prints were marked."""
    return _read_daily(
        Path(root) / "liq_tape_log",
        market_research.TAPE_FIELDS,
        market_research.TAPE_NUMERIC,
        ("ts",),
    )


def load_all(root="data"):
    """Every research frame, keyed by the file family."""
    return {
        "market": load_market_log(root),
        "orderbook": load_orderbook_log(root),
        "listings": load_listings_log(root),
        "liq_proxy": load_liq_proxy_log(root),
        "liq_trades": load_liq_trades_log(root),
        "liq_tape": load_liq_tape_log(root),
    }
