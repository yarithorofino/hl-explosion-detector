#!/usr/bin/env python3
"""Replay early signals and phase-2 watches on public candles.

CVD, hourly open interest and intraday funding are not in candleSnapshot.
This script does not pretend they are. Absorption, relative volume and the
squeeze are computed from the candles. Times are candle opens in Europe/Madrid.
"""
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import detector
import early_signals
import watch as watchmod
from perps_all import post

MADRID = ZoneInfo("Europe/Madrid")
ROOT = Path(__file__).resolve().parent
CACHE = Path("/tmp/hl-replay")
OUT = ROOT / "reports" / "replay_2026-11-10.md"
# coin, first day to search, last day to search, day whose swing is "the move"
CASES = [
    ("INIT", "2026-11-08", "2026-11-10", "2026-11-09"),
    ("para:ANSEM", "2026-11-07", "2026-11-09", "2026-11-08"),
    ("xyz:ANT", "2026-11-09", "2026-11-10", "2026-11-10"),
    ("VVV", "2026-11-09", "2026-11-10", "2026-11-10"),
    ("SOPH", "2026-11-09", "2026-11-10", "2026-11-10"),
    ("kPEPE", "2026-11-09", "2026-11-10", "2026-11-10"),
]
COOL_MS = 4 * 3600 * 1000
IMPULSE = {"1h_atr", "1h_legacy", "15m_early", "3h_escalera", "squeeze_breakout"}


def ntl(c):
    return float(c["v"]) * (float(c["h"]) + float(c["l"]) + float(c["c"])) / 3


def madrid(ms):
    return datetime.fromtimestamp(ms / 1000, MADRID)


def stamp(c):
    return madrid(c["t"]).strftime("%Y-%m-%d %H:%M")


def fetch_candles(coin, interval, start_ms, end_ms):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{coin.replace(':', '_')}_{interval}.json"
    if path.exists():
        return json.loads(path.read_text())
    data = post({
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms},
    })
    if not isinstance(data, list):
        data = []
    path.write_text(json.dumps(data))
    return data


def universe():
    meta, ctxs = post({"type": "metaAndAssetCtxs"})
    names = []
    for u, c in zip(meta["universe"], ctxs):
        if u.get("isDelisted"):
            continue
        try:
            day = float(c.get("dayNtlVlm") or 0)
        except (TypeError, ValueError):
            day = 0
        if day >= 1_000_000:
            names.append(u["name"])
    return sorted(set(names) | {c[0] for c in CASES})


def prefix(cs):
    out = [0.0]
    for c in cs:
        out.append(out[-1] + ntl(c))
    return out


def rolling_day(pref, i, n=24):
    start = max(0, i + 1 - n)
    return pref[i + 1] - pref[start]


def _quiet(when):
    return int(when[11:13]) < 8


def score_hour(cs, i, cfg):
    """Same weights as the live scorer. No CVD, no open interest, no funding."""
    parts = {}
    side = None
    detail = None
    if i >= int(cfg["absorption_median_bars"]):
        absorbed = early_signals.absorption(cs, i, cfg)
        if absorbed.get("hit"):
            detail = absorbed
            parts["absorption"] = True
            if absorbed.get("repeated"):
                parts["absorption_repeat"] = True
            side = absorbed.get("side")
    if "absorption" not in parts and i >= int(cfg["vol_accel_avg_bars"]):
        got = early_signals.vol_accel(cs, i, cfg, cfg["vol_accel_min_notional_1h"])
        if got.get("hit"):
            parts["vol_accel"] = True
    if i >= int(cfg["squeeze_bb_period"]) + int(cfg["squeeze_min_bars_inside"]):
        sq = early_signals.squeeze_breakout(cs, i, cfg)
        if sq and sq.get("hit"):
            parts["squeeze"] = True
            if sq.get("side"):
                side = sq.get("side")
    scored = early_signals.confluence(parts, cfg, side)
    scored["absorption"] = detail
    return scored


def move_on(candles, day):
    bars = [c for c in candles if madrid(c["t"]).strftime("%Y-%m-%d") == day]
    if len(bars) < 4:
        return None
    lo = min(bars, key=lambda c: float(c["l"]))
    hi = max(bars, key=lambda c: float(c["h"]))
    if hi["t"] >= lo["t"]:
        return {"side": "long", "start_t": lo["t"], "end_t": hi["t"],
                "start": float(lo["l"]), "end": float(hi["h"])}
    return {"side": "short", "start_t": hi["t"], "end_t": lo["t"],
            "start": float(hi["h"]), "end": float(lo["l"])}


def pct_done(move, price):
    span = move["end"] - move["start"]
    if span == 0:
        return None
    return (price - move["start"]) / span * 100


def lead_hours(signal_t, move):
    if not signal_t or not move:
        return None
    return (move["start_t"] - signal_t) / 3_600_000


def in_window(when, start, end):
    day = when[:10]
    return start <= day <= end


def collect(coin, cs_1h, cs_15m, cfg):
    floor = 3.0 if coin in {"BTC", "ETH", "SOL"} else 5.0
    events = []
    classic = detector.replay_candles(cs_1h, cs_15m, cfg, floor)
    h_index = {stamp(c): c for c in cs_1h}
    m_index = {stamp(c): c for c in cs_15m}
    for e in classic:
        src = h_index if e["interval"] == "1h" else m_index
        bar = src.get(e["madrid"])
        impulse = []
        if bar and e["interval"] == "1h":
            i = cs_1h.index(bar)
            impulse = cs_1h[max(0, i - 2):i + 1]
        elif bar:
            i = cs_15m.index(bar)
            impulse = cs_15m[max(0, i - 2):i + 1]
        events.append({
            **e,
            "family": "existing",
            "quiet": _quiet(e["madrid"]),
            "t": bar["t"] if bar else None,
            "impulse": impulse,
            "side": "long" if e["pct"] >= 0 else "short",
            "coin": coin,
            "kind": "retest",
        })
    pref = prefix(cs_1h)
    early = []
    spikes = []
    min_day = cfg["early_min_day_ntl_usd"]
    for i, c in enumerate(cs_1h):
        got = score_hour(cs_1h, i, cfg)
        detail = got.get("absorption")
        liquid = rolling_day(pref, i) >= min_day
        when = stamp(c)
        if detail and detail.get("hit"):
            spikes.append({
                "madrid": when, "t": c["t"], "close": float(c["c"]),
                "rvol": detail.get("rvol"), "repeats": detail.get("repeats"),
                "side": detail.get("side"), "repeated": detail.get("repeated"),
                "return_pct": detail.get("return_pct"), "range_pct": detail.get("range_pct"),
                "range_low": detail.get("range_low"), "range_high": detail.get("range_high"),
                "liquid": liquid, "coin": coin, "quiet": _quiet(when),
            })
        if not liquid or not got["hit"]:
            continue
        kind = "absorption" if "absorption" in got["triggers"] and not (set(got["triggers"]) & IMPULSE) else "retest"
        early.append({
            "interval": "1h", "madrid": when, "t": c["t"], "close": float(c["c"]),
            "open": float(c["o"]), "pct": round((float(c["c"]) / float(c["o"]) - 1) * 100, 2),
            "triggers": got["triggers"], "side": got["side"], "family": "early",
            "quiet": _quiet(when), "impulse": cs_1h[max(0, i - 2):i + 1], "coin": coin,
            "score": got["score"], "kind": kind,
            "range_low": (detail or {}).get("range_low"),
            "range_high": (detail or {}).get("range_high"),
            "repeats": (detail or {}).get("repeats"),
            "rvol": (detail or {}).get("rvol"),
        })
    return events, early, spikes


def _describe_watch_result(result):
    status = result.get("status")
    if status == "entry_ready":
        text = f"entrada {result['at']} rr {result['rr']} → {result['outcome']}"
        if result.get("quiet_break"):
            text = f"rotura en silencio {result['quiet_break']}; publicada {text}"
        return text
    if status == "quiet_only":
        return f"rotura en silencio {result.get('at')}, la vigilancia caducó antes de poder publicarla"
    text = status or "—"
    if result.get("at"):
        text += f" {result['at']}"
    return text


def phase2(cs_15m, signal, cfg):
    """Walk 15m bars after one signal. Quiet hours hold the POST, they do not consume the watch."""
    if not signal or not signal.get("t"):
        return {"status": "sin aviso"}
    if signal.get("kind") == "absorption":
        watch = watchmod.build_absorption_watch(
            signal["coin"], signal.get("dex") or "main", signal["side"],
            "+".join(signal["triggers"]), signal.get("range_low"), signal.get("range_high"),
            signal["t"] / 1000, cfg,
        )
    else:
        watch = watchmod.build_watch(
            signal["coin"], signal.get("dex") or "main", signal["side"],
            "+".join(signal["triggers"]), signal.get("impulse") or [], signal["t"] / 1000, cfg,
        )
    if not watch:
        return {"status": "no_watch"}
    return _walk(cs_15m, watch, cfg)


def phase2_absorption_campaign(cs_15m, signals, cfg):
    """Refresh the range and the clock on each later spike, as the live scan does."""
    usable = [s for s in signals if s.get("t") and s.get("range_low") is not None and s.get("side")]
    usable.sort(key=lambda s: s["t"])
    if not usable:
        return {"status": "sin aviso"}
    watch = None
    quiet_break = None
    cursor = 0
    bars = [c for c in cs_15m if c["t"] >= usable[0]["t"]]
    for c in bars:
        while cursor < len(usable) and usable[cursor]["t"] <= c["t"]:
            sig = usable[cursor]
            cursor += 1
            built = watchmod.build_absorption_watch(
                sig["coin"], "main", sig["side"], "+".join(sig["triggers"]),
                sig.get("range_low"), sig.get("range_high"), sig["t"] / 1000, cfg,
            )
            if not built:
                continue
            if watch and watch.get("status") == "open" and watch.get("side") == built["side"]:
                built["created_at"] = watch.get("created_at")
                watch = built
            elif watch is None or watch.get("status") != "open":
                watch = built
        if watch is None or watch.get("status") != "open":
            continue
        # Do not treat the spike hour itself as the breakout.
        if c["t"] <= usable[min(cursor, len(usable)) - 1]["t"]:
            continue
        allow = madrid(c["t"]).hour >= 8
        watch, event = watchmod.advance_watch(
            watch, float(c["h"]), float(c["l"]), float(c["c"]), c["t"] / 1000, cfg, allow_entry=allow,
        )
        if event == "quiet" and quiet_break is None:
            quiet_break = madrid(c["t"]).strftime("%Y-%m-%d %H:%M")
        if event == "invalidated":
            return {"status": "invalidated", "at": madrid(c["t"]).strftime("%Y-%m-%d %H:%M")}
        if event == "expired":
            if quiet_break:
                return {"status": "quiet_only", "at": quiet_break}
            return {"status": "expired", "at": madrid(c["t"]).strftime("%Y-%m-%d %H:%M")}
        if event == "entry_ready":
            later = [x for x in cs_15m if x["t"] > c["t"] and x["t"] / 1000 <= watch["expires_at"]]
            outcome = watchmod.outcome_after_entry(watch, later)
            return {
                "status": "entry_ready",
                "at": madrid(c["t"]).strftime("%Y-%m-%d %H:%M"),
                "entry": watch["fill"], "sl": watch["sl"], "tp1": watch["tp1"], "tp2": watch["tp2"],
                "rr": round(watch["rr"], 2), "outcome": outcome, "quiet_break": quiet_break,
            }
    if watch and watch.get("status") == "open":
        return {"status": "open_at_end"}
    return {"status": "sin rotura"}


def _walk(cs_15m, watch, cfg):
    bars = [c for c in cs_15m if c["t"] / 1000 > watch["created_at"]]
    quiet_break = None
    for c in bars:
        if c["t"] / 1000 > watch["expires_at"] and watch.get("status") == "open":
            if quiet_break:
                return {"status": "quiet_only", "at": quiet_break}
            return {"status": "expired", "at": madrid(c["t"]).strftime("%Y-%m-%d %H:%M")}
        allow = madrid(c["t"]).hour >= 8
        watch, event = watchmod.advance_watch(
            watch, float(c["h"]), float(c["l"]), float(c["c"]), c["t"] / 1000, cfg, allow_entry=allow,
        )
        if event == "quiet" and quiet_break is None:
            quiet_break = madrid(c["t"]).strftime("%Y-%m-%d %H:%M")
        if event == "invalidated":
            return {"status": "invalidated", "at": madrid(c["t"]).strftime("%Y-%m-%d %H:%M")}
        if event == "expired":
            if quiet_break:
                return {"status": "quiet_only", "at": quiet_break}
            return {"status": "expired", "at": madrid(c["t"]).strftime("%Y-%m-%d %H:%M")}
        if event == "entry_ready":
            later = [x for x in bars if x["t"] > c["t"] and x["t"] / 1000 <= watch["expires_at"]]
            outcome = watchmod.outcome_after_entry(watch, later)
            return {
                "status": "entry_ready",
                "at": madrid(c["t"]).strftime("%Y-%m-%d %H:%M"),
                "rr": round(watch["rr"], 2), "outcome": outcome, "quiet_break": quiet_break,
            }
    if quiet_break:
        return {"status": "quiet_only", "at": quiet_break}
    return {"status": "open_at_end" if watch.get("status") == "open" else "sin rotura"}


def day_counts(events):
    per = defaultdict(lambda: defaultdict(int))
    for e in events:
        if e.get("quiet"):
            continue
        day = e["madrid"][:10]
        per[day]["__n"] += 1
        for name in e["triggers"]:
            per[day][name] += 1
    return per


def cool(events):
    """One explosive alert per coin per 4h, which is how the live cooldown works."""
    last = {}
    kept = []
    for e in sorted(events, key=lambda item: item.get("t") or 0):
        if e.get("quiet") or not e.get("t"):
            continue
        prev = last.get(e["coin"])
        if prev is not None and e["t"] - prev < COOL_MS:
            continue
        last[e["coin"]] = e["t"]
        kept.append(e)
    return kept


def fmt_event(e, move):
    if not e:
        return "—"
    done = pct_done(move, e["close"]) if move and e.get("close") is not None else None
    lead = lead_hours(e.get("t"), move)
    bits = "+".join(e.get("triggers") or ["absorption"])
    extra = ""
    if e.get("rvol"):
        extra += f", {e['rvol']:.0f}×"
    if e.get("repeats"):
        n = int(e["repeats"])
        extra += f", {n} vela" if n == 1 else f", {n} velas"
    if isinstance(lead, float) and lead >= 0:
        extra += f", {lead:.1f}h antes del inicio"
    elif isinstance(done, float):
        extra += f", {done:.0f}% del recorrido"
        if isinstance(lead, float):
            extra += f", {abs(lead):.1f}h tarde"
    if e.get("quiet"):
        extra += ", silencio"
    if e.get("liquid") is False:
        extra += ", libro < 1M$, no avisa"
    return f"{e['madrid']} ({bits}{extra})"


def soph_rows(cs, cfg):
    """The owner's 9 Nov hours, plus any other >=3× bar that day and the next morning."""
    lines = []
    pref = prefix(cs)
    for i, c in enumerate(cs):
        when = stamp(c)
        if when[:10] not in ("2026-11-09", "2026-11-10"):
            continue
        hour = int(when[11:13])
        bar = early_signals._absorption_bar(cs, i, cfg)
        # Range or the average gate can reject a bar before both multiples are stored.
        period = int(cfg["absorption_median_bars"])
        rvol = bar.get("rvol")
        rvol_mean = bar.get("rvol_mean")
        if i >= period and (rvol is None or rvol_mean is None):
            prior = [early_signals.bar_notional(cs[k]) for k in range(i - period, i)]
            prior = [p for p in prior if p is not None]
            cur = early_signals.bar_notional(c)
            if prior and cur is not None:
                base = early_signals.median(prior)
                avg = sum(prior) / len(prior)
                if base:
                    rvol = cur / base
                if avg:
                    rvol_mean = cur / avg
        notable = hour in (11, 12, 16, 22, 3) or (rvol or 0) >= 8 or bar.get("hit")
        if not notable or (when[:10] == "2026-11-10" and hour > 8 and hour not in (11, 12)):
            continue
        o, h, l, cl = float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"])
        ab = early_signals.absorption(cs, i, cfg) if bar.get("hit") else {}
        lines.append({
            "when": when,
            "ntl": ntl(c),
            "roll": rolling_day(pref, i),
            "ret": abs(cl / o - 1) * 100,
            "rng": (h - l) / o * 100,
            "rvol": rvol,
            "rvol_mean": rvol_mean,
            "hit": bool(bar.get("hit")),
            "repeats": ab.get("repeats") or 0,
            "side": ab.get("side") or "—",
            "clv": early_signals.close_location(c),
        })
    return lines


def write_report(cases, raw_counts, cool_counts, n_universe, cfg, soph):
    lines = []
    a = lines.append
    a("# Repetición 3–10 nov 2026")
    a("")
    a("Horas en Europe/Madrid, en la apertura de la vela. La pasada en directo ve esa vela al cerrarse, hasta unos 75 minutos después. Esto es un estudio, no una promesa de resultados.")
    a("")
    a("## Qué se pudo repetir y qué no")
    a("")
    a("- Velas de 1h y de 15m: sí, `candleSnapshot` (incluye nombres HIP-3 como `xyz:ANT` y `para:ANSEM`).")
    a("- Volumen relativo, absorción (esfuerzo contra resultado) y compresión de Bollinger/Keltner: sí, salen de esas velas.")
    a("- Interés abierto hora a hora: **no**. La API pública no guarda el OI histórico. El archivo `s3://hyperliquid-archive/asset_ctxs/` trae un contexto **por día** y es de pago por descarga (requester-pays). El detector guarda el OI de cada pasada en `detector-state` para el futuro. En esta repetición `oi_buildup` no puede saltar.")
    a("- Flujo agresivo (CVD, desequilibrio comprador/vendedor): **no en el pasado**. `recentTrades` devuelve solo los últimos ~10 cierres de cada nombre, sin rango de fechas. El archivo completo está en `s3://hl-mainnet-node-data/node_fills_by_block` y también es requester-pays; no se ha descargado. El websocket `trades` es en directo, no histórico. El delta solo se puede validar de ahora en adelante: cada pasada acumula la muestra que alcanza a ver y la guarda en el estado. No es la cinta entera.")
    a("- Funding histórico intradía: no está en las velas. El cambio de funding queda para cuando el estado acumule varias pasadas.")
    a("- Miedo y codicia y la tendencia de BTC entran en directo con peso ±1 y no pueden crear un aviso solos. No cambian esta tabla.")
    a("")
    a("## De dónde sale cada regla")
    a("")
    a("| Pieza | Método que copia | Por qué puede ir por delante del precio | Peso | En la repetición |")
    a("| --- | --- | --- | --- | --- |")
    a("| `absorption` | Ley de esfuerzo y resultado de Wyckoff (el volumen es el esfuerzo, el recorrido es el resultado; StockCharts ChartSchool y Villahermosa, *The Wyckoff Methodology in Depth*). El múltiplo pedido por el dueño es 8× la mediana de 48 horas. En esta cinta eso solo no basta: la mediana de un día con horas muertas cae en esas horas muertas, y una hora normal de BTC o DOGE sale a 8–20× la mediana siendo 2–3× la media. El RVOL de siempre (TradingView y los filtros de volumen inusual) divide por la media. La vela tiene que cumplir las dos: 8× la mediana y 8× la media. El cuerpo ≤ 1% y el rango ≤ 2% son las velas de SOPH que sí pasan (cuerpo 0,4–0,7%, rango 1,4–1,7%). La dirección es el close location de Larry Williams / Arms. Si hay delta, el delta manda. | Un esfuerzo grande que no mueve el precio es absorción. El precio sale después. Hace falta repetirlo: una campaña, no un print. | 3 si es una sola vela (no llega a 5). 5 si hay 2 o más dentro de 48h. No se suma `vol_accel`: es la misma idea. | Sí. SOPH hora por hora, y el ruido del universo líquido. |")
    a("| `vol_accel` | RVOL contra la media de 20 velas, con el precio todavía por debajo del 3% | El mismo esfuerzo/resultado, en versión débil (3×). Sirve de compañero de otra pista, no de aviso. | 2 | Se calcula. No avisa solo. |")
    a("| `squeeze_breakout` | Squeeze de Bollinger dentro de Keltner (John Carter, *Mastering the Trade*) y volumen en la salida (John Bollinger) | La compresión se resuelve al salir del rango, antes de que el movimiento lleve un 8% | 3. Con el volumen del propio breakout hace 3, así que necesita otra pieza para llegar a 5. | Sí |")
    a("| `oi_buildup` | Lectura de interés abierto del CME: el OI sube y el precio no | Se abren posiciones nuevas antes de que el precio elija lado | 2 | No hay OI horario histórico |")
    a("| `cvd_absorption` | Delta: agresor comprador (lado B en Hyperliquid) menos agresor vendedor (lado A), con el precio aún en un rango pequeño | Quien cruza el spread es quien empuja. Si empuja y el precio no cede, la salida suele seguir. | 3 | Solo en directo, muestra de `recentTrades` |")
    a("| `cvd_divergence` | Divergencia de delta (el precio sube y el agresor vende, o al revés) | El movimiento no tiene quien lo empuje: menos confianza | resta 2 | Solo en directo |")
    a("| `funding_shift` | Funding horario de Hyperliquid, que es la prima del lado abarrotado | Un salto de prima marca un cambio de sesgo. No elige la operación. | 1 | No hay serie intradía |")
    a("| Contexto BTC / miedo y codicia | Filtro de régimen. El índice es el Crypto Fear & Greed de alternative.me | No adelanta a la alt. Evita ir contra el líder y contra un extremo de ánimo. | ±1, no crea avisos | No entra en las cifras |")
    a("| Fase 2, impulso | Retroceso de Fibonacci 38,2–50% y movimiento medido (Edwards y Magee, Bulkowski) | El aviso de fase 1 llega con el impulso hecho. La entrada operable es el retest, y solo si TP1 paga al menos 2 veces el riesgo. | — | Sí, con las velas de 15m posteriores |")
    a("| Fase 2, absorción | La vigilancia no es un retroceso: el impulso todavía no existe. La entrada es el cierre al otro lado del rango absorbido. El stop es el lado contrario del rango (el esfuerzo falló). TP1 son dos alturas de ese rango, para que una entrada en el borde pague justo 2R, que es el mínimo que ya pide la fase 2. TP2 son tres. Si la vela abre ya muy por encima, no se persigue. | La absorción avisa antes; la entrada es la rotura, no la vela del volumen. Urgencia baja. | — | Sí, en los casos de abajo. De 00:00 a 08:00 la rotura se anota y no se publica. |")
    a("")
    a("Umbral de fase 1 temprana: puntuación ≥ **5**. Una absorción repetida llega justo a 5. Una compresión (3) necesita otra pieza. Objetivo orientativo: no muchos más de ~10 avisos de fase 1 al día en todo el universo líquido. La fase 2 solo publica `entry_ready` si R:R a TP1 ≥ **2**. `entry_ready` no gasta el cupo de 10 al día ni el de 2 por hora.")
    a("")
    a(f"Universo líquido (volumen 24h **actual** ≥ 1M$ en el dex principal, más los seis casos): **{n_universe}** nombres. El volumen de hace una semana no está en la API; para cada vela se usa la suma de las 24 velas de 1 hora anteriores. El filtro de OI de HIP-3 no se aplica porque no hay OI histórico. SOPH hoy cotiza justo por encima del millón (~1,27M$); el 9 nov a las 11:00 la suma de 24 horas todavía estaba por debajo, y a las 16:00 ya estaba por encima.")
    a("")
    a("## SOPH, 9 nov: volumen sin precio")
    a("")
    a("El dueño señaló cuatro horas del 9 nov (Madrid) con precio casi quieto y, desde las 03:00 del 10 nov, una subida de 0,00119 a 0,00148 (+24%). Medido en las velas de Hyperliquid:")
    a("")
    a("| Hora | Notional | × mediana / × media | Cuerpo | Rango | Suma 24h | Close location | ¿Pasa el filtro? |")
    a("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in soph:
        rvol_mean = row.get("rvol_mean")
        rvol = f"{row['rvol']:.1f}×" if row["rvol"] is not None else "—"
        if rvol_mean is not None:
            rvol += f" / media {rvol_mean:.1f}×"
        why = "sí" if row["hit"] else "no"
        if row["hit"] and row["repeats"] >= 2 and row["roll"] >= 1_000_000:
            why = f"sí, aviso ({row['repeats']} velas, {row['side']})"
        elif row["hit"] and row["roll"] < 1_000_000:
            why = f"la vela sí, el libro no (suma 24h {row['roll']/1e6:.2f}M$)"
        elif row["hit"]:
            why = f"vela sí, todavía no es repetición ({row['side']})"
        elif row["rng"] > 2:
            why = f"no: el rango es {row['rng']:.2f}%, por encima del 2%"
        elif (row.get("rvol") or 0) >= 8 and (row.get("rvol_mean") or 0) < 8:
            why = "no: pasa la mediana y no llega a 8× la media"
        a(
            f"| {row['when'][5:]} | {row['ntl']/1e3:.0f}k$ | {rvol} | {row['ret']:.2f}% | "
            f"{row['rng']:.2f}% | {row['roll']/1e6:.2f}M$ | {row['clv']:+.2f} | {why} |"
        )
    a("")
    a("La de las 11:00 (265k$, 14× la mediana y 10× la media) es la primera vela que cumple, pero el libro de 24 horas todavía está en 0,85M$, por debajo del millón, y cierra en la parte baja del rango: no abre una vigilancia en largo. La de las 12:00 (154k$) llega a 8× la mediana y solo a 5× la media, y el rango se va a 2,11%, así que no entra. La de las 16:00 (565k$, 30× la mediana y 17× la media) es la repetición, el libro ya está en 1,50M$ y el cierre queda en la parte alta: ese es el aviso, 10,8 horas antes del mínimo de las 02:45 del día 10. La de las 22:00 llega a 15× la mediana pero solo a unas 6× la media, así que no refresca la vigilancia. La vigilancia de las 16:00 no llega a entrada: a las 18:30 un cierre de 15 minutos (0,001223) queda por debajo del suelo del rango (cerca de 0,001225) y se invalida. La subida de las 03:00–05:00 ocurre ya sin esa vigilancia. No hay `entry_ready` de la absorción.")
    a("")
    a("## Casos")
    a("")
    a("El movimiento es el mínimo y el máximo del día en velas de 15 minutos (de uno al otro, en el orden en que ocurren). El porcentaje es cuánto de ese recorrido ya estaba hecho al cierre de la vela del aviso. Un adelanto positivo es horas antes del inicio de ese movimiento.")
    a("")
    a("| Caso | Movimiento | Primera vela de absorción | Primer aviso de absorción | Aviso ya existente | Fase 2 de la absorción | Fase 2 del aviso existente |")
    a("| --- | --- | --- | --- | --- | --- | --- |")
    for row in cases:
        a("| " + " | ".join([
            row["name"], row["move"], row["spike"], row["early"], row["existing"],
            row["phase_abs"], row["phase_old"],
        ]) + " |")
    a("")
    a("## Avisos por día")
    a("")
    a("Fuera del silencio (00:00–08:00). `total` cuenta avisos, no piezas: un aviso con dos nombres suma uno. La primera tabla no aplica la pausa de 4 horas (techo). La segunda sí, un aviso explosivo por nombre cada 4 horas, que es lo que hace el detector.")
    a("")
    trigs = ["absorption", "squeeze_breakout", "vol_accel", "1h_legacy", "1h_atr", "15m_early", "3h_escalera", "total"]

    def table(title, counts):
        a(title)
        a("")
        days = sorted(counts)
        a("| Día | " + " | ".join(trigs) + " |")
        a("| --- | " + " | ".join("---" for _ in trigs) + " |")
        for day in days:
            vals = []
            for name in trigs:
                key = "__n" if name == "total" else name
                vals.append(str(counts[day].get(key, 0)))
            a("| " + day + " | " + " | ".join(vals) + " |")
        a("")

    table("Sin la pausa de 4 horas:", raw_counts)
    table("Con la pausa de 4 horas:", cool_counts)
    a("## Umbrales que quedan")
    a("")
    a("Se dejan los de `data/detector_config.json`. Absorción: 8× la mediana y 8× la media de las 48 horas anteriores, cuerpo ≤ 1%, rango ≤ 2%, y al menos 2 velas así dentro de esas 48 horas. Una sola vela pesa 3 y no avisa. La repetición pesa 5 y abre la vigilancia. RVOL débil: 3× la media con el precio aún por debajo del 3%, peso 2. Squeeze: al menos 6 velas de Bollinger dentro de Keltner y volumen de salida ≥ 1,5×, peso 3. En dos días de esta semana esa pareja, ya con la pausa de 4 horas, se acerca o pasa los 10 avisos; si hay que bajar ese ruido, el siguiente paso es subir `squeeze_min_bars_inside`, no inventar otro filtro. Puntuación mínima 5. Retroceso 38,2–50% cuando el impulso ya está hecho. En la absorción, la entrada es la rotura del rango, el stop del plan es el otro lado y TP1 son dos alturas del rango. Mientras no hay entrada, una mecha al otro lado no invalida (es el spring de Wyckoff); invalida un cierre al otro lado. R:R mínimo 2. Caducidad 12 horas. `oi_buildup` y el delta quedan activos para el directo y esta repetición no los puede confirmar: el delta solo se podrá juzgar con lo que se acumule a partir de ahora.")
    a("")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


def main():
    cfg = detector.load_cfg()
    start = int(datetime(2026, 11, 3, tzinfo=timezone.utc).timestamp() * 1000)
    end = int(datetime(2026, 11, 11, tzinfo=timezone.utc).timestamp() * 1000)
    names = universe()
    print(f"universe {len(names)}", flush=True)
    all_early = {}
    all_existing = {}
    all_spikes = {}
    store = {}
    for i, coin in enumerate(names):
        try:
            h = fetch_candles(coin, "1h", start, end)
            m = fetch_candles(coin, "15m", start, end)
        except Exception as exc:
            print("skip", coin, exc, flush=True)
            continue
        if len(h) < 30:
            continue
        existing, early, spikes = collect(coin, h, m, cfg)
        all_existing[coin] = existing
        all_early[coin] = early
        all_spikes[coin] = spikes
        store[coin] = (h, m)
        if (i + 1) % 10 == 0:
            print(f"scored {i + 1}/{len(names)}", flush=True)
    liquid_now = set()
    for coin, (h, _) in store.items():
        pref = prefix(h)
        if rolling_day(pref, len(h) - 1) >= cfg["early_min_day_ntl_usd"] or coin in {c[0] for c in CASES}:
            liquid_now.add(coin)
    merged = []
    for coin in liquid_now:
        merged.extend(all_existing.get(coin, []))
        merged.extend(all_early.get(coin, []))
    raw_counts = day_counts(merged)
    cool_counts = day_counts(cool(merged))
    case_rows = []
    for coin, day_from, day_to, move_day in CASES:
        pair = store.get(coin)
        label = f"{coin} {move_day}"
        if not pair:
            case_rows.append({
                "name": label, "move": "sin velas", "spike": "—", "early": "—",
                "existing": "—", "phase_abs": "—", "phase_old": "—",
            })
            continue
        h, m15 = pair
        mv = move_on(m15, move_day)
        if not mv:
            case_rows.append({
                "name": label, "move": "sin rango", "spike": "—", "early": "—",
                "existing": "—", "phase_abs": "—", "phase_old": "—",
            })
            continue
        move_s = (
            f"{madrid(mv['start_t']).strftime('%m-%d %H:%M')} {mv['side']} "
            f"{mv['start']:.4g} → {madrid(mv['end_t']).strftime('%H:%M')} {mv['end']:.4g}"
        )
        spikes = [s for s in all_spikes.get(coin, []) if in_window(s["madrid"], day_from, day_to)]
        early = [e for e in all_early.get(coin, []) if in_window(e["madrid"], day_from, day_to)]
        exist = [e for e in all_existing.get(coin, []) if in_window(e["madrid"], day_from, day_to)]
        # Keep a signal if it sits on the move, or in the 18h before it.
        # A quiet staircase from the day before is a different episode.
        near = mv["start_t"] - 18 * 3_600_000
        def _near(e):
            return e.get("t") and near <= e["t"] <= mv["end_t"] + 6 * 3_600_000
        spikes = [s for s in spikes if _near(s)] or spikes
        early = [e for e in early if _near(e)]
        exist = [e for e in exist if _near(e)]
        spikes.sort(key=lambda e: e["t"])
        early.sort(key=lambda e: e["t"])
        exist.sort(key=lambda e: e["t"])
        abs_alerts = [e for e in early if "absorption" in e["triggers"] and e.get("kind") == "absorption"]
        first_spike = spikes[0] if spikes else None
        first_abs = abs_alerts[0] if abs_alerts else None
        first_old = exist[0] if exist else None
        if first_spike and "triggers" not in first_spike:
            first_spike = {**first_spike, "triggers": ["vela"]}
        campaign = [e for e in abs_alerts if e["t"] <= mv["end_t"]]
        p_abs = phase2_absorption_campaign(m15, campaign, cfg) if campaign else {"status": "sin aviso"}
        source = next((e for e in exist if not e.get("quiet")), first_old)
        p_old = phase2(m15, source, cfg) if source else {"status": "sin aviso"}
        case_rows.append({
            "name": label,
            "move": move_s,
            "spike": fmt_event(first_spike, mv),
            "early": fmt_event(first_abs, mv),
            "existing": fmt_event(first_old, mv),
            "phase_abs": _describe_watch_result(p_abs),
            "phase_old": _describe_watch_result(p_old),
        })
        print(label, "abs", case_rows[-1]["early"], "old", case_rows[-1]["existing"], flush=True)
    soph = []
    if "SOPH" in store:
        soph = soph_rows(store["SOPH"][0], cfg)
    write_report(case_rows, raw_counts, cool_counts, len(liquid_now), cfg, soph)


if __name__ == "__main__":
    main()
