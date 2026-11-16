#!/usr/bin/env python3
"""Hourly paper run for trend4h, squeeze4h and trapped_shorts1h.

Public info API only. No exchange keys and no orders. One run appends the
market snapshot every hour. The 4h books act when a new 4h candle has
closed. The 1h book acts on each new closed hour, and stays quiet
(warming_up) until the snapshot log covers 7 days.
"""
import os
import sys
from datetime import datetime, timezone

import detector
import paper
import perps_all
import squeeze4h
import trapped_shorts1h
import trend4h


def env_flag(name):
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _latest(series, expected):
    if any(expected in prep["by_t"] for prep in series.values()):
        return expected
    found = []
    for prep in series.values():
        prior = [t for t in prep["by_t"] if t <= expected]
        if prior:
            found.append(max(prior))
    return max(found) if found else None


def _fetch_closed(coins, interval_name, interval_ms, bars, now_ms):
    closed, forming = {}, {}
    start = now_ms - int(bars) * interval_ms
    for coin in coins:
        try:
            raw = paper.fetch_candles(coin, interval_name, start, now_ms)
            norm = paper.normalize_candles(raw, interval_ms)
        except Exception as exc:
            paper.log(f"velas {interval_name} {coin}: {exc}")
            continue
        done, form = paper.split_candles(norm, now_ms, interval_ms)
        if done:
            closed[coin] = done
        if form:
            forming[coin] = form
    return closed, forming


def _funding_for(book, universe, process_from, new_bars, now_ms):
    coins = {p["coin"] for p in book.get("positions") or []}
    coins.update(p["coin"] for p in book.get("pending_entries") or [])
    if new_bars > 1:
        coins.update(universe)
    if not coins:
        return {}
    starts = [p["entry_t"] for p in book.get("positions") or [] if p.get("entry_t")]
    starts.append(process_from or now_ms)
    start = min(starts)
    out = {}
    for coin in sorted(coins):
        try:
            out[coin] = paper.fetch_funding(coin, start, now_ms)
        except Exception:
            paper.log(f"funding {coin}: no se pudo leer; el R de esta pasada no lo incluye")
            out[coin] = None
    return out


def _line(strategy, result, latest):
    closed = sum(1 for r in result["rows"] if r["status"] == "closed")
    skipped = sum(1 for r in result["rows"] if r["status"] == "skipped_cap")
    return (
        f"{strategy} vela={paper.iso(latest)} abiertas={len(result['positions'])} "
        f"cierres={closed} skipped_cap={skipped} eventos={len(result['events'])}"
    )


def run_book(strategy, cfg, series, signal_fn, universe, forming_opens, now_ms, marks):
    """Update one book. Returns events, whether the book file changed, and a log line."""
    book = paper.load_book(strategy)
    interval = int(cfg["interval"])
    expected = paper.latest_closed_open(now_ms, interval)
    latest = _latest(series, expected) if series else None
    boundary = now_ms - (now_ms % interval)
    forming_t = boundary
    last = book.get("last_processed_open_ms")
    pending = (book.get("pending_entries") or []) or (book.get("pending_exits") or [])
    if latest is None and not pending:
        book["last_run"] = paper.iso(now_ms)
        paper.save_book(strategy, book)
        return [], True, f"{strategy}: sin velas cerradas"
    if last is None:
        process_from = latest
        new_bars = 1 if latest is not None else 0
    else:
        process_from = int(last) + interval
        if latest is None or process_from > latest:
            new_bars = 0
        else:
            new_bars = int((latest - process_from) / interval) + 1
    if new_bars <= 0 and not pending:
        return [], False, f"{strategy}: al dia ({paper.iso(latest)})"
    if last is not None and series:
        earliest = min(min(prep["by_t"]) for prep in series.values() if prep["by_t"])
        if process_from < earliest:
            paper.log(
                f"{strategy}: hueco de velas entre {paper.iso(process_from)} y {paper.iso(earliest)}; "
                "solo se revisan las velas que hay"
            )
            process_from = earliest
            if latest is not None and process_from <= latest:
                new_bars = int((latest - process_from) / interval) + 1
    funding = _funding_for(book, universe, process_from, new_bars, now_ms)
    result = paper.simulate(
        series, signal_fn, book, cfg,
        process_from=process_from, latest_closed=latest, forming_t=forming_t,
        forming_opens=forming_opens, marks=marks, universe=universe, funding=funding,
    )
    book["positions"] = result["positions"]
    book["pending_entries"] = result["pending_entries"]
    book["pending_exits"] = result["pending_exits"]
    if new_bars > 0 and latest is not None:
        book["last_processed_open_ms"] = latest
    book["last_run"] = paper.iso(now_ms)
    paper.save_book(strategy, book)
    if result["rows"]:
        paper.write_trades(strategy, result["rows"])
    for row in result["rows"]:
        if row["status"] == "skipped_cap":
            paper.log(f"{strategy} skipped_cap {row['coin']} {row['side']} entrada={row['entry']}")
    line = _line(strategy, result, latest)
    paper.log(line)
    return result["events"], True, line


def _marks(assets):
    out = {}
    for asset in assets:
        if asset.get("dex") != "main":
            continue
        mark = asset.get("mark")
        if mark:
            out[asset["coin"]] = mark
    return out


def _opens_of(forming, coins):
    return {coin: forming[coin]["o"] for coin in coins if coin in forming and forming[coin].get("o")}


def run_trend(closed, forming, assets, listed, delisted, now_ms):
    active = []
    for coin in trend4h.UNIVERSE:
        if coin in delisted:
            paper.log(f"trend4h: {coin} delistado; se omite")
            continue
        if coin not in listed:
            paper.log(f"trend4h: {coin} ausente en metaAndAssetCtxs; se omite")
            continue
        active.append(coin)
    book = paper.load_book("trend4h")
    needed = set(active)
    needed.update(p["coin"] for p in book.get("positions") or [])
    series = {}
    for coin in needed:
        candles = closed.get(coin)
        if not candles:
            if any(p["coin"] == coin for p in book.get("positions") or []):
                paper.log(f"trend4h: {coin} sin velas; la posición abierta sigue en el libro")
            continue
        if coin in active and len(candles) < trend4h.WARMUP:
            paper.log(f"trend4h: {coin} tiene {len(candles)} velas de 4h (<{trend4h.WARMUP}); sin señales")
        series[coin] = trend4h.prepare(candles)
    return run_book(
        "trend4h", trend4h.CFG, series, trend4h.make_signal(series), active,
        _opens_of(forming, needed), now_ms, _marks(assets),
    )


def run_squeeze(closed, forming, assets, now_ms):
    active = paper.liquid_coins(assets)
    book = paper.load_book("squeeze4h")
    needed = set(active)
    needed.update(p["coin"] for p in book.get("positions") or [])
    series = {}
    for coin in needed:
        candles = closed.get(coin)
        if not candles:
            continue
        if coin in active and len(candles) < squeeze4h.BW_LOOKBACK + squeeze4h.BB_PERIOD:
            paper.log(f"squeeze4h: {coin} tiene {len(candles)} velas de 4h; aun sin percentil")
        series[coin] = squeeze4h.prepare(candles)
    return run_book(
        "squeeze4h", squeeze4h.CFG, series, squeeze4h.make_signal(series), active,
        _opens_of(forming, needed), now_ms, _marks(assets),
    )


def run_trapped(closed, forming, assets, now_ms):
    active = paper.liquid_coins(assets)
    warm = paper.spans_warmup(paper.market_log_dir(), trapped_shorts1h.LOOKBACK_H)
    if not warm:
        paper.log("trapped_shorts1h warming_up")
    info = {}
    if warm:
        _first, last = paper.snapshot_span_ms(paper.market_log_dir())
        target = (last or now_ms) - trapped_shorts1h.LOOKBACK_H * paper.HOUR_MS
        by_name = {a["coin"]: a for a in assets if a.get("dex") == "main"}
        for coin in active:
            asset = by_name.get(coin) or {}
            info[coin] = {
                "oi_now": asset.get("oi_coins"),
                "oi_then": paper.oi_near(paper.market_log_dir(), coin, target),
                "premium": asset.get("premium"),
            }
    book = paper.load_book("trapped_shorts1h")
    needed = set(active)
    needed.update(p["coin"] for p in book.get("positions") or [])
    series = {}
    for coin in needed:
        candles = closed.get(coin)
        if candles:
            series[coin] = trapped_shorts1h.prepare(candles)
    expected = paper.latest_closed_open(now_ms, paper.HOUR_MS)
    latest = _latest(series, expected) if series else None
    return run_book(
        "trapped_shorts1h", trapped_shorts1h.CFG, series,
        trapped_shorts1h.make_signal(series, info, latest, warm),
        active, _opens_of(forming, needed), now_ms, _marks(assets),
    )


def _weekly_payload(now_ms):
    books = {}
    for name in paper.BOOKS:
        book = paper.load_book(name)
        books[name] = paper.summarize_book(paper.read_trades(name), book.get("positions") or [], now_ms)
    trend = books["trend4h"]
    return {
        "type": "trend4h_weekly",
        "paper": True,
        "open_positions": trend["open_positions"],
        "closed_trades": trend["closed_trades"],
        "cumulative_r": trend["cumulative_r"],
        "win_rate": trend["win_rate"],
        "books": books,
    }


def _collect_research(assets, snap, now):
    """Order book, listings and liquidation proxy. Never raises into the books."""
    try:
        import market_research
        wrote, notes = market_research.collect(assets, snap, now)
        return bool(wrote), list(notes or [])
    except Exception as exc:
        paper.log(f"research: aviso, los libros no se tocan: {exc}")
        return False, ["- Registros de investigación: fallo, los libros no se han tocado."]


def send_test():
    payload = [{
        "test": True,
        "paper": True,
        "type": "trend4h_test",
        "source": "hl-explosion-detector",
        "message": "POST de prueba trend4h",
    }]
    paper.log("prueba manual: se intenta un POST")
    status = paper.post_webhook(payload)
    paper.log(f"trend4h_test status={status}")
    paper.write_summary([f"- Prueba manual: `{status}`"])
    return 0 if status != "post_failed" else 1


def run(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    paper.log("paper: inicio (sin ordenes, solo info publica)")
    try:
        snap = perps_all.scan()
    except Exception as exc:
        paper.log(f"paper: no se pudo leer Hyperliquid: {exc}")
        paper.write_summary([
            "## Paper",
            "",
            "- No se han podido leer los perps. El estado guardado no se ha modificado.",
        ])
        return False
    assets = snap.get("assets") or []
    if not any(a.get("dex") == "main" for a in assets):
        paper.log("paper: la respuesta no trae el dex principal. No se guarda.")
        paper.write_summary([
            "## Paper",
            "",
            "- No se han podido leer los perps del dex principal. El estado guardado no se ha modificado.",
        ])
        return False
    appended = paper.append_market_log(assets, now)
    try:
        raw = perps_all.post({"type": "metaAndAssetCtxs"})
        listed, delisted = paper.main_listing(raw)
    except Exception:
        paper.log("paper: no se pudo releer meta para distinguir delistados")
        listed = {a["coin"] for a in assets if a.get("dex") == "main"}
        delisted = set()
    changed = bool(appended)
    lines = ["## Paper (sin ordenes)", ""]
    if appended:
        lines.append(f"- Snapshot horario: {len(assets)} perps.")
    else:
        lines.append("- Snapshot horario: esta hora ya estaba anotada.")

    trend_coins = [c for c in trend4h.UNIVERSE if c in listed]
    squeeze_coins = paper.liquid_coins(assets)

    def _needs(name, expected):
        book = paper.load_book(name)
        if book.get("pending_entries") or book.get("pending_exits"):
            return True
        last = book.get("last_processed_open_ms")
        return last is None or int(last) < int(expected)

    events = []
    expected_4h = paper.latest_closed_open(now_ms, paper.FOUR_H_MS)
    if _needs("trend4h", expected_4h) or _needs("squeeze4h", expected_4h):
        coins_4h = set(trend_coins) | set(squeeze_coins)
        for name in ("trend4h", "squeeze4h"):
            coins_4h.update(p["coin"] for p in paper.load_book(name).get("positions") or [])
        closed_4h, forming_4h = _fetch_closed(
            sorted(coins_4h), "4h", paper.FOUR_H_MS, trend4h.FETCH_BARS, now_ms,
        )
        for label, fn in (
            ("trend4h", lambda: run_trend(closed_4h, forming_4h, assets, listed, delisted, now_ms)),
            ("squeeze4h", lambda: run_squeeze(closed_4h, forming_4h, assets, now_ms)),
        ):
            try:
                ev, mutated, line = fn()
            except Exception as exc:
                paper.log(f"{label}: fallo, el libro no se ha sustituido: {exc}")
                lines.append(f"- {label}: fallo, libro sin cambios.")
                continue
            events.extend(ev)
            changed = changed or mutated
            lines.append(f"- {line}")
    else:
        paper.log("trend4h: al dia")
        paper.log("squeeze4h: al dia")
        lines.append("- trend4h: al día.")
        lines.append("- squeeze4h: al día.")

    expected_1h = paper.latest_closed_open(now_ms, paper.HOUR_MS)
    trapped_book = paper.load_book("trapped_shorts1h")
    warm = paper.spans_warmup(paper.market_log_dir(), trapped_shorts1h.LOOKBACK_H)
    trapped_pending = (trapped_book.get("pending_entries") or []) or (trapped_book.get("pending_exits") or [])
    if not warm and not trapped_book.get("positions") and not trapped_pending:
        # No OI history yet, and no position to manage. Remember the hour so
        # the first live hour after warmup does not replay the quiet week.
        paper.log("trapped_shorts1h warming_up")
        trapped_book["last_processed_open_ms"] = expected_1h
        trapped_book["last_run"] = paper.iso(now_ms)
        paper.save_book("trapped_shorts1h", trapped_book)
        changed = True
        lines.append("- trapped_shorts1h warming_up")
    else:
        coins_1h = set(squeeze_coins)
        coins_1h.update(p["coin"] for p in trapped_book.get("positions") or [])
        closed_1h, forming_1h = _fetch_closed(
            sorted(coins_1h), "1h", paper.HOUR_MS, trapped_shorts1h.FETCH_BARS, now_ms,
        )
        try:
            ev, mutated, line = run_trapped(closed_1h, forming_1h, assets, now_ms)
            events.extend(ev)
            changed = changed or mutated
            lines.append(f"- {line}")
        except Exception as exc:
            paper.log(f"trapped_shorts1h: fallo, el libro no se ha sustituido: {exc}")
            lines.append("- trapped_shorts1h: fallo, libro sin cambios.")

    runner = paper.load_runner()
    if paper.weekly_due(now, runner.get("last_weekly_sent")):
        payload = _weekly_payload(now_ms)
        status = paper.post_webhook([payload])
        paper.log(f"trend4h_weekly status={status}")
        lines.append(f"- Resumen semanal: `{status}`.")
        if status != "post_failed":
            runner["last_weekly_sent"] = now.date().isoformat()
            paper.save_runner(runner)
            changed = True
    if events:
        status = paper.post_webhook(events)
        paper.log(f"paper webhook status={status} eventos={len(events)}")
        lines.append(f"- Avisos de entrada o salida: {len(events)} (`{status}`).")
    else:
        lines.append("- Sin entradas ni salidas en esta pasada.")
    research_wrote, research_notes = _collect_research(assets, snap, now)
    lines.extend(research_notes)
    paper.write_summary(lines)
    changed = changed or research_wrote
    if changed:
        bits = []
        if appended:
            bits.append(f"snapshot={len(assets)}")
        bits.append(f"eventos={len(events)}")
        if research_wrote:
            bits.append("research=1")
        paper.mark_ready("paper " + " ".join(bits))
    else:
        paper.log("paper: sin cambios. La rama detector-state no se toca.")
    return True


def main(argv=None):
    ok = False
    try:
        ok = run()
    except Exception as exc:
        paper.log(f"paper: fallo del ciclo, estado NO modificado: {exc}")
        paper.write_summary([
            "## Paper",
            "",
            "- No se ha podido completar el ciclo. El estado guardado no se ha modificado.",
            f"- Detalle: {detector.redact(exc)}",
        ])
        ok = False
    test_rc = 0
    if env_flag("TREND4H_TEST"):
        test_rc = send_test()
    if not ok:
        return 1
    return test_rc


if __name__ == "__main__":
    sys.exit(main())
