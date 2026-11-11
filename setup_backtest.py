#!/usr/bin/env python3
"""Replay the setup rules on the last 14 days of 1h candles.

One position per coin at a time. The stop is the initial stop until price
has traded +1R, then Charles LeBeau's Chandelier Exit (3 ATR) can only
tighten it. Hyperliquid taker fee is 0.045% per side. The report is a
study, not a promise.
"""
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import setup_scanner as setups
from detector import load_cfg
from perps_all import post

MADRID = ZoneInfo("Europe/Madrid")
ROOT = Path(__file__).resolve().parent
CACHE = Path("/tmp/hl-setup")
OUT = ROOT / "reports" / "setup_backtest_2026-11-11.md"
EVAL_FROM = datetime(2026, 10, 12, tzinfo=timezone.utc)
EVAL_TO = datetime(2026, 11, 12, tzinfo=timezone.utc)
FETCH_FROM = datetime(2026, 9, 20, tzinfo=timezone.utc)


def fetch(coin, start_ms, end_ms):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{coin.replace(':', '_')}_1h_{start_ms}.json"
    if path.exists():
        return json.loads(path.read_text())
    data = post({
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": "1h", "startTime": start_ms, "endTime": end_ms},
    })
    if not isinstance(data, list):
        data = []
    path.write_text(json.dumps(data))
    return data


def universe():
    meta, ctxs = post({"type": "metaAndAssetCtxs"})
    names = []
    listed = below = 0
    for u, c in zip(meta["universe"], ctxs):
        if u.get("isDelisted"):
            continue
        listed += 1
        try:
            day = float(c.get("dayNtlVlm") or 0)
        except (TypeError, ValueError):
            day = 0
        if day >= 1_000_000:
            names.append(u["name"])
        else:
            below += 1
    forced = "GRAM" not in names
    if forced:
        names.append("GRAM")
    return sorted(set(names)), {"listed": listed, "below_1m": below, "at_least_1m": listed - below, "gram_added": forced}


def asset_for(coin):
    return {
        "coin": coin, "dex": "main", "dayNtlVlm": 5_000_000, "oi_usd": 2_000_000,
        "funding_pct": 0.0, "premium_pct": 0.0,
    }


def day_key(ms):
    return datetime.fromtimestamp(ms / 1000, MADRID).strftime("%Y-%m-%d")


def rose_on(cs, day):
    bars = [c for c in cs if day_key(c["t"]) == day]
    if len(bars) < 4:
        return None
    first = float(bars[0]["o"])
    last = float(bars[-1]["c"])
    high = max(float(c["h"]) for c in bars)
    if not first:
        return None
    return {
        "open": first, "close": last, "high": high,
        "close_pct": (last / first - 1) * 100,
        "high_pct": (high / first - 1) * 100,
    }


def replay_coin(cs, cfg, counts, btc_4h):
    """Closed trades whose entry bar is inside the window. `counts` collects filter rejections."""
    trades = []
    start_ms = EVAL_FROM.timestamp() * 1000
    end_ms = EVAL_TO.timestamp() * 1000
    need = int(cfg["setup_ema_slow"]) + 2
    i = need
    coin = cs[0].get("s") or ""
    busy_until = -1
    while i < len(cs) - 1:
        if cs[i]["t"] < start_ms or cs[i]["t"] >= end_ms:
            i += 1
            continue
        window = cs[max(0, i - 23):i + 1]
        day = sum(setups.bar_notional(c) or 0 for c in window)
        asset = asset_for(coin)
        asset["dayNtlVlm"] = day
        bias = setups.btc_bias(btc_4h, cs[i]["t"] + 3_600_000) or "unknown"
        _sig, reason = setups.inspect_breakout(cs[:i + 1], i, asset, cfg, bias=bias)
        if reason != "quiet":
            counts["breakout:" + reason] += 1
        got = setups.evaluate(cs[:i + 1], i, asset, cfg, bias=bias)
        if i <= busy_until or not got:
            i += 1
            continue
        risk = abs(got["entry"] - got["initial_sl"])
        walked = setups.simulate(
            cs, i, got["side"], got["initial_sl"], cfg["setup_chandelier_atr"], risk, got["atr"],
        )
        if not walked:
            i += 1
            continue
        exit_i, exit_px, partial = walked
        net = setups.net_managed(got["entry"], exit_px, risk, got["side"], partial)
        if net is None:
            i += 1
            continue
        net_r, net_pct, fee_r, gross_r = net
        hold_h = max(0, (cs[min(exit_i, len(cs) - 1)]["t"] - cs[i]["t"]) / 3_600_000)
        trade = {
            "setup": got["setup"], "side": got["side"], "t": cs[i]["t"],
            "exit_t": cs[min(exit_i, len(cs) - 1)]["t"],
            "entry": got["entry"], "exit": exit_px, "sl": got["initial_sl"],
            "tp1": got["tp1"], "net_r": net_r, "net_pct": net_pct,
            "fee_r": fee_r, "gross_r": gross_r, "hold_h": hold_h,
            "coin": asset["coin"],
        }
        trades.append(trade)
        busy_until = exit_i
        i += 1
    return trades


def replay_4h(cs, cfg, counts, btc_4h):
    """Donchian on completed 4h bars. Its own position book, same stop and trail."""
    bars = setups.to_4h(cs)
    trades = []
    start_ms = EVAL_FROM.timestamp() * 1000
    end_ms = EVAL_TO.timestamp() * 1000
    need = int(cfg["setup_ema_slow"]) + 2
    coin = cs[0].get("s") or ""
    busy_until = -1
    i = need
    while i < len(bars):
        if bars[i]["t"] < start_ms or bars[i]["t"] >= end_ms:
            i += 1
            continue
        # 24h notional from the hourly bars that ended at or before this 4h close.
        close_ms = bars[i]["t"] + 4 * 3_600_000
        window = [c for c in cs if close_ms - 24 * 3_600_000 <= (c.get("t") or 0) < close_ms]
        asset = asset_for(coin)
        asset["dayNtlVlm"] = sum(setups.bar_notional(c) or 0 for c in window) if window else 0
        bias = setups.btc_bias(btc_4h, close_ms) or "unknown"
        _sig, reason = setups.inspect_breakout(bars[:i + 1], i, asset, cfg, bias=bias, kind="trend_breakout_4h")
        if reason != "quiet":
            counts["breakout4:" + reason] += 1
        if i <= busy_until or _sig is None:
            i += 1
            continue
        got = _sig
        risk = abs(got["entry"] - got["initial_sl"])
        walked = setups.simulate(bars, i, got["side"], got["initial_sl"], cfg["setup_chandelier_atr"], risk, got["atr"])
        if not walked:
            i += 1
            continue
        exit_i, exit_px, partial = walked
        net = setups.net_managed(got["entry"], exit_px, risk, got["side"], partial)
        if net is None:
            i += 1
            continue
        net_r, net_pct, fee_r, gross_r = net
        hold_h = max(0, (bars[min(exit_i, len(bars) - 1)]["t"] - bars[i]["t"]) / 3_600_000)
        trades.append({
            "setup": "trend_breakout_4h", "side": got["side"], "t": bars[i]["t"],
            "exit_t": bars[min(exit_i, len(bars) - 1)]["t"],
            "entry": got["entry"], "exit": exit_px, "sl": got["initial_sl"],
            "tp1": got["tp1"], "net_r": net_r, "net_pct": net_pct,
            "fee_r": fee_r, "gross_r": gross_r, "hold_h": hold_h, "coin": coin,
        })
        busy_until = exit_i
        i += 1
    return trades


def streak(trades):
    worst = cur = 0
    for t in trades:
        if t["net_r"] < 0:
            cur += 1
            worst = max(worst, cur)
        else:
            cur = 0
    return worst


def summarise(trades):
    if not trades:
        return {"n": 0, "win": 0, "exp_r": 0, "gross_r": 0, "fee_share": 0, "streak": 0, "hold": 0}
    wins = sum(1 for t in trades if t["net_r"] > 0)
    gross = sum(abs(t["gross_r"]) for t in trades)
    fees = sum(t["fee_r"] for t in trades)
    return {
        "n": len(trades),
        "win": wins / len(trades) * 100,
        "exp_r": sum(t["net_r"] for t in trades) / len(trades),
        "gross_r": sum(t["gross_r"] for t in trades) / len(trades),
        "fee_share": (fees / gross) if gross else 0,
        "streak": streak(trades),
        "hold": sum(t["hold_h"] for t in trades) / len(trades),
    }


def coin_breakouts(cs, cfg, coin, days, btc_4h):
    """Donchian hours on the named days: taken, or the filter that stopped them."""
    out = []
    asset = asset_for(coin)
    need = int(cfg["setup_ema_slow"]) + 2
    for i, c in enumerate(cs):
        if i < need or day_key(c["t"]) not in days:
            continue
        window = cs[max(0, i - 23):i + 1]
        asset["dayNtlVlm"] = sum(setups.bar_notional(b) or 0 for b in window)
        bias = setups.btc_bias(btc_4h, c["t"] + 3_600_000) or "unknown"
        _got, reason = setups.inspect_breakout(cs[:i + 1], i, asset, cfg, bias=bias)
        if reason == "quiet":
            continue
        out.append({
            "when": datetime.fromtimestamp(c["t"] / 1000, MADRID).strftime("%m-%d %H:%M"),
            "price": float(c["c"]),
            "reason": reason,
            "day_ntl": asset["dayNtlVlm"],
        })
    return out


def _table(a, per_type):
    a("| Tipo | Lado | Operaciones | Acierto | Bruto (R) | Neto (R) | Comisión/bruto | Racha | Horas |")
    a("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    order = ["trend_breakout", "trend_pullback", "trend_breakout_4h", "support_retest"]
    labels = {
        "trend_breakout": "Ruptura 20×1h",
        "trend_pullback": "Retroceso EMA20 1h",
        "trend_breakout_4h": "Ruptura 20×4h",
        "support_retest": "Retest de soporte",
    }
    for key in order:
        for side, side_name in (("long", "largo"), ("short", "corto")):
            s = per_type.get((key, side)) or summarise([])
            a(
                f"| {labels[key]} | {side_name} | {s['n']} | {s['win']:.1f}% | {s['gross_r']:.3f} | "
                f"{s['exp_r']:.3f} | {s['fee_share']:.1%} | {s['streak']} | {s['hold']:.1f} |"
            )


def write(design, oos, gram, lit, risers, n_coins, meta, counts, span):
    lines = []
    a = lines.append
    a("# Preparaciones, diseño 12–31 oct y fuera de muestra 1–11 nov 2026")
    a("")
    a("Una ruptura o un retroceso no tiene objetivo fijo: sale en el stop inicial y, desde +1R, en el Chandelier de 3 ATR. La mitad se puede cerrar en +2R. Solo se veta si el máximo o mínimo de 24h o de 7 días, medido en velas de 4 horas ya cerradas, queda a menos de 1R de la entrada. El retest de soporte sigue exigiendo el primer nivel real y un R:R de 2. La continuación (1h y 4h) solo se toma a favor de Bitcoin: largo si el cierre de 4h de BTC está por encima de su EMA200 de 4h, corto si está por debajo. La ruptura de 20 velas también se evalúa en el gráfico de 4 horas, con el stop de 2 ATR de esas velas y el mismo Chandelier. Ningún número se ha movido para que noviembre salga mejor. Comisión taker de Hyperliquid 0,045% por lado. Comisión/bruto es la suma de las comisiones en R partida por la suma del valor absoluto del resultado bruto en R. El cupo de 6 al día no se aplica aquí.")
    a("")
    a(f"Ventana de las velas pedidas: {span}. Perps listados (sin spot, sin delistados): **{meta['listed']}**. Por debajo de 1M$ de volumen 24h actual: **{meta['below_1m']}**. En el universo: **{meta['at_least_1m']}**" + (" más GRAM, que hoy no llegaba al millón." if meta["gram_added"] else "."))
    a(f"Nombres con velas suficientes para la EMA50: **{n_coins}**. Con historia corta (se saltan): **{meta.get('short_history', 0)}**.")
    btc = meta.get("btc_span") or "velas de 4h desde el 1 jun 2026"
    a("En cada vela el filtro de libro usa la suma de las 24 horas anteriores. Un corto pide 5M$ en esa suma. El interés abierto de HIP-3 no está en las velas. El régimen de BTC usa " + btc + ".")
    a("")
    a("## Diseño, 12–31 oct (fecha de Madrid de la entrada)")
    a("")
    _table(a, design)
    a("")
    a("## Fuera de muestra, 1–11 nov (fecha de Madrid de la entrada)")
    a("")
    _table(a, oos)
    a("")
    a("## Qué filtro paró las rupturas de canal")
    a("")
    a("Solo cuentan las velas que cerraron fuera del canal de 20 horas. El resto no es una ruptura y no entra en esta tabla. Aceptadas es una vela que pasa los filtros. Si ese nombre ya tiene una posición abierta, no se abre otra: por eso hay menos operaciones que velas aceptadas.")
    a("")
    a("| Filtro | Velas |")
    a("| --- | --- |")
    labels = {
        "breakout:short_history": "Historia corta (EMA50 o Donchian)",
        "breakout:ema": "Ruptura con la EMA20 del lado contrario",
        "breakout:volume": "Volumen por debajo de 1,5× la media de 20",
        "breakout:book_1m": "Libro de 24h por debajo de 1M$ (largo)",
        "breakout:book_5m": "Libro de 24h por debajo de 5M$ (corto)",
        "breakout:atr": "ATR o stop no calculable",
        "breakout:major_level": "Máximo o mínimo de 24h/7d a menos de 1R",
        "breakout:regime": "BTC 4h del lado contrario a su EMA200",
        "breakout:regime_unknown": "EMA200 de BTC todavía sin 200 velas",
        "breakout:taken": "Aceptadas en 1h",
        "breakout4:ema": "4h: EMA20 del lado contrario",
        "breakout4:volume": "4h: volumen por debajo de 1,5×",
        "breakout4:book_1m": "4h: libro largo por debajo de 1M$",
        "breakout4:book_5m": "4h: libro corto por debajo de 5M$",
        "breakout4:major_level": "4h: extremo de 24h/7d a menos de 1R",
        "breakout4:regime": "4h: BTC del lado contrario",
        "breakout4:regime_unknown": "4h: EMA200 de BTC no lista",
        "breakout4:taken": "Aceptadas en 4h",
    }
    for key, label in labels.items():
        a(f"| {label} | {counts.get(key, 0)} |")
    a("")
    a("## GRAM, 9–10 nov")
    a("")
    if not gram:
        a("No hay velas de GRAM en la ventana.")
    else:
        a(f"Movimiento del 10 nov (cierre contra la apertura, Madrid): {gram['move']}")
        if gram["trades"]:
            for t in gram["trades"]:
                when = datetime.fromtimestamp(t["t"] / 1000, MADRID).strftime("%m-%d %H:%M")
                a(
                    f"- {when} {t['setup']} {t['side']} entrada {t['entry']:.4g} "
                    f"salida {t['exit']:.4g} resultado {t['net_r']:.2f}R ({t['net_pct']:.2f}%)"
                )
        else:
            a("Ninguna preparación de GRAM quedó abierta entre el 9 y el 10 nov.")
        if gram.get("breaks"):
            a("")
            a("Horas del 9 y el 10 en las que el cierre salió del canal de 20. El motivo es el filtro que paró esa vela, o `taken` si entró:")
            for m in gram["breaks"]:
                a(f"- {m['when']} precio {m['price']:.4g}, libro 24h {m['day_ntl']/1e6:.2f}M$, {m['reason']}")
    a("")
    a("## LIT, 10 nov")
    a("")
    if not lit:
        a("No hay velas de LIT.")
    else:
        a(f"Movimiento del 10 nov: {lit['move']}")
        if lit["trades"]:
            for t in lit["trades"]:
                when = datetime.fromtimestamp(t["t"] / 1000, MADRID).strftime("%m-%d %H:%M")
                a(f"- {when} {t['setup']} {t['side']} {t['net_r']:.2f}R")
        else:
            a("LIT no abrió una preparación ese día.")
        if lit.get("breaks"):
            a("Rupturas de canal el 10 nov:")
            for m in lit["breaks"]:
                a(f"- {m['when']} precio {m['price']:.4g}, libro {m['day_ntl']/1e6:.2f}M$, {m['reason']}")
    a("")
    a("## Nombres que subieron más de un 6% el 10 nov")
    a("")
    a("El porcentaje es el cierre de ese día en Madrid contra su apertura. «Cazada» significa que hubo una preparación en largo el 9 o el 10 nov, antes o durante ese día.")
    a("")
    a("| Nombre | Cierre/apertura | Máximo/apertura | Preparación |")
    a("| --- | --- | --- | --- |")
    if not risers:
        a("| — | — | — | — |")
    for row in risers:
        a(f"| {row['coin']} | {row['close_pct']:.1f}% | {row['high_pct']:.1f}% | {row['caught']} |")
    a("")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


def fetch_btc():
    start = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
    end = int(EVAL_TO.timestamp() * 1000)
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"BTC_4h_{start}.json"
    if path.exists():
        data = json.loads(path.read_text())
    else:
        data = post({
            "type": "candleSnapshot",
            "req": {"coin": "BTC", "interval": "4h", "startTime": start, "endTime": end},
        })
        if not isinstance(data, list):
            data = []
        path.write_text(json.dumps(data))
    return data


def main():
    cfg = load_cfg()
    start = int(FETCH_FROM.timestamp() * 1000)
    end = int(EVAL_TO.timestamp() * 1000)
    btc_4h = fetch_btc()
    if btc_4h:
        btc_span = (
            f"{len(btc_4h)} velas de 4h, "
            + datetime.fromtimestamp(btc_4h[0]["t"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            + " → "
            + datetime.fromtimestamp(btc_4h[-1]["t"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        )
    else:
        btc_span = "ninguna vela de 4h de BTC"
    print(f"btc 4h {btc_span}", flush=True)
    names, meta = universe()
    print(f"universe {len(names)} listed {meta['listed']} below_1m {meta['below_1m']}", flush=True)
    by_type = defaultdict(list)
    counts = defaultdict(int)
    store = {}
    short_history = 0
    span = "sin velas"
    for i, coin in enumerate(names):
        try:
            cs = fetch(coin, start, end)
        except Exception as exc:
            print("skip", coin, exc, flush=True)
            continue
        if len(cs) < int(cfg["setup_ema_slow"]) + 10:
            short_history += 1
            continue
        if not cs[0].get("s"):
            for c in cs:
                c["s"] = coin
        if span == "sin velas":
            span = (
                datetime.fromtimestamp(cs[0]["t"] / 1000, timezone.utc).strftime("%Y-%m-%d")
                + " → "
                + datetime.fromtimestamp(cs[-1]["t"] / 1000, timezone.utc).strftime("%Y-%m-%d")
                + f" ({len(cs)} velas de 1h en el primer nombre)"
            )
        trades = replay_coin(cs, cfg, counts, btc_4h) + replay_4h(cs, cfg, counts, btc_4h)
        for t in trades:
            t["coin"] = coin
            period = "design" if day_key(t["t"]) < "2026-11-01" else "oos"
            by_type[(period, t["setup"], t["side"])].append(t)
        store[coin] = (cs, trades)
        if (i + 1) % 10 == 0:
            print(f"replayed {i+1}/{len(names)}", flush=True)
    meta["short_history"] = short_history
    meta["btc_span"] = btc_span
    design = {k[1:]: summarise(v) for k, v in by_type.items() if k[0] == "design"}
    oos = {k[1:]: summarise(v) for k, v in by_type.items() if k[0] == "oos"}
    for label, table in (("design", design), ("oos", oos)):
        for key, side in (
            ("trend_breakout", "long"), ("trend_breakout", "short"),
            ("trend_pullback", "long"), ("trend_pullback", "short"),
            ("trend_breakout_4h", "long"), ("trend_breakout_4h", "short"),
        ):
            s = table.get((key, side)) or summarise([])
            print(f"{label} {key} {side} n={s['n']} netR={s['exp_r']:.3f} fee={s['fee_share']:.1%}", flush=True)
    gram_cs, gram_tr = store.get("GRAM", (None, []))
    gram = None
    if gram_cs:
        move = rose_on(gram_cs, "2026-11-10")
        window = [
            t for t in gram_tr
            if "2026-11-09" <= day_key(t["t"]) <= "2026-11-10"
        ]
        gram = {
            "move": "sin rango" if not move else f"{move['close_pct']:.1f}% (máximo {move['high_pct']:.1f}%)",
            "trades": window,
            "breaks": coin_breakouts(gram_cs, cfg, "GRAM", {"2026-11-09", "2026-11-10"}, btc_4h),
        }
    lit = None
    lit_cs, lit_tr = store.get("LIT", (None, []))
    if lit_cs:
        move = rose_on(lit_cs, "2026-11-10")
        lit = {
            "move": "sin rango" if not move else f"{move['close_pct']:.1f}% (máximo {move['high_pct']:.1f}%)",
            "trades": [t for t in lit_tr if day_key(t["t"]) == "2026-11-10"],
            "breaks": coin_breakouts(lit_cs, cfg, "LIT", {"2026-11-10"}, btc_4h),
        }
    risers = []
    for coin, (cs, trades) in store.items():
        move = rose_on(cs, "2026-11-10")
        if not move or move["close_pct"] <= 6:
            continue
        caught = [
            t for t in trades
            if t["side"] == "long" and "2026-11-09" <= day_key(t["t"]) <= "2026-11-10"
        ]
        risers.append({
            "coin": coin,
            "close_pct": move["close_pct"],
            "high_pct": move["high_pct"],
            "caught": ", ".join(t["setup"] for t in caught) if caught else "no",
        })
    risers.sort(key=lambda r: r["close_pct"], reverse=True)
    write(design, oos, gram, lit, risers, len(store), meta, counts, span)


if __name__ == "__main__":
    main()
