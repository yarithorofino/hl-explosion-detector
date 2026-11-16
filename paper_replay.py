#!/usr/bin/env python3
"""Replay trend4h and squeeze4h over the last 120 days. Paper only.

trapped_shorts1h is not replayed: its open interest comes only from the
hourly snapshot log, and that log is not backfilled.

The books start flat at the beginning of the window. Candles before the
window are indicator history only. Prints realized R after fees and funding.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import paper
import perps_all
import squeeze4h
import trend4h

REPLAY_DAYS = 120
CACHE = Path("/tmp/hl_paper_replay")


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


def _window(now_ms):
    cutoff = now_ms - REPLAY_DAYS * 24 * paper.HOUR_MS
    extra = cutoff % paper.FOUR_H_MS
    if extra:
        cutoff += paper.FOUR_H_MS - extra
    return cutoff


def _load_candles(coin, now_ms, start_ms):
    def load():
        return paper.fetch_candles(coin, "4h", start_ms, now_ms)

    raw = _cache(f"{coin}_4h.json", load)
    norm = paper.normalize_candles(raw, paper.FOUR_H_MS)
    return paper.split_candles(norm, now_ms, paper.FOUR_H_MS)


def _reprice(rows, funding):
    for row in rows:
        if row.get("status") != "closed":
            continue
        try:
            pos = {
                "side": row["side"],
                "entry": float(row["entry"]),
                "size": float(row["size_coins"]),
                "entry_t": paper.parse_iso(row["entry_time"]),
                "risk_usd": float(row["risk_usd"] or paper.RISK_USD),
            }
            exit_px = float(row["exit"])
            exit_t = paper.parse_iso(row["exit_time"])
        except (TypeError, ValueError):
            continue
        points = funding.get(row["coin"])
        acct = paper.account(pos, exit_px, exit_t, points if points is not None else None)
        row["gross_pnl"] = paper.csv_num(acct["gross_pnl"])
        row["fees"] = paper.csv_num(acct["fees"])
        row["funding_pnl"] = paper.csv_num(acct["funding_pnl"]) if acct["funding_ok"] else ""
        row["net_pnl"] = paper.csv_num(acct["net_pnl"])
        row["r"] = paper.csv_num(acct["r"])
        row["funding_ok"] = "1" if acct["funding_ok"] else "0"


def _stats(rows):
    closed = [r for r in rows if r.get("status") == "closed"]
    values = []
    reasons = {}
    for row in closed:
        try:
            values.append(float(row["r"]))
        except (TypeError, ValueError):
            continue
        reasons[row.get("exit_reason") or "?"] = reasons.get(row.get("exit_reason") or "?", 0) + 1
    skipped = sum(1 for r in rows if r.get("status") == "skipped_cap")
    wins = sum(1 for v in values if v > 0)
    return {
        "n": len(closed),
        "with_r": len(values),
        "sum_r": sum(values),
        "win_rate": (wins / len(values)) if values else None,
        "reasons": reasons,
        "skipped": skipped,
        "avg_r": (sum(values) / len(values)) if values else None,
    }


def _fmt(n, digits=3):
    if n is None:
        return "-"
    return f"{n:.{digits}f}"


def _unrealized(pos, candles, points, now_ms):
    if not candles:
        return None
    last = candles[-1]
    px = last["c"]
    exit_t = min(now_ms, last["t"] + paper.FOUR_H_MS)
    acct = paper.account(pos, px, exit_t, points)
    return px, acct


def _section(name, rows, positions, series, funding, now_ms):
    stats = _stats(rows)
    lines = [
        f"## {name}",
        "",
        f"- Cierres: {stats['n']}",
        f"- R acumulada: {_fmt(stats['sum_r'])}",
        f"- R media: {_fmt(stats['avg_r'])}",
        "- Acierto (R>0): "
        + ("-" if stats["win_rate"] is None else f"{stats['win_rate'] * 100:.1f}%"),
        f"- Por salida: {stats['reasons'] or '-'}",
        f"- skipped_cap: {stats['skipped']}",
        f"- Abiertas al corte: {len(positions)}",
        "",
        "coin | side | entrada | salida | entry | exit | R | motivo",
        "--- | --- | --- | --- | --- | --- | --- | ---",
    ]
    ordered = sorted(
        (r for r in rows if r.get("status") == "closed"),
        key=lambda r: (r.get("entry_time") or "", r.get("coin") or ""),
    )
    for row in ordered:
        lines.append(
            " | ".join([
                row.get("coin") or "",
                row.get("side") or "",
                row.get("entry_time") or "",
                row.get("exit_time") or "",
                row.get("entry") or "",
                row.get("exit") or "",
                row.get("r") or "",
                row.get("exit_reason") or "",
            ])
        )
    if positions:
        lines.append("")
        lines.append("Abiertas (R no realizada, al último cierre, con fees y funding hasta ahora):")
        for pos in positions:
            coin = pos["coin"]
            mark = _unrealized(pos, (series.get(coin) or {}).get("candles"), funding.get(coin), now_ms)
            if not mark:
                lines.append(f"- {coin} {pos['side']} entrada {pos['entry']} stop {pos['stop']}")
                continue
            px, acct = mark
            lines.append(
                f"- {coin} {pos['side']} desde {paper.iso(pos['entry_t'])} "
                f"entry {paper.csv_num(pos['entry'])} ahora {paper.csv_num(px)} "
                f"stop {paper.csv_num(pos['stop'])} R {_fmt(acct['r'])}"
            )
    lines.append("")
    return lines, stats


def main():
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    process_from = _window(now_ms)
    fetch_start = process_from - trend4h.FETCH_BARS * paper.FOUR_H_MS
    print(f"replay ventana desde {paper.iso(process_from)} hasta {paper.iso(now_ms)}", flush=True)

    snap = perps_all.scan()
    raw = perps_all.post({"type": "metaAndAssetCtxs"})
    listed, delisted = paper.main_listing(raw)
    missing = [c for c in trend4h.UNIVERSE if c not in listed and c not in delisted]
    trend_names = [c for c in trend4h.UNIVERSE if c in listed]
    squeeze_names = paper.liquid_coins(snap.get("assets") or [])
    coins = sorted(set(trend_names) | set(squeeze_names))

    closed = {}
    forming = {}
    for coin in coins:
        try:
            done, form = _load_candles(coin, now_ms, fetch_start)
        except Exception as exc:
            print(f"velas {coin}: {exc}", flush=True)
            continue
        if done:
            closed[coin] = done
        if form:
            forming[coin] = form["o"]
        print(f"velas {coin}: {len(done)} cerradas", flush=True)

    trend_series = {c: trend4h.prepare(closed[c]) for c in trend_names if c in closed}
    squeeze_series = {c: squeeze4h.prepare(closed[c]) for c in squeeze_names if c in closed}
    empty_trend = paper.empty_book("trend4h")
    empty_squeeze = paper.empty_book("squeeze4h")
    latest = paper.latest_closed_open(now_ms, paper.FOUR_H_MS)
    forming_t = now_ms - (now_ms % paper.FOUR_H_MS)
    trend_result = paper.simulate(
        trend_series, trend4h.make_signal(trend_series), empty_trend, trend4h.CFG,
        process_from=process_from, latest_closed=latest, forming_t=forming_t,
        forming_opens=forming, marks=None,
        universe=[c for c in trend_names if c in trend_series], funding={},
    )
    squeeze_result = paper.simulate(
        squeeze_series, squeeze4h.make_signal(squeeze_series), empty_squeeze, squeeze4h.CFG,
        process_from=process_from, latest_closed=latest, forming_t=forming_t,
        forming_opens=forming, marks=None,
        universe=[c for c in squeeze_names if c in squeeze_series], funding={},
    )

    traded = set()
    for result in (trend_result, squeeze_result):
        for row in result["rows"]:
            if row.get("status") == "closed":
                traded.add(row["coin"])
        for pos in result["positions"]:
            traded.add(pos["coin"])
    funding = {}
    fund_start = process_from - paper.HOUR_MS
    for coin in sorted(traded):
        print(f"funding {coin}", flush=True)
        try:
            funding[coin] = _cache(
                f"{coin}_funding.json",
                lambda c=coin: paper.fetch_funding(c, fund_start, now_ms),
            )
        except Exception as exc:
            print(f"funding {coin}: {exc}", flush=True)
            funding[coin] = None
    _reprice(trend_result["rows"], funding)
    _reprice(squeeze_result["rows"], funding)

    short_hist = [c for c in trend_names if c in closed and len(closed[c]) < trend4h.WARMUP]
    lines = [
        "# Replay paper, 120 días",
        "",
        f"- Corte: {paper.iso(now_ms)}",
        f"- Ventana (velas de 4h desde): {paper.iso(process_from)}",
        f"- Los libros empiezan planos. Lo anterior solo calienta los indicadores.",
        f"- Universo trend4h: {', '.join(trend_names)}",
        f"- Delistados u omitidos: {', '.join(sorted(delisted & set(trend4h.UNIVERSE)) or []) or '-'}",
        f"- Ausentes: {', '.join(missing) or '-'}",
        f"- Menos de {trend4h.WARMUP} velas: {', '.join(short_hist) or '-'}",
        f"- Universo squeeze4h ahora (main, dayNtlVlm >= 5M): {len(squeeze_series)} monedas",
        f"- {', '.join(c for c in squeeze_names if c in squeeze_series)}",
        "",
        "El R incluye 0,07% por lado y el funding horario sobre el nocional de entrada.",
        "squeeze4h usa el universo líquido de hoy, no el de cada vela del pasado.",
        "",
    ]
    t_lines, t_stats = _section(
        "trend4h", trend_result["rows"], trend_result["positions"], trend_series, funding, now_ms,
    )
    s_lines, s_stats = _section(
        "squeeze4h", squeeze_result["rows"], squeeze_result["positions"], squeeze_series, funding, now_ms,
    )
    lines.extend(t_lines)
    lines.extend(s_lines)
    text = "\n".join(lines).rstrip() + "\n"
    print(text)
    out = CACHE / "report.md"
    out.write_text(text, encoding="utf-8")
    print(f"informe {out}", flush=True)
    print(
        "RESUMEN "
        f"trend cierres={t_stats['n']} R={_fmt(t_stats['sum_r'])} "
        f"acierto={_fmt(None if t_stats['win_rate'] is None else t_stats['win_rate'] * 100, 1)}% "
        f"squeeze cierres={s_stats['n']} R={_fmt(s_stats['sum_r'])} "
        f"acierto={_fmt(None if s_stats['win_rate'] is None else s_stats['win_rate'] * 100, 1)}%",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
