#!/usr/bin/env python3
"""Replay c1_maker over the last 120 days of 1h candles. Paper only.

No exchange keys and no orders. Candles and funding come from the public
info API. The book starts flat. Bars before the window are the 48h pump
history and the ATR only. The live l2Book is not replayed.

The live book is variant A. This pass also reports B and C.

    python3 c1_replay.py
    python3 c1_replay.py --days 120
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import c1_maker
import paper
import perps_all

CACHE = Path("/tmp/c1_replay_cache")


def _cutoff(now_ms, days):
    raw = int(now_ms) - int(days) * 24 * paper.HOUR_MS
    return raw - (raw % paper.HOUR_MS)


def _cache(name, loader):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / name
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            path.unlink()
    payload = loader()
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _universe(now_ms):
    def load():
        return perps_all.post({"type": "metaAndAssetCtxs"})

    raw = _cache(f"meta_{datetime.fromtimestamp(now_ms / 1000, timezone.utc):%Y-%m-%d}.json", load)
    if not isinstance(raw, (list, tuple)) or len(raw) < 2 or not isinstance(raw[0], dict):
        raise RuntimeError("metaAndAssetCtxs no trae el universo")
    meta = raw[0]
    ctxs = raw[1] if isinstance(raw[1], list) else []
    assets = []
    for uni, ctx in zip(meta.get("universe") or [], ctxs):
        if not isinstance(uni, dict) or uni.get("isDelisted"):
            continue
        name = uni.get("name")
        if not name:
            continue
        volume = None
        if isinstance(ctx, dict):
            try:
                volume = float(ctx.get("dayNtlVlm"))
            except (TypeError, ValueError):
                volume = None
        assets.append({"dex": "main", "coin": name, "dayNtlVlm": volume})
    return c1_maker.main_universe(assets)


def _safe(coin):
    return "".join(ch if ch.isalnum() else "_" for ch in coin)


def _candles(coin, start_ms, now_ms, stamp):
    def load():
        return paper.fetch_candles(coin, "1h", start_ms, now_ms)

    raw = _cache(f"{_safe(coin)}_1h_{stamp}.json", load)
    norm = paper.normalize_candles(raw, paper.HOUR_MS)
    closed, _forming = paper.split_candles(norm, now_ms, paper.HOUR_MS)
    return closed


def _funding(coin, start_ms, now_ms, stamp):
    def load():
        try:
            points = paper.fetch_funding(coin, start_ms, now_ms)
        except Exception as exc:
            paper.log(f"c1_replay funding {coin}: {exc}")
            return None
        return [[ts, rate] for ts, rate in points]

    raw = _cache(f"{_safe(coin)}_funding_{stamp}.json", load)
    if raw is None:
        return None
    return [(int(ts), float(rate)) for ts, rate in raw]


def _load_market(days, now_ms):
    cutoff = _cutoff(now_ms, days)
    fetch_from = cutoff - 3 * 24 * paper.HOUR_MS
    stamp = datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
    names, meta = _universe(now_ms)
    series = {}
    funding = {}
    skipped = []
    for i, coin in enumerate(names, start=1):
        try:
            closed = _candles(coin, fetch_from, now_ms, f"{days}d_{stamp}")
        except Exception as exc:
            paper.log(f"c1_replay {coin}: {exc}")
            skipped.append(coin)
            continue
        if closed:
            series[coin] = c1_maker.prepare(closed)
            funding[coin] = _funding(coin, fetch_from, now_ms, f"{days}d_{stamp}")
        if i % 25 == 0 or i == len(names):
            paper.log(f"c1_replay velas {i}/{len(names)}")
    if not series:
        raise RuntimeError("no hay velas de 1h para el repaso")
    latest = max(max(prep["by_t"]) for prep in series.values())
    return {
        "names": names,
        "meta": meta,
        "series": series,
        "funding": funding,
        "skipped": skipped,
        "cutoff": cutoff,
        "latest": latest,
    }


def replay(days=120, now_ms=None):
    """Run variants A, B and C from `days` ago through the latest closed hour."""
    now_ms = int(now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000)
    days = int(days)
    market = _load_market(days, now_ms)
    paper.log(
        f"c1_replay paso {paper.iso(market['cutoff'])} -> {paper.iso(market['latest'])} "
        f"monedas={len(market['series'])}"
    )
    missing_funding = sum(1 for points in market["funding"].values() if points is None)
    reports = []
    for name in ("A", "B", "C"):
        cfg = c1_maker.variant(name)
        book, rows, _events = c1_maker.step(
            {"signals": [], "last_signal_t": {}}, market["series"],
            process_from=market["cutoff"], latest_closed=market["latest"],
            meta=market["meta"], active=market["names"], cfg=cfg, funding=market["funding"],
        )
        stats = c1_maker.summarize_rows(rows, book.get("signals"), days=days)
        stats.update({
            "variant": name,
            "dd24_max": cfg["dd24_max"],
            "sl_atr": cfg["sl_atr"],
            "tp_r": cfg["tp_r"],
            "max_hold": cfg["max_hold"],
            "days": days,
            "coins": len(market["names"]),
            "coins_with_candles": len(market["series"]),
            "coins_skipped": len(market["skipped"]),
            "funding_missing": missing_funding,
            "window_start": paper.iso(market["cutoff"]),
            "window_end": paper.iso(market["latest"]),
        })
        reports.append(stats)
    return reports


def _fmt(value, digits=4):
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Repaso en papel de c1_maker. Sin ordenes.")
    parser.add_argument("--days", type=int, default=120, help="Dias de velas de 1h ya cerradas")
    args = parser.parse_args(argv)
    if args.days < 1:
        print("c1_replay: --days tiene que ser >= 1", file=sys.stderr)
        return 2
    reports = replay(args.days)
    for stats in reports:
        print(
            "c1_replay "
            f"var={stats['variant']} "
            f"dd24={stats['dd24_max']} sl={stats['sl_atr']} tp={stats['tp_r']}R hold={stats['max_hold']} "
            f"senales={stats['signals']} senales_dia={_fmt(stats['signals_per_day'], 3)} "
            f"fill={_fmt(stats['fill_rate'])} "
            f"avg_r={_fmt(stats['avg_r'])} pf={_fmt(stats['pf'])} "
            f"avg_taker_r_unfilled={_fmt(stats['avg_taker_r_unfilled'])} "
            f"trades={stats['trades']} unfilled={stats['unfilled']} "
            f"skipped_cap={stats['skipped_cap']}"
        )
    print("C1_REPLAY_JSON=" + json.dumps(reports, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
