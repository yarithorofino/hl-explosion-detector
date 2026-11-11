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


def replay_coin(cs, cfg, counts):
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
        _sig, reason = setups.inspect_breakout(cs[:i + 1], i, asset, cfg)
        if reason != "quiet":
            counts["breakout:" + reason] += 1
        got = setups.evaluate(cs[:i + 1], i, asset, cfg)
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
        net_r, net_pct = net
        hold_h = max(0, (cs[min(exit_i, len(cs) - 1)]["t"] - cs[i]["t"]) / 3_600_000)
        trade = {
            "setup": got["setup"], "side": got["side"], "t": cs[i]["t"],
            "exit_t": cs[min(exit_i, len(cs) - 1)]["t"],
            "entry": got["entry"], "exit": exit_px, "sl": got["initial_sl"],
            "tp1": got["tp1"], "net_r": net_r, "net_pct": net_pct, "hold_h": hold_h,
            "coin": asset["coin"],
        }
        trades.append(trade)
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
        return {"n": 0, "win": 0, "exp_r": 0, "exp_pct": 0, "streak": 0, "hold": 0}
    wins = sum(1 for t in trades if t["net_r"] > 0)
    return {
        "n": len(trades),
        "win": wins / len(trades) * 100,
        "exp_r": sum(t["net_r"] for t in trades) / len(trades),
        "exp_pct": sum(t["net_pct"] for t in trades) / len(trades),
        "streak": streak(trades),
        "hold": sum(t["hold_h"] for t in trades) / len(trades),
    }


def coin_breakouts(cs, cfg, coin, days):
    """Donchian hours on the named days: taken, or the filter that stopped them."""
    out = []
    asset = asset_for(coin)
    need = int(cfg["setup_ema_slow"]) + 2
    for i, c in enumerate(cs):
        if i < need or day_key(c["t"]) not in days:
            continue
        window = cs[max(0, i - 23):i + 1]
        asset["dayNtlVlm"] = sum(setups.bar_notional(b) or 0 for b in window)
        _got, reason = setups.inspect_breakout(cs[:i + 1], i, asset, cfg)
        if reason == "quiet":
            continue
        out.append({
            "when": datetime.fromtimestamp(c["t"] / 1000, MADRID).strftime("%m-%d %H:%M"),
            "price": float(c["c"]),
            "reason": reason,
            "day_ntl": asset["dayNtlVlm"],
        })
    return out


def write(per_type, gram, lit, risers, n_coins, meta, counts, span):
    lines = []
    a = lines.append
    a("# Preparaciones, repetición hasta el 11 nov 2026")
    a("")
    a("Una ruptura o un retroceso no tiene objetivo fijo: sale en el stop inicial y, desde +1R, en el Chandelier de 3 ATR. La mitad se puede cerrar en +2R. Solo se veta si el máximo o mínimo de 24h o de 7 días, medido en velas de 4 horas ya cerradas, queda a menos de 1R de la entrada. El retest de soporte sigue exigiendo el primer nivel real y un R:R de 2. Comisión taker de Hyperliquid 0,045% por lado. El cupo de 6 al día no se aplica aquí.")
    a("")
    a(f"Ventana de las velas pedidas: {span}. Perps listados (sin spot, sin delistados): **{meta['listed']}**. Por debajo de 1M$ de volumen 24h actual: **{meta['below_1m']}**. En el universo: **{meta['at_least_1m']}**" + (" más GRAM, que hoy no llegaba al millón." if meta["gram_added"] else "."))
    a(f"Nombres con velas suficientes para la EMA50: **{n_coins}**. Con historia corta (se saltan): **{meta.get('short_history', 0)}**.")
    a("En cada vela el filtro de libro usa la suma de las 24 horas anteriores. Un corto pide 5M$ en esa suma. El interés abierto de HIP-3 no está en las velas.")
    a("")
    a("| Tipo | Operaciones | Acierto | Esperanza neta (R) | Esperanza neta (%) | Racha de pérdidas | Horas medias |")
    a("| --- | --- | --- | --- | --- | --- | --- |")
    order = ["trend_breakout", "trend_pullback", "support_retest"]
    labels = {
        "trend_breakout": "Ruptura Donchian 20",
        "trend_pullback": "Retroceso a la EMA20",
        "support_retest": "Retest de soporte",
    }
    for key in order:
        s = per_type.get(key) or summarise([])
        a(
            f"| {labels[key]} | {s['n']} | {s['win']:.1f}% | {s['exp_r']:.3f} | {s['exp_pct']:.3f}% | "
            f"{s['streak']} | {s['hold']:.1f} |"
        )
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
        "breakout:taken": "Aceptadas",
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


def main():
    cfg = load_cfg()
    start = int(FETCH_FROM.timestamp() * 1000)
    end = int(EVAL_TO.timestamp() * 1000)
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
        trades = replay_coin(cs, cfg, counts)
        for t in trades:
            t["coin"] = coin
            by_type[t["setup"]].append(t)
        store[coin] = (cs, trades)
        if (i + 1) % 10 == 0:
            print(f"replayed {i+1}/{len(names)}", flush=True)
    meta["short_history"] = short_history
    per_type = {k: summarise(v) for k, v in by_type.items()}
    for key in ("trend_breakout", "trend_pullback", "support_retest"):
        per_type.setdefault(key, summarise([]))
        s = per_type[key]
        print(f"{key} n={s['n']} win={s['win']:.1f} expR={s['exp_r']:.3f} exp%={s['exp_pct']:.3f}", flush=True)
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
            "breaks": coin_breakouts(gram_cs, cfg, "GRAM", {"2026-11-09", "2026-11-10"}),
        }
    lit = None
    lit_cs, lit_tr = store.get("LIT", (None, []))
    if lit_cs:
        move = rose_on(lit_cs, "2026-11-10")
        lit = {
            "move": "sin rango" if not move else f"{move['close_pct']:.1f}% (máximo {move['high_pct']:.1f}%)",
            "trades": [t for t in lit_tr if day_key(t["t"]) == "2026-11-10"],
            "breaks": coin_breakouts(lit_cs, cfg, "LIT", {"2026-11-10"}),
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
    write(per_type, gram, lit, risers, len(store), meta, counts, span)


if __name__ == "__main__":
    main()
