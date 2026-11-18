#!/usr/bin/env python3
"""Replay c1_maker over the last 120 days of 1h candles. Paper only.

No exchange keys and no orders. Candles come from the public info API.
The book starts flat. Bars before the window are the 48h pump history and
the ATR only. The live l2Book is not replayed: the info API has no history
of it, so queue fields stay empty in this pass.

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


def replay(days=120, now_ms=None):
    """Run the flat book from `days` ago through the latest closed hour."""
    now_ms = int(now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000)
    days = int(days)
    cutoff = _cutoff(now_ms, days)
    fetch_from = cutoff - 3 * 24 * paper.HOUR_MS
    stamp = datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
    names, meta = _universe(now_ms)
    series = {}
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
        if i % 25 == 0 or i == len(names):
            paper.log(f"c1_replay velas {i}/{len(names)}")
    if not series:
        raise RuntimeError("no hay velas de 1h para el repaso")
    latest = max(max(prep["by_t"]) for prep in series.values())
    paper.log(f"c1_replay paso {paper.iso(cutoff)} -> {paper.iso(latest)} monedas={len(series)}")
    book, rows, _events = c1_maker.step(
        {"signals": []}, series,
        process_from=cutoff, latest_closed=latest,
        meta=meta, active=names,
    )
    stats = c1_maker.summarize_rows(rows, book.get("signals"))
    stats["days"] = days
    stats["coins"] = len(names)
    stats["coins_with_candles"] = len(series)
    stats["coins_skipped"] = len(skipped)
    stats["window_start"] = paper.iso(cutoff)
    stats["window_end"] = paper.iso(latest)
    return stats


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
    stats = replay(args.days)
    print(
        "c1_replay "
        f"dias={stats['days']} monedas={stats['coins_with_candles']}/{stats['coins']} "
        f"senales={stats['signals']} trades={stats['trades']} "
        f"fill_naive={_fmt(stats['fill_rate_naive'])} "
        f"fill_cons={_fmt(stats['fill_rate_conservative'])} "
        f"avg_r_naive={_fmt(stats['avg_r_naive'])} "
        f"avg_r_cons={_fmt(stats['avg_r_conservative'])} "
        f"pf_naive={_fmt(stats['pf_naive'])} "
        f"pf_cons={_fmt(stats['pf_conservative'])} "
        f"unfilled={stats['unfilled']} "
        f"avg_r_hyp={_fmt(stats['avg_r_hypothetical_unfilled'])}"
    )
    print("C1_REPLAY_JSON=" + json.dumps(stats, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
