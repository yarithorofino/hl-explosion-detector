#!/usr/bin/env python3
"""Clock-hour replay of confluence_1h.

Live also scores a rolling hour at :15, :30 and :45. This uses closed 1h
candles only (the :00 case). Delta is not in the public candles, so it is
not applied. To stay inside the public rate limit this run keeps the 30
liquid perps with the most 24h notional right now, not the whole book.
"""
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import confluence
import setup_scanner
from detector import load_cfg
from perps_all import post, scan

ROOT = Path(__file__).resolve().parent
CACHE = Path("/tmp/hl_confluence_replay")
MADRID = ZoneInfo("Europe/Madrid")
H_MS = confluence.H_MS
OUTCOME_HOURS = 48
REPORT_DAYS = 14
WARMUP_DAYS = 5
LIMIT = 30
# Taker fee 0.045% plus 0.01% of slippage, each side. Applied as a worse fill.
COST_PER_SIDE = (0.045 + 0.01) / 100.0
# A funding print lands a few milliseconds after the hour. Two minutes would
# still be the same settlement; five seconds cannot reach the next hour.
FUNDING_SLACK_MS = 5_000


def madrid(ms):
    return datetime.fromtimestamp(ms / 1000, MADRID)


def fetch_candles(coin, start_ms, end_ms):
    data = post({"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "1h", "startTime": start_ms, "endTime": end_ms,
    }})
    if not isinstance(data, list):
        raise RuntimeError("velas no son una lista")
    return data


def fetch_funding(coin, start_ms, end_ms):
    rows = []
    cursor = start_ms
    for _ in range(6):
        data = post({"type": "fundingHistory", "coin": coin, "startTime": cursor})
        if not isinstance(data, list) or not data:
            break
        last = 0
        for item in data:
            try:
                when = int(item.get("time"))
                pct = float(item.get("fundingRate")) * 100
            except (TypeError, ValueError):
                continue
            last = max(last, when)
            if start_ms <= when <= end_ms + FUNDING_SLACK_MS:
                premium = item.get("premium")
                try:
                    premium_pct = float(premium) * 100.0
                except (TypeError, ValueError):
                    premium_pct = None
                rows.append((when, pct, premium_pct))
        if last <= cursor or last >= end_ms:
            break
        cursor = last + 1
    rows.sort()
    deduped = []
    for row in rows:
        if deduped and deduped[-1][0] == row[0]:
            deduped[-1] = row
        else:
            deduped.append(row)
    return deduped


def cached(name, loader):
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


def funding_at(series, close_ms):
    """Latest settlement at or just after the bar close. Not the next hour."""
    found = (None, None)
    for row in series:
        when = row[0]
        if when <= close_ms + FUNDING_SLACK_MS:
            found = (row[1], row[2] if len(row) > 2 else None)
        else:
            break
    return found


def upper_wick_pct(candle):
    """Upper wick as a percent of the bar range. None if the bar has no range."""
    try:
        opened, high, low, close = (float(candle[k]) for k in ("o", "h", "l", "c"))
    except (TypeError, ValueError, KeyError):
        return None
    span = high - low
    if span <= 0:
        return None
    return (high - max(opened, close)) / span * 100.0


def net_r(side, entry, exit_price, sl):
    """R after a worse fill on both sides. Risk is the planned entry-to-stop distance."""
    try:
        entry, exit_price, sl = float(entry), float(exit_price), float(sl)
    except (TypeError, ValueError):
        return None
    risk = abs(entry - sl)
    if entry <= 0 or risk <= 0 or side not in ("long", "short"):
        return None
    if side == "long":
        filled_entry = entry * (1 + COST_PER_SIDE)
        filled_exit = exit_price * (1 - COST_PER_SIDE)
        return (filled_exit - filled_entry) / risk
    filled_entry = entry * (1 - COST_PER_SIDE)
    filled_exit = exit_price * (1 + COST_PER_SIDE)
    return (filled_entry - filled_exit) / risk


def short_rule(item):
    """Funding above -0.01%/h is already required to be posted.

    Exhaustion is an upper wick over half the signal hour, funding above
    +0.01%/h, or a premium above +0.1%. Anything else is a plain breakdown.
    """
    reasons = []
    wick = upper_wick_pct(item.get("signal_bar") or {})
    if wick is not None and wick > 50:
        reasons.append("mecha")
    funding = item.get("funding_pct")
    if funding is not None and funding > 0.01:
        reasons.append("funding")
    premium = item.get("premium_pct")
    if premium is not None and premium > 0.1:
        reasons.append("prima")
    if reasons:
        return "agotamiento", reasons
    return "ruptura", []


def day_notional(candles, index):
    if index < 23:
        return None
    total = 0.0
    for candle in candles[index - 23:index + 1]:
        value = setup_scanner.bar_notional(candle)
        if value is None:
            return None
        total += value
    return total


def main():
    cfg = load_cfg()
    now_ms = int(time.time() * 1000)
    report_start = now_ms - REPORT_DAYS * 24 * H_MS
    fetch_start = now_ms - (REPORT_DAYS + WARMUP_DAYS) * 24 * H_MS
    snap = scan()
    liquid = [a for a in snap.get("assets") or [] if confluence.in_universe(a, cfg)]
    liquid.sort(key=lambda a: a.get("dayNtlVlm") or 0, reverse=True)
    universe = liquid[:LIMIT]
    print(f"universo {len(universe)} de {len(liquid)} liquidos / {snap.get('count')} perps", flush=True)
    errors = []
    series = {}
    funding = {}
    for i, asset in enumerate(universe, 1):
        coin = asset["coin"]
        safe = coin.replace(":", "_").replace("/", "_")
        try:
            candles = cached(f"{safe}_1h.json", lambda c=coin: fetch_candles(c, fetch_start, now_ms))
            fund = cached(f"{safe}_funding_v2.json", lambda c=coin: fetch_funding(c, fetch_start, now_ms))
        except Exception as exc:
            errors.append(f"{coin}: {exc}")
            print(f"fallo {coin}: {exc}", flush=True)
            continue
        candles = [c for c in candles if isinstance(c, dict) and c.get("t")]
        candles.sort(key=lambda c: c["t"])
        series[coin] = candles
        funding[coin] = fund
        print(f"velas {i}/{len(universe)} {coin} n={len(candles)}", flush=True)

    events = []
    funnel = defaultdict(int)
    for asset in universe:
        coin = asset["coin"]
        candles = series.get(coin)
        if not candles:
            funnel["sin_velas"] += 1
            continue
        fund = funding.get(coin) or []
        for index, candle in enumerate(candles):
            close_ms = int(candle["t"]) + H_MS
            if close_ms < report_start or close_ms > now_ms:
                continue
            day = day_notional(candles, index)
            if day is None:
                funnel["historia"] += 1
                continue
            view = dict(asset)
            view["dayNtlVlm"] = day
            view["mark"] = candle.get("c")
            funding_pct, premium_pct = funding_at(fund, close_ms)
            view["funding_pct"] = funding_pct
            view["premium_pct"] = premium_pct
            if not confluence.in_universe(view, cfg):
                funnel["libro"] += 1
                continue
            try:
                close = float(candle["c"])
            except (TypeError, ValueError):
                funnel["historia"] += 1
                continue
            scored = confluence.score_bar(
                candles[:index], dict(candle), close, view, None, cfg, require_delta=False,
            )
            funnel[scored.get("status")] += 1
            if scored.get("status") not in ("alerta", "extendido", "veto_funding"):
                continue
            scored.update({
                "coin": coin, "dex": asset.get("dex") or "main", "t": int(candle["t"]),
                "close_ms": close_ms, "index": index, "candles": candles,
                "funding_pct": funding_pct, "premium_pct": premium_pct,
                "signal_bar": {
                    "t": candle.get("t"), "o": candle.get("o"), "h": candle.get("h"),
                    "l": candle.get("l"), "c": candle.get("c"),
                },
            })
            events.append(scored)

    events.sort(key=lambda item: (item["close_ms"], item["coin"]))
    cool_ms = float(cfg["confluence_cooldown_hours"]) * H_MS
    cap = int(cfg["confluence_max_wakes_per_day"])
    fire = {}
    wakes = defaultdict(int)
    posted = []
    quiet_n = pause_n = cap_n = 0
    by_close = defaultdict(list)
    for item in events:
        by_close[item["close_ms"]].append(item)
    for close_ms in sorted(by_close):
        when = madrid(close_ms)
        quiet = cfg["quiet_start_hour"] <= when.hour < cfg["quiet_end_hour"]
        day = when.strftime("%Y-%m-%d")
        batch = []
        for item in by_close[close_ms]:
            if item["status"] != "alerta":
                continue
            seen = fire.get(item["coin"])
            if seen is not None and close_ms - seen < cool_ms:
                pause_n += 1
                continue
            if quiet:
                quiet_n += 1
                continue
            batch.append(item)
        if not batch:
            continue
        if wakes[day] >= cap:
            cap_n += len(batch)
            continue
        wakes[day] += 1
        for item in batch:
            fire[item["coin"]] = close_ms
            item["outcome"] = confluence.walk_outcome(
                item["candles"], item["index"], item["side"],
                item["entry"], item["sl"], item["tp1"], horizon=OUTCOME_HOURS,
            )
            left = len(item["candles"]) - 1 - item["index"]
            item["censored"] = item["outcome"] == "open" and left < OUTCOME_HOURS
            item["day"] = day
            if item["outcome"] == "tp1":
                item["net_r"] = net_r(item["side"], item["entry"], item["tp1"], item["sl"])
            elif item["outcome"] == "sl":
                item["net_r"] = net_r(item["side"], item["entry"], item["sl"], item["sl"])
            else:
                item["net_r"] = None
            if item["side"] == "short":
                kind, reasons = short_rule(item)
                item["short_kind"] = kind
                item["short_reasons"] = reasons
            posted.append(item)

    days = []
    cursor = madrid(report_start).date()
    last = madrid(now_ms).date()
    while cursor <= last:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    by_day = defaultdict(list)
    for item in posted:
        by_day[item["day"]].append(item)
    lines = []
    a = lines.append
    a("# Confluencia 1h, replay de velas cerradas")
    a("")
    a(
        "Cada vela de 1h ya cerrada se puntúa con la regla de las 00: tendencia "
        "(cierre sobre la EMA20 y EMA20 sobre la EMA50), ruptura del Donchian de 20, "
        "volumen al menos 1,5 veces la media de esas 20 horas, stop no más ajustado que "
        "2 ATR y descarte si el cierre ya está a más de 2 ATR del nivel. El corto pide "
        "además funding por encima de −0,01%/h. El cupo es 8 POSTs al día (varias monedas "
        "en la misma hora son un solo POST) y cada moneda espera 4 horas. De 00:00 a 08:00 "
        "(Madrid) no se envía."
    )
    a("")
    a(
        "El delta no está en las velas públicas, así que **aquí no se aplica**. En directo "
        "no hay POST si la cinta no cubrió al menos el 95% de la hora. Estas cifras son un "
        "techo, no los despertares reales."
    )
    a("")
    a(
        f"Para no pasarse del cupo de la API se han usado los **{len(universe)}** perps líquidos "
        f"con más volumen de 24h ahora mismo, de {len(liquid)} que pasan el filtro "
        f"(24h ≥ 5M$ y, en HIP-3, OI ≥ 1M$) sobre {snap.get('count')} perps. "
        f"Con velas: **{len(series)}**. Errores: **{len(errors)}**. "
        f"Ventana (cierre de la vela, Madrid): {madrid(report_start).strftime('%Y-%m-%d %H:%M')} → "
        f"{madrid(now_ms).strftime('%Y-%m-%d %H:%M')}."
    )
    a("")
    a(
        "El volumen de 24h de cada vela es la suma del nocional de esa hora y las 23 anteriores. "
        "El OI de HIP-3 es el de hoy. La salida mira 48 horas; si la misma vela toca stop y TP1, "
        "cuenta como stop. El R neto descuenta 0,045% de comisión y 0,01% de slippage por lado, "
        "como un peor precio de entrada y de salida. No se ha cambiado ningún umbral después de ver el resultado."
    )
    a("")
    a("## Embudo (sin delta)")
    a("")
    a("| Paso | Velas |")
    a("| --- | ---: |")
    for key, label in (
        ("libro", "Libro de 24h por debajo de 5M$ en esa vela"),
        ("historia", "Historia corta o con hueco"),
        ("no", "No es ruptura con tendencia y volumen"),
        ("extendido", "Ya a más de 2 ATR del nivel"),
        ("veto_funding", "Corto con funding en −0,01%/h o sin dato"),
        ("alerta", "Regla de precio, antes de cupo y silencio"),
    ):
        a(f"| {label} | {funnel[key]} |")
    a(f"| En silencio 00:00–08:00 | {quiet_n} |")
    a(f"| En pausa de 4h | {pause_n} |")
    a(f"| Por encima del cupo de 8 POSTs | {cap_n} |")
    a(f"| Se habrían enviado (sin delta) | {len(posted)} |")
    a("")
    n_days = max(1, len(days))
    per_day = len(posted) / n_days
    a(f"**{per_day:.2f} avisos por día** ({len(posted)} en {n_days} días de calendario) en estos {len(universe)} nombres, sin exigir delta.")
    a("")
    a("## Resultado de los que se habrían enviado")
    a("")
    a("| Día | Avisos | TP1 | SL | Abierto | Abierto por falta de horas |")
    a("| --- | ---: | ---: | ---: | ---: | ---: |")
    totals = defaultdict(int)
    for day in days:
        rows = by_day.get(day) or []
        counts = defaultdict(int)
        cens = 0
        for item in rows:
            counts[item["outcome"]] += 1
            if item["censored"]:
                cens += 1
        totals["n"] += len(rows)
        totals["tp1"] += counts["tp1"]
        totals["sl"] += counts["sl"]
        totals["open"] += counts["open"]
        totals["cens"] += cens
        a(f"| {day} | {len(rows)} | {counts['tp1']} | {counts['sl']} | {counts['open']} | {cens} |")
    a(f"| **Total** | **{totals['n']}** | **{totals['tp1']}** | **{totals['sl']}** | **{totals['open']}** | **{totals['cens']}** |")
    a("")
    resolved = totals["tp1"] + totals["sl"]
    if resolved:
        rate = 100.0 * totals["tp1"] / resolved
        a(
            f"De los que ya tocaron un extremo, TP1 ganó en **{totals['tp1']}** y el stop en "
            f"**{totals['sl']}** ({rate:.1f}% a favor del TP1). Abiertos: **{totals['open']}**, "
            f"de ellos **{totals['cens']}** porque aún no hay 48 horas."
        )
        if totals["tp1"] <= totals["sl"]:
            a("")
            a("El resultado es flojo: el stop llega al menos tan a menudo como el primer objetivo.")
    else:
        a("Ningún aviso enviado llegó a tocar TP1 o el stop dentro de los datos.")
    kinds = defaultdict(int)
    sides = defaultdict(int)
    for item in posted:
        kinds[item.get("tp_kind") or "—"] += 1
        sides[item["side"]] += 1
    if posted:
        a("")
        a(
            "Lados: " + ", ".join(f"{name} {sides[name]}" for name in ("long", "short") if sides[name])
            + ". Objetivo: " + ", ".join(f"{name} {kinds[name]}" for name in sorted(kinds)) + "."
        )
    a("")
    a("## Largos y cortos")
    a("")
    a(
        "El R medio es solo de los que ya tocaron TP1 o el stop. "
        "Cada lado paga 0,055% (0,045% de comisión más 0,01% de slippage) "
        "y el riesgo es la distancia prevista entre la entrada y el stop."
    )
    a("")
    a("| Grupo | Avisos | TP1 | SL | Abierto | Resueltos | R neto medio |")
    a("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")

    def _group_line(label, rows):
        counts = defaultdict(int)
        rs = []
        for item in rows:
            counts[item["outcome"]] += 1
            if item.get("net_r") is not None:
                rs.append(item["net_r"])
        resolved_n = counts["tp1"] + counts["sl"]
        avg = sum(rs) / len(rs) if rs else None
        avg_txt = "—" if avg is None else f"{avg:.3f}"
        a(f"| {label} | {len(rows)} | {counts['tp1']} | {counts['sl']} | {counts['open']} | {resolved_n} | {avg_txt} |")
        return counts, avg

    longs = [item for item in posted if item["side"] == "long"]
    shorts = [item for item in posted if item["side"] == "short"]
    plain = [item for item in shorts if item.get("short_kind") == "ruptura"]
    tired = [item for item in shorts if item.get("short_kind") == "agotamiento"]
    _group_line("Largo", longs)
    _group_line("Corto, todos (funding ya > −0,01%/h)", shorts)
    _group_line("Corto de ruptura, sin agotamiento", plain)
    _group_line("Corto de agotamiento", tired)
    a("")
    reason_counts = defaultdict(int)
    missing_premium = 0
    for item in shorts:
        if item.get("premium_pct") is None:
            missing_premium += 1
        for reason in item.get("short_reasons") or []:
            reason_counts[reason] += 1
    a(
        f"De {len(shorts)} cortos enviados, {len(plain)} son ruptura limpia y {len(tired)} "
        f"llevan agotamiento. La mecha superior de la hora de la señal pasa de la mitad del rango "
        f"en {reason_counts['mecha']}, el funding pasa de +0,01%/h en {reason_counts['funding']}, "
        f"y la prima pasa de +0,1% en {reason_counts['prima']}. "
        f"Sin prima en el historial de funding: {missing_premium}."
    )
    a("")
    a(
        "La regla de corto del equipo, además del funding por encima de −0,01%/h que ya se exige "
        "para enviar, deja fuera el agotamiento. Esos cortos de ruptura limpia son los que la pasarían."
    )
    a("")
    a("## Sesgo de mirada al futuro")
    a("")
    a("| Comprobación | Resultado |")
    a("| --- | --- |")
    a("| La entrada es el cierre de la vela de la señal, el precio que existe cuando esa hora cierra. No es el máximo de una vela posterior ni la apertura de la siguiente. | Sin sesgo |")
    a("| El recorrido hacia TP1 y el stop empieza en la vela siguiente. La vela de la señal no se camina. | Sin sesgo |")
    a("| Si una vela posterior toca el stop y el TP1, cuenta como stop. | Sin sesgo |")
    a("| El Donchian, el ATR y el giro del stop salen de las velas que ya habían cerrado antes de abrir la vela de la señal. | Sin sesgo |")
    a("| La EMA20 y la EMA50 dan un paso con el cierre de la señal. Ese cierre ya se conoce en el momento del aviso; no entra ninguna vela posterior. Es la misma cuenta que el detector en directo. | Sin sesgo |")
    a("| El funding y la prima son la liquidación de esa hora (el print cae unos milisegundos después del cierre). No se usa la hora siguiente: la ventana aceptada es de 5 segundos, y el siguiente print es una hora después. | Sin sesgo |")
    a("")
    a("No había una cifra calculada con una vela que todavía no existía. No se ha rehecho el replay por ese motivo.")
    a("")
    a("## Qué no mide esto")
    a("")
    a("- El delta. En directo es obligatorio.")
    a("- Las horas corridas de :15, :30 y :45.")
    a(f"- Los perps líquidos fuera de estos {len(universe)} (el directo sí los mira todos).")
    a("- El interés abierto histórico de HIP-3.")
    a("- El R de los avisos que siguen abiertos. El R medio de arriba solo usa los que ya salieron.")
    if errors:
        a("")
        a("Errores de lectura:")
        for line in errors[:15]:
            a(f"- {line}")
    report = ROOT / "reports" / "confluence_replay_2026-11-11.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"escrito {report} enviados={len(posted)} por_dia={per_day:.2f} "
        f"tp1={totals['tp1']} sl={totals['sl']} abierto={totals['open']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
