#!/usr/bin/env python3
"""Retired. Early-signal shadow rows are no longer written.

The 24 Nov count is data/confluence/ on the detector-state branch.
This script only reads a leftover data/shadow/ folder.

Summarise the shadow log for the 24 Nov review.

Reads data/shadow/YYYY-MM-DD.csv. Counts hits per trigger. When later prices
are available, measures how many hours until a 4% move in the signal's
direction (the same 4% the 15-minute trigger already uses) and counts a hit
as a false alarm when that move does not show up inside 12 hours, or when a
4% move the other way comes first.

    python3 shadow_report.py
    python3 shadow_report.py --fetch
    python3 shadow_report.py --prices prices.json --out reports/shadow_review.md

--fetch reads public candles. It is for the review, not for the 15-minute job.
"""
import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MADRID = ZoneInfo("Europe/Madrid")
ROOT = Path(__file__).resolve().parent
MOVE_PCT = 4.0
WINDOW_HOURS = 12


def _parse_time(text):
    raw = str(text).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(raw, fmt)
            break
        except ValueError:
            parsed = None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MADRID)
    return parsed.timestamp()


def load_hits(directory):
    hits = []
    for path in sorted(Path(directory).glob("*.csv")):
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                ts = _parse_time(row.get("ts_madrid") or "")
                try:
                    price = float(row.get("price") or "")
                except ValueError:
                    price = None
                if ts is None or not price:
                    continue
                hits.append({
                    "ts": ts,
                    "coin": row.get("coin") or "",
                    "side": row.get("side") or "",
                    "trigger": row.get("trigger") or row.get("components") or "",
                    "price": price,
                })
    hits.sort(key=lambda item: item["ts"])
    return hits


def classify(hit, prices, move_pct=MOVE_PCT, window_hours=WINDOW_HOURS):
    """Return lead hours, or None when the path never pays the signal."""
    series = prices.get(hit["coin"]) or []
    end = hit["ts"] + window_hours * 3600
    side = 1 if hit["side"] == "long" else -1 if hit["side"] == "short" else 0
    if not side:
        return None
    for ts, price in series:
        if ts <= hit["ts"] or ts > end or not price:
            continue
        change = (price / hit["price"] - 1) * 100 * side
        if change >= move_pct:
            return (ts - hit["ts"]) / 3600
        if change <= -move_pct:
            return None
    return None


def summarise(hits, prices=None, move_pct=MOVE_PCT, window_hours=WINDOW_HOURS):
    prices = prices or {}
    per = defaultdict(lambda: {"hits": 0, "leads": [], "false": 0, "no_path": 0, "no_side": 0})
    have_prices = bool(prices)
    for hit in hits:
        names = [n for n in (hit.get("trigger") or "").split("+") if n]
        if not names:
            names = ["(sin nombre)"]
        outcome = None
        judged = have_prices and hit["coin"] in prices
        if judged and hit.get("side") in ("long", "short"):
            outcome = classify(hit, prices, move_pct, window_hours)
        for name in names:
            bucket = per[name]
            bucket["hits"] += 1
            if not judged:
                if hit.get("side") not in ("long", "short"):
                    bucket["no_side"] += 1
                else:
                    bucket["no_path"] += 1
            elif outcome is None:
                bucket["false"] += 1
            else:
                bucket["leads"].append(outcome)
    return per


def render(per, priced):
    lines = [
        "# Repaso de la sombra",
        "",
        "Una señal cuenta como acierto si el precio se mueve al menos un 4% a favor "
        f"dentro de {WINDOW_HOURS} horas, antes de un 4% en contra. El 4% es el mismo "
        "suelo que ya usa el disparo de 15 minutos. Sin precios posteriores solo se pueden contar los avisos.",
        "",
        "| Disparo | Avisos | Con recorrido | Falsos | Mediana de adelanto (h) | Sin precios |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    if not per:
        lines.append("| — | 0 | 0 | 0 | — | 0 |")
    for name in sorted(per):
        bucket = per[name]
        leads = sorted(bucket["leads"])
        if leads:
            mid = leads[len(leads) // 2]
            mid_s = f"{mid:.1f}"
        else:
            mid_s = "—"
        lines.append(
            f"| {name} | {bucket['hits']} | {len(leads)} | {bucket['false']} | {mid_s} | {bucket['no_path']} |"
        )
    lines.append("")
    if not priced:
        lines.append("No había precios posteriores. Vuelve a lanzarlo con `--fetch` el 24 nov para el adelanto y los falsos.")
        lines.append("")
    return "\n".join(lines)


def fetch_prices(hits):
    from perps_all import post

    prices = defaultdict(list)
    if not hits:
        return prices
    start = int((min(h["ts"] for h in hits) - 3600) * 1000)
    end = int((max(h["ts"] for h in hits) + WINDOW_HOURS * 3600) * 1000)
    for coin in sorted({h["coin"] for h in hits}):
        data = post({
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": "15m", "startTime": start, "endTime": end},
        })
        if not isinstance(data, list):
            continue
        for candle in data:
            try:
                prices[coin].append((candle["t"] / 1000, float(candle["c"])))
            except (KeyError, TypeError, ValueError):
                continue
        prices[coin].sort()
    return prices


def main(argv=None):
    parser = argparse.ArgumentParser(description="Resume el registro de señales en sombra.")
    parser.add_argument("--dir", default=str(ROOT / "data" / "shadow"))
    parser.add_argument("--prices", help="JSON {moneda: [[epoch, precio], ...]}")
    parser.add_argument("--fetch", action="store_true", help="Pide velas públicas para medir el adelanto.")
    parser.add_argument("--out", help="Escribe el markdown aquí además de imprimirlo.")
    args = parser.parse_args(argv)
    hits = load_hits(args.dir)
    prices = {}
    if args.prices:
        raw = json.loads(Path(args.prices).read_text(encoding="utf-8"))
        prices = {coin: [(float(ts), float(px)) for ts, px in series] for coin, series in raw.items()}
    elif args.fetch:
        prices = fetch_prices(hits)
    per = summarise(hits, prices)
    text = render(per, priced=bool(prices))
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
