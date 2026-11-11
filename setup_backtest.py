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
EVAL_FROM = datetime(2026, 10, 28, tzinfo=timezone.utc)
EVAL_TO = datetime(2026, 11, 12, tzinfo=timezone.utc)
FETCH_FROM = datetime(2026, 10, 18, tzinfo=timezone.utc)


def fetch(coin, start_ms, end_ms):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{coin.replace(':', '_')}_1h.json"
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
    for u, c in zip(meta["universe"], ctxs):
        if u.get("isDelisted"):
            continue
        try:
            day = float(c.get("dayNtlVlm") or 0)
        except (TypeError, ValueError):
            day = 0
        if day >= 1_000_000:
            names.append(u["name"])
    if "GRAM" not in names:
        names.append("GRAM")
    return sorted(set(names))


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


def replay_coin(cs, cfg):
    """Closed trades whose entry bar is inside the 14-day window."""
    trades = []
    signals = []
    start_ms = EVAL_FROM.timestamp() * 1000
    end_ms = EVAL_TO.timestamp() * 1000
    need = int(cfg["setup_ema_slow"]) + 2
    i = need
    coin = cs[0].get("s") or ""
    while i < len(cs) - 1:
        if cs[i]["t"] < start_ms or cs[i]["t"] >= end_ms:
            i += 1
            continue
        window = cs[max(0, i - 23):i + 1]
        day = sum(setups.bar_notional(c) or 0 for c in window)
        asset = asset_for(coin)
        asset["dayNtlVlm"] = day
        got = setups.evaluate(cs[:i + 1], i, asset, cfg)
        if not got:
            i += 1
            continue
        risk = abs(got["entry"] - got["initial_sl"])
        walked = setups.simulate(
            cs, i, got["side"], got["initial_sl"], cfg["setup_chandelier_atr"], risk, got["atr"],
        )
        if not walked:
            i += 1
            continue
        exit_i, exit_px = walked
        net = setups.net_trade(got["entry"], exit_px, risk, got["side"])
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
        signals.append(trade)
        i = max(exit_i, i + 1)
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


def gram_near_misses(cs, cfg):
    """Hours where the Donchian rules held and the structural R:R did not."""
    out = []
    asset = asset_for("GRAM")
    asset["dayNtlVlm"] = 5_000_000
    need = int(cfg["setup_ema_slow"]) + 2
    for i, c in enumerate(cs):
        if i < need:
            continue
        if day_key(c["t"]) not in ("2026-11-09", "2026-11-10"):
            continue
        cl = setups.closes(cs[:i + 1])
        fast, slow = setups.ema_series(cl, 20), setups.ema_series(cl, 50)
        hi = setups._donchian(cs, i, 20, "high")
        if not cl or fast[i] is None or slow[i] is None or hi is None:
            continue
        if not (cl[i] > hi and fast[i] > slow[i] and setups._volume_ok(cs, i, 1.5, 20)):
            continue
        got = setups.trend_breakout(cs[:i + 1], i, asset, cfg)
        if got:
            continue
        import watch
        price = cl[i]
        atrs = setups.wilder_atr(cs[:i + 1], int(cfg["atr_period"]))
        swing = setups.last_swing(cs, i, int(cfg["swing_wing"]), "low")
        sl = setups._stop("long", price, atrs[i], swing, cfg["setup_atr_mult"])
        tp1, _tp2 = watch.structural_targets(cs[:i + 1], "long", price)
        rr = watch.reward_risk("long", price, sl, tp1)
        out.append({
            "when": datetime.fromtimestamp(c["t"] / 1000, MADRID).strftime("%m-%d %H:%M"),
            "price": price, "tp1": tp1, "rr": rr,
        })
    return out


def write(per_type, gram, risers, n_coins):
    lines = []
    a = lines.append
    a("# Preparaciones, 28 oct – 11 nov 2026")
    a("")
    a("Velas de 1 hora. Una posición por nombre: el stop inicial hasta +1R, después el Chandelier de 3 ATR, que solo aprieta. Comisión taker de Hyperliquid 0,045% por lado (0,09% ida y vuelta), restada del resultado. Esto es un estudio, no una promesa. El cupo de 6 al día del directo no se aplica aquí: se quiere ver la regla, no el freno.")
    a("")
    a(f"Nombres con volumen 24h actual de al menos 1M$ (más GRAM si no entraba): **{n_coins}**. En cada vela el filtro de libro usa la suma de las 24 horas anteriores. Un corto pide 5M$ en esa suma. El interés abierto de HIP-3 no está en las velas, así que ese filtro no se aplica aquí.")
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
    all_trades = []
    # per_type here is stats; caller also passes nothing else. Recomputed by caller in the table only.
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
            a("La regla no abrió ninguna preparación en GRAM entre el 9 y el 10 nov.")
        if gram.get("misses"):
            a("")
            a("Horas en las que la ruptura de 20 velas, la media de 20 por encima de la de 50 y el volumen sí se cumplían, y el R:R hasta el primer nivel no llegaba a 2:")
            for m in gram["misses"]:
                rr = "sin nivel" if m["rr"] is None else f"{m['rr']:.2f}"
                tp = "—" if m["tp1"] is None else f"{m['tp1']:.4g}"
                a(f"- {m['when']} precio {m['price']:.4g}, TP1 {tp}, R:R {rr}")
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
    names = universe()
    print(f"universe {len(names)}", flush=True)
    by_type = defaultdict(list)
    store = {}
    for i, coin in enumerate(names):
        try:
            cs = fetch(coin, start, end)
        except Exception as exc:
            print("skip", coin, exc, flush=True)
            continue
        if len(cs) < int(cfg["setup_ema_slow"]) + 10:
            continue
        if not cs[0].get("s"):
            for c in cs:
                c["s"] = coin
        trades = replay_coin(cs, cfg)
        for t in trades:
            t["coin"] = coin
            by_type[t["setup"]].append(t)
        store[coin] = (cs, trades)
        if (i + 1) % 10 == 0:
            print(f"replayed {i+1}/{len(names)}", flush=True)
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
            "misses": gram_near_misses(gram_cs, cfg),
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
    write(per_type, gram, risers, len(names))


if __name__ == "__main__":
    main()
