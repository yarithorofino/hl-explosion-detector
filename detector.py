#!/usr/bin/env python3
"""Explosion detector for Hyperliquid perps (main + HIP-3). Analysis only, never trades.

Live:     python3 detector.py
Backtest: python3 detector.py --backtest [hours]   (1h rule only)
Test POST: python3 detector.py --force-test

Direction-neutral. A name alerts on either:
  1h_atr:    |1h move| >= atr_mult x ATR14 (in %) AND >= the floor
             (floor_pct_major for majors, floor_pct_other otherwise),
             1h notional >= vol_mult_min x the prior 24h hourly average
             AND >= vol_1h_min_usd
  15m_early: |15m move| >= pct_15m_min, 15m notional >= vol_mult_min x the
             prior 24h average 15m bar AND >= vol_15m_min_usd
  1h_legacy: |1h move| >= legacy_pct_1h AND the same 1h volume rule as above
  3h_escalera: |close-to-open move| across escalera_candles 1h bars >= escalera_pct,
             with at least escalera_min_aligned bars closing in that direction
             and printing higher highs (or lower lows). No volume multiple.
             Liquidity gates still apply. Label is "escalera".

An alert fires if any of those holds. The payload lists every trigger that fired.

Majors: names in major_coins, plus any main-dex perp whose 24h notional
is >= major_day_ntl_usd. HIP-3 names do not become majors from volume alone.
Liquidity: 24h notional >= liquidity_min_day_ntl_usd, and non-main dexes
also need OI >= hip3_min_oi_usd.

Webhook URL and auth come from the environment (never from this file):
  EXPLOSION_WEBHOOK_URL
  EXPLOSION_WEBHOOK_AUTH   full header line, e.g. "Authorization: Bearer ..."
If either is missing the cycle still runs, but no POST is made (dry-run).
A failed read of Hyperliquid does not rewrite data/detector_state.json.
"""
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import early_signals
import watch as watchmod
from perps_all import post, scan

ROOT = Path(__file__).resolve().parent
CFG = ROOT / "data" / "detector_config.json"
STATE = ROOT / "data" / "detector_state.json"
PERPS = ROOT / "data" / "perps_all.json"
ALERTS = ROOT / "signals" / "explosion_alerts.jsonl"
LOG = ROOT / "logs" / "detector.log"
MADRID = ZoneInfo("Europe/Madrid")
H = 3600
M15 = 900
ALERT_KEEP = 300
_CFG_KEYS = (
    "atr_period", "atr_mult", "floor_pct_major", "floor_pct_other", "major_coins",
    "major_day_ntl_usd", "vol_mult_min", "vol_1h_min_usd", "vol_15m_min_usd",
    "pct_15m_min", "avg_window_hours", "avg_window_15m",
    "liquidity_min_day_ntl_usd", "hip3_min_oi_usd", "tradable_min_day_ntl_usd",
    "exhaustion_wick_pct", "legacy_pct_1h", "escalera_pct", "escalera_candles",
    "escalera_min_aligned", "escalera_ignore_quiet_hours",
    "prefilter_pct_15m", "prefilter_pct_1h", "prefilter_pct_24h", "prefilter_pct_3h",
    "prefilter_15m_min_age_sec", "prefilter_15m_max_age_sec", "prefilter_15m_target_sec",
    "prefilter_1h_min_age_sec", "prefilter_1h_max_age_sec", "prefilter_1h_target_sec",
    "prefilter_3h_min_age_sec", "prefilter_3h_max_age_sec", "prefilter_3h_target_sec",
    "max_candle_calls", "cooldown_hours", "max_alerts_per_day", "max_alerts_per_hour",
    "quiet_start_hour", "quiet_end_hour", "scan_keep_days",
    "watch_expiry_hours", "watch_rr_min", "watch_fib_shallow", "watch_fib_deep",
    "watch_impulse_bars", "entry_ready_respect_quiet_hours",
    "early_min_day_ntl_usd", "early_score_min", "early_extra_names",
    "vol_accel_mult", "vol_accel_max_price_pct", "vol_accel_avg_bars",
    "vol_accel_min_notional_15m", "vol_accel_min_notional_1h",
    "oi_buildup_pct", "oi_buildup_max_price_pct",
    "squeeze_bb_period", "squeeze_bb_mult", "squeeze_kc_mult",
    "squeeze_min_bars_inside", "squeeze_vol_mult",
    "cvd_imbalance_min", "cvd_max_price_pct", "cvd_min_notional_usd",
    "cvd_window_hours", "cvd_max_coins", "funding_shift_abs",
    "early_weight_vol", "early_weight_squeeze", "early_weight_oi",
    "early_weight_cvd", "early_weight_funding", "early_weight_divergence",
    "absorption_median_bars", "absorption_repeat_bars", "absorption_rvol_min",
    "absorption_mean_mult",
    "absorption_max_return_pct", "absorption_max_range_pct", "absorption_min_repeats",
    "early_weight_absorption", "early_weight_absorption_repeat",
    "early_rotate_names",
)
_SCAN_HEADERS = (
    "dex", "coin", "15m %", "1h %", "3h %", "24h %", "mult 1h", "vol 24h", "OI",
    "funding %", "ATR %", "suelo", "disparo", "resultado",
)


def redact(value):
    text = str(value)
    secrets = []
    for name in ("EXPLOSION_WEBHOOK_URL", "EXPLOSION_WEBHOOK_AUTH", "EXPLOSION_WEBHOOK_KEY"):
        raw = os.environ.get(name) or ""
        if not raw:
            continue
        secrets.append(raw)
        secrets.append(raw.strip())
        _, _, rest = raw.partition(":")
        if rest.strip():
            secrets.append(rest.strip())
    for secret in secrets:
        if len(secret) >= 4:
            text = text.replace(secret, "[redacted]")
    return text


def log(m):
    text = redact(m)
    if len(text) > 500:
        text = text[:500] + "..."
    line = f"{datetime.now(MADRID).isoformat(timespec='seconds')} {text}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = text.encode("utf-8")
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


def write_summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines).rstrip() + "\n")
    except OSError as e:
        log(f"no se pudo escribir el resumen del job: {e}")


def _finite_num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    if v != v or v in (float("inf"), float("-inf")):
        return False
    return v > 0


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return float(v)


def load_cfg():
    if not CFG.exists():
        raise RuntimeError(f"falta {CFG}")
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    missing = [k for k in _CFG_KEYS if k not in cfg]
    if missing:
        raise RuntimeError("config sin " + ", ".join(missing))
    if not isinstance(cfg["major_coins"], list) or not cfg["major_coins"]:
        raise RuntimeError("config major_coins debe ser una lista con nombres")
    return cfg


def load_state():
    if not STATE.exists() or STATE.stat().st_size == 0:
        return {"marks": [], "last_fire": {}, "last_fire_escalera": {}, "wakes": {}, "wakes_hour": {},
                "watches": [], "cvd": {}, "early_cursor": 0}
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"estado ilegible ({e}); no se modifica") from e
    if not isinstance(st, dict):
        raise RuntimeError("estado con forma inválida; no se modifica")
    marks = []
    for s in st.get("marks") or []:
        if not isinstance(s, dict):
            continue
        ts, m = s.get("ts"), s.get("m")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not isinstance(m, dict):
            continue
        clean = {str(k): v for k, v in m.items() if _finite_num(v)}
        if not clean:
            continue
        item = {"ts": float(ts), "m": clean}
        oi = s.get("oi")
        if isinstance(oi, dict):
            clean_oi = {str(k): v for k, v in oi.items() if _finite_num(v)}
            if clean_oi:
                item["oi"] = clean_oi
        fund = s.get("funding")
        if isinstance(fund, dict):
            clean_f = {}
            for k, v in fund.items():
                n = _num(v)
                if n is not None:
                    clean_f[str(k)] = n
            if clean_f:
                item["funding"] = clean_f
        marks.append(item)
    last_fire = {}
    for k, v in (st.get("last_fire") or {}).items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if v != v or v in (float("inf"), float("-inf")):
            continue
        last_fire[str(k)] = float(v)
    wakes = {}
    for k, v in (st.get("wakes") or {}).items():
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            wakes[str(k)] = v
    wakes_hour = {}
    for k, v in (st.get("wakes_hour") or {}).items():
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            wakes_hour[str(k)] = v
    last_fire_escalera = {}
    for k, v in (st.get("last_fire_escalera") or {}).items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if v != v or v in (float("inf"), float("-inf")):
            continue
        last_fire_escalera[str(k)] = float(v)
    watches = []
    for w in st.get("watches") or []:
        if isinstance(w, dict) and w.get("coin") and w.get("side") in ("long", "short") and w.get("status"):
            watches.append(w)
    cvd = {}
    raw_cvd = st.get("cvd") if isinstance(st.get("cvd"), dict) else {}
    for coin, book in raw_cvd.items():
        if not isinstance(book, dict):
            continue
        trades = []
        for t in book.get("trades") or []:
            if isinstance(t, dict) and t.get("side") in ("A", "B") and _finite_num(t.get("px")) and _finite_num(t.get("sz")):
                trades.append(t)
        cvd[str(coin)] = {"trades": trades[-400:]}
    cursor = st.get("early_cursor")
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        cursor = 0
    return {
        "marks": marks, "last_fire": last_fire, "last_fire_escalera": last_fire_escalera,
        "wakes": wakes, "wakes_hour": wakes_hour, "watches": watches, "cvd": cvd,
        "early_cursor": cursor,
    }


def log_webhook_mode():
    url = (os.environ.get("EXPLOSION_WEBHOOK_URL") or "").strip()
    auth = (os.environ.get("EXPLOSION_WEBHOOK_AUTH") or "").strip()
    key = (os.environ.get("EXPLOSION_WEBHOOK_KEY") or "").strip()
    if url and auth:
        log("webhook configurado (EXPLOSION_WEBHOOK_URL y EXPLOSION_WEBHOOK_AUTH presentes; valores ocultos)")
        return "ready"
    if url and key:
        log("webhook configurado (EXPLOSION_WEBHOOK_URL y EXPLOSION_WEBHOOK_KEY presentes; valores ocultos)")
        return "ready"
    missing = []
    if not url:
        missing.append("EXPLOSION_WEBHOOK_URL")
    if not auth:
        missing.append("EXPLOSION_WEBHOOK_AUTH")
    log("dry-run: faltan " + " y ".join(missing) + "; no se hará POST")
    return "dry_run"


def _webhook_mode_label():
    url = (os.environ.get("EXPLOSION_WEBHOOK_URL") or "").strip()
    auth = (os.environ.get("EXPLOSION_WEBHOOK_AUTH") or "").strip()
    key = (os.environ.get("EXPLOSION_WEBHOOK_KEY") or "").strip()
    if url and (auth or key):
        return "webhook configurado (secrets ocultos)"
    return "dry-run (faltan secrets; no se envía nada)"


def candles(coin, interval, bars):
    step = H if interval == "1h" else M15
    end = int(time.time() * 1000)
    data = post({"type": "candleSnapshot", "req": {
        "coin": coin, "interval": interval,
        "startTime": end - int(bars) * step * 1000, "endTime": end,
    }})
    if not isinstance(data, list):
        raise RuntimeError("respuesta de velas no es una lista")
    return data


def ntl(c):  # notional of a candle ~ base volume x typical price
    return float(c["v"]) * (float(c["h"]) + float(c["l"]) + float(c["c"])) / 3


def atr_pct(cs, i, period):
    """SMA of the last `period` true ranges, as a percent of this bar's close."""
    period = int(period)
    if i < period:
        return None
    trs = []
    for j in range(i - period + 1, i + 1):
        prev = float(cs[j - 1]["c"])
        h, l = float(cs[j]["h"]), float(cs[j]["l"])
        trs.append(max(h - l, abs(h - prev), abs(l - prev)))
    atr = sum(trs) / len(trs)
    ref = float(cs[i]["c"]) or float(cs[i]["o"])
    if not ref:
        return None
    return atr / ref * 100


def wick_ratio_pct(c):
    """Rejection wick, as a percent of the full range.

    Up bar: upper wick. Down bar: lower wick. A doji uses the larger wick.
    That is the wick in the direction of the impulse that did not hold,
    which is what "posible agotamiento" refers to.
    """
    o, h, l, cl = (float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"]))
    rng = h - l
    if rng <= 0:
        return 0.0
    upper = h - max(o, cl)
    lower = min(o, cl) - l
    if cl > o:
        wick = upper
    elif cl < o:
        wick = lower
    else:
        wick = max(upper, lower)
    return max(0.0, wick) / rng * 100


def is_major(asset, cfg):
    names = {str(c).upper() for c in cfg["major_coins"]}
    if str(asset.get("coin") or "").upper() in names:
        return True
    if asset.get("dex") == "main" and (asset.get("dayNtlVlm") or 0) >= cfg["major_day_ntl_usd"]:
        return True
    return False


def floor_for(asset, cfg):
    return float(cfg["floor_pct_major"] if is_major(asset, cfg) else cfg["floor_pct_other"])


def passes_liquidity(asset, cfg):
    if not asset.get("mark"):
        return False
    if (asset.get("dayNtlVlm") or 0) < cfg["liquidity_min_day_ntl_usd"]:
        return False
    if asset.get("dex") != "main" and (asset.get("oi_usd") or 0) < cfg["hip3_min_oi_usd"]:
        return False
    return True


def _bar_stats(cs, i, interval, cfg):
    if i < 1 or i >= len(cs):
        return None
    try:
        o, cl = float(cs[i]["o"]), float(cs[i]["c"])
        pct = (cl / o - 1) * 100 if o else 0.0
        vol = ntl(cs[i])
        wick = wick_ratio_pct(cs[i])
    except (TypeError, ValueError, KeyError):
        return None
    window = int(cfg["avg_window_hours"] if interval == "1h" else cfg["avg_window_15m"])
    prior = cs[max(0, i - window):i]
    avg = sum(ntl(c) for c in prior) / len(prior) if prior else 0.0
    mult = (vol / avg) if avg > 0 else None
    atr = None
    if interval == "1h":
        try:
            atr = atr_pct(cs, i, cfg["atr_period"])
        except (TypeError, ValueError, KeyError):
            atr = None
    min_i = int(cfg["atr_period"]) if interval == "1h" else 6
    vol_enough = len(prior) >= 6
    return {
        "i": i, "pct": pct, "vol": vol, "vol_mult": mult, "atr_pct": atr,
        "wick_ratio": wick, "enough": i >= min_i and vol_enough, "vol_enough": vol_enough,
        "bar_open_madrid": datetime.fromtimestamp(cs[i]["t"] / 1000, MADRID).strftime("%Y-%m-%d %H:%M"),
    }


def _volume_multiple_ok(stats, cfg):
    return stats["vol_mult"] is not None and stats["vol_mult"] >= cfg["vol_mult_min"]


def _is_hit(stats, interval, cfg, floor):
    """1h path is the ATR rule. 15m path is the early rule. Legacy is separate."""
    if not stats or not stats["enough"]:
        return False
    mult_ok = _volume_multiple_ok(stats, cfg)
    if interval == "1h":
        if stats["atr_pct"] is None:
            return False
        move_ok = abs(stats["pct"]) >= cfg["atr_mult"] * stats["atr_pct"] and abs(stats["pct"]) >= floor
        return move_ok and mult_ok and stats["vol"] >= cfg["vol_1h_min_usd"]
    move_ok = abs(stats["pct"]) >= cfg["pct_15m_min"]
    return move_ok and mult_ok and stats["vol"] >= cfg["vol_15m_min_usd"]


def _legacy_hit(stats, cfg):
    """Fixed 1h rule: |move| >= legacy_pct_1h and the same notional gates."""
    if not stats or not stats.get("vol_enough"):
        return False
    return (
        abs(stats["pct"]) >= cfg["legacy_pct_1h"]
        and _volume_multiple_ok(stats, cfg)
        and stats["vol"] >= cfg["vol_1h_min_usd"]
    )


def escalera_at(cs, i, cfg):
    """Staircase over `escalera_candles` 1h bars ending at i, including that bar.

    Cumulative move is last close / first open. An up move needs at least
    escalera_min_aligned green closes and that many higher highs; a down move
    mirrors it with red closes and lower lows.
    """
    n = int(cfg["escalera_candles"])
    if i < n - 1 or i >= len(cs):
        return None
    window = cs[i - n + 1:i + 1]
    try:
        first_o = float(window[0]["o"])
        last_c = float(window[-1]["c"])
    except (TypeError, ValueError, KeyError):
        return None
    if not first_o:
        return None
    pct = (last_c / first_o - 1) * 100
    up = pct >= 0
    aligned = 0
    extremes = 0
    for k, bar in enumerate(window):
        try:
            o, c = float(bar["o"]), float(bar["c"])
            h, l = float(bar["h"]), float(bar["l"])
        except (TypeError, ValueError, KeyError):
            return None
        if (c > o) if up else (c < o):
            aligned += 1
        prev = window[k - 1] if k else (cs[i - n] if i - n >= 0 else None)
        if prev is None:
            continue
        try:
            if up and h > float(prev["h"]):
                extremes += 1
            elif not up and l < float(prev["l"]):
                extremes += 1
        except (TypeError, ValueError, KeyError):
            return None
    need = int(cfg["escalera_min_aligned"])
    return {
        "hit": abs(pct) >= cfg["escalera_pct"] and aligned >= need and extremes >= need,
        "pct": pct,
        "close": last_c,
        "aligned": aligned,
        "extremes": extremes,
    }


def _annotate_bar(cs, s, interval, cfg, floor):
    if interval == "1h":
        esc = escalera_at(cs, s["i"], cfg)
        s["atr_hit"] = _is_hit(s, "1h", cfg, floor)
        s["legacy_hit"] = _legacy_hit(s, cfg)
        s["escalera_hit"] = bool(esc and esc["hit"])
        s["pct_3h"] = None if esc is None else esc["pct"]
        s["hit"] = s["atr_hit"] or s["legacy_hit"] or s["escalera_hit"]
    else:
        s["atr_hit"] = s["legacy_hit"] = s["escalera_hit"] = False
        s["pct_3h"] = None
        s["hit"] = _is_hit(s, "15m", cfg, floor)
    return s


def series_view(cs, interval, cfg, floor):
    """Metrics for the more violent of the last closed bar and the live bar."""
    if not isinstance(cs, list) or len(cs) < 2:
        return None
    stats = []
    for i in (len(cs) - 2, len(cs) - 1):
        s = _bar_stats(cs, i, interval, cfg)
        if not s:
            continue
        stats.append(_annotate_bar(cs, s, interval, cfg, floor))
    if not stats:
        return None
    display = max(stats, key=lambda s: abs(s["pct"]))
    hits = [s for s in stats if s["hit"]]
    chosen = max(hits, key=lambda s: abs(s["pct"])) if hits else None
    esc_hits = [s for s in stats if s["escalera_hit"] and s.get("pct_3h") is not None]
    if esc_hits:
        pct_3h = max(esc_hits, key=lambda s: abs(s["pct_3h"]))["pct_3h"]
    else:
        pct_3h = stats[-1].get("pct_3h")
    return {
        "pct": display["pct"],
        "vol": display["vol"],
        "vol_mult": display["vol_mult"],
        "atr_pct": display["atr_pct"] if interval == "1h" else None,
        "pct_3h": pct_3h,
        "wick_ratio": (chosen or display)["wick_ratio"],
        "hit": bool(chosen),
        "atr_hit": any(s["atr_hit"] for s in stats),
        "legacy_hit": any(s["legacy_hit"] for s in stats),
        "escalera_hit": any(s["escalera_hit"] for s in stats),
        "chosen": chosen,
        "bar_open_madrid": (chosen or display)["bar_open_madrid"],
    }


def trigger_names(view_1h, view_15m):
    names = []
    if view_1h and view_1h.get("atr_hit"):
        names.append("1h_atr")
    if view_1h and view_1h.get("legacy_hit"):
        names.append("1h_legacy")
    if view_15m and view_15m.get("hit"):
        names.append("15m_early")
    if view_1h and view_1h.get("escalera_hit"):
        names.append("3h_escalera")
    return names


def _pick_mark(marks, now, target, min_age, max_age):
    pool = [s for s in marks if min_age <= now - s["ts"] <= max_age]
    if not pool:
        return None
    return min(pool, key=lambda s: abs((now - s["ts"]) - target))


def _pct_between(mark, prev):
    if not mark or not prev:
        return None
    return (mark / prev - 1) * 100


def _funding_pct(asset):
    if asset.get("funding_pct") is not None:
        return asset["funding_pct"]
    raw = _num(asset.get("funding"))
    return round(raw * 100, 6) if raw is not None else None


def _premium_pct(asset):
    if asset.get("premium_pct") is not None:
        return asset["premium_pct"]
    raw = _num(asset.get("premium"))
    if raw is not None:
        return round(raw * 100, 4)
    mark, oracle = _num(asset.get("mark")), _num(asset.get("oracle"))
    if mark and oracle:
        return round((mark / oracle - 1) * 100, 4)
    return None


def _oi_change_pct(old, coin, oi_now):
    if not old or not isinstance(old.get("oi"), dict):
        return None
    prev = old["oi"].get(coin)
    if not prev or not oi_now:
        return None
    return (oi_now / prev - 1) * 100


def _fmt(v, nd=2):
    if v is None:
        return "—"
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return "—"
    if nd == 0:
        return f"{v:,.0f}"
    return f"{v:,.{nd}f}"


def _cell(text):
    return str(text).replace("|", "/").replace("\n", " ")


def render_scan_markdown(when, mode, n_perps, n_cands, n_series, n_alerts, n_sent, status, rows):
    lines = [
        "## Detector de explosiones",
        "",
        f"- Hora (Madrid): {when}",
        f"- Modo: {mode}",
        f"- Perps revisados: {n_perps}",
        f"- Candidatos: {n_cands}",
        f"- Series de velas leídas: {n_series}",
        f"- Alertas en esta tanda: {n_alerts}",
        f"- Alertas enviadas: {n_sent}",
        f"- Estado del envío: `{status}`",
        "",
        "| " + " | ".join(_SCAN_HEADERS) + " |",
        "| " + " | ".join("---" for _ in _SCAN_HEADERS) + " |",
    ]
    if not rows:
        lines.append("| " + " | ".join("—" for _ in _SCAN_HEADERS) + " |")
    for row in rows:
        lines.append("| " + " | ".join(_cell(row[h]) for h in _SCAN_HEADERS) + " |")
    lines.append("")
    lines.append("Resultado: alerta (se incluye en el aviso), no llega (no cumple), pausa (cooldown), silencio (00:00–08:00, solo anotada), tope (máximo del día o de la hora), sin velas (la API no contestó), fuera del top (pasó el filtro pero no entró en las velas de este ciclo).")
    return "\n".join(lines)


def _csv_cell(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.6g}"
    text = str(v)
    if text[:1] in "=+-@":
        return "'" + text
    return text


def _scans_dir():
    return STATE.parent / "scans"


def write_scans(when, day, markdown, rows, keep_days):
    directory = _scans_dir()
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write(directory / "latest.md", markdown + "\n")
    path = directory / f"{day}.csv"
    previous = []
    if path.exists():
        previous = path.read_text(encoding="utf-8").splitlines()
    header = ["ts_madrid", "dex", "coin", "pct_15m", "pct_1h", "pct_3h", "pct_24h", "vol_mult_1h",
              "day_ntl", "oi_usd", "funding_pct", "atr_pct", "floor_pct", "triggers", "result"]
    # Rewrite the file so a crash cannot leave a half line, and so the header exists once.
    body = []
    if previous:
        start = 1 if previous[0].startswith("ts_madrid") else 0
        body.extend(previous[start:])
    buf_rows = []
    for row in rows:
        buf_rows.append([
            when, row["dex"], row["coin"], _csv_cell(row["_pct_15m"]), _csv_cell(row["_pct_1h"]),
            _csv_cell(row["_pct_3h"]), _csv_cell(row["_pct_24h"]), _csv_cell(row["_vol_mult"]),
            _csv_cell(row["_day"]), _csv_cell(row["_oi"]), _csv_cell(row["_funding"]),
            _csv_cell(row["_atr"]), _csv_cell(row["_floor"]), row.get("_triggers") or "",
            row["resultado"],
        ])
    import io
    sio = io.StringIO()
    writer = csv.writer(sio, lineterminator="\n")
    writer.writerow(header)
    for line in body:
        if line.strip():
            sio.write(line + "\n")
    for item in buf_rows:
        writer.writerow(item)
    atomic_write(path, sio.getvalue())
    cutoff = datetime.strptime(day, "%Y-%m-%d").date() - timedelta(days=int(keep_days) - 1)
    for old in directory.glob("*.csv"):
        try:
            stamp = datetime.strptime(old.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if stamp < cutoff:
            old.unlink()


def _row_from(asset, floor, view_1h, view_15m, result):
    pct_15 = view_15m["pct"] if view_15m else None
    pct_1h = view_1h["pct"] if view_1h else None
    pct_3h = view_1h.get("pct_3h") if view_1h else None
    atr = vol_mult = None
    if view_1h:
        atr = view_1h.get("atr_pct")
        vol_mult = view_1h.get("vol_mult")
    names = trigger_names(view_1h, view_15m)
    disparo = "+".join(names) if names else "—"
    return {
        "dex": asset.get("dex") or "",
        "coin": asset.get("coin") or "",
        "15m %": _fmt(pct_15),
        "1h %": _fmt(pct_1h),
        "3h %": _fmt(pct_3h),
        "24h %": _fmt(asset.get("pct_24h")),
        "mult 1h": _fmt(vol_mult, 1),
        "vol 24h": _fmt(asset.get("dayNtlVlm"), 0),
        "OI": _fmt(asset.get("oi_usd"), 0),
        "funding %": _fmt(_funding_pct(asset), 5),
        "ATR %": _fmt(atr, 2),
        "suelo": _fmt(floor, 1),
        "disparo": disparo,
        "resultado": result,
        "_pct_15m": None if pct_15 is None else round(pct_15, 4),
        "_pct_1h": None if pct_1h is None else round(pct_1h, 4),
        "_pct_3h": None if pct_3h is None else round(pct_3h, 4),
        "_pct_24h": asset.get("pct_24h"),
        "_vol_mult": None if vol_mult is None else round(vol_mult, 4),
        "_day": asset.get("dayNtlVlm"),
        "_oi": asset.get("oi_usd"),
        "_funding": _funding_pct(asset),
        "_atr": None if atr is None else round(atr, 4),
        "_floor": floor,
        "_triggers": "+".join(names),
    }


def _finite_round(v, nd):
    n = _num(v)
    if n is None:
        return None
    return round(n, nd)


def build_alert(asset, floor, view_1h, view_15m, oi_change, nowm):
    fired = trigger_names(view_1h, view_15m)
    trigger = "+".join(fired)
    wicks = []
    for view in (view_1h, view_15m):
        if view and view["hit"]:
            wicks.append(view["wick_ratio"])
    wick = max(wicks) if wicks else (view_1h or view_15m or {}).get("wick_ratio")
    exhaustion = wick is not None and wick > asset.get("_exhaustion", 50)
    day = asset.get("dayNtlVlm") or 0
    tradable = day >= asset.get("_tradable_min", 0)
    chosen_1h = (view_1h or {}).get("chosen") or {}
    chosen_15 = (view_15m or {}).get("chosen") or {}
    atr = None
    if view_1h and view_1h.get("chosen") and view_1h["chosen"].get("atr_pct") is not None:
        atr = view_1h["chosen"]["atr_pct"]
    elif view_1h and view_1h.get("atr_pct") is not None:
        atr = view_1h["atr_pct"]
    return {
        "dex": asset.get("dex"),
        "coin": asset.get("coin"),
        "trigger": trigger,
        "pct_1h": _finite_round(chosen_1h.get("pct", (view_1h or {}).get("pct")), 2),
        "pct_15m": _finite_round(chosen_15.get("pct", (view_15m or {}).get("pct")), 2),
        "atr_pct": _finite_round(atr, 3),
        "floor_pct": floor,
        "vol_1h_usd": None if chosen_1h.get("vol") is None else round(chosen_1h.get("vol", 0)),
        "vol_mult": _finite_round(chosen_1h.get("vol_mult"), 2),
        "vol_15m_usd": None if chosen_15.get("vol") is None else round(chosen_15.get("vol", 0)),
        "vol_15m_mult": _finite_round(chosen_15.get("vol_mult"), 2),
        "mark": asset.get("mark"),
        "dayNtlVlm": asset.get("dayNtlVlm"),
        "oi_usd": asset.get("oi_usd"),
        "oi_change_1h_pct": _finite_round(oi_change, 2),
        "funding_pct": _funding_pct(asset),
        "premium_pct": _premium_pct(asset),
        "wick_ratio": _finite_round(wick, 1),
        "exhaustion_flag": bool(exhaustion),
        "exhaustion_label": "posible agotamiento" if exhaustion else None,
        "early_floor_pct": asset.get("_early_floor") if "15m_early" in fired else None,
        "pct_3h": _finite_round((view_1h or {}).get("pct_3h"), 2),
        "tradable": bool(tradable),
        "label": "escalera" if "3h_escalera" in fired else ("operable" if tradable else "vigilancia, no operable"),
        "maxLeverage": asset.get("maxLeverage"),
        "detected_at_madrid": nowm.strftime("%Y-%m-%d %H:%M"),
    }


def append_jsonl(path, record, limit=ALERT_KEEP):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if path.exists():
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    lines.append(json.dumps(record, ensure_ascii=False))
    if len(lines) > limit:
        lines = lines[-limit:]
    atomic_write(path, "\n".join(lines) + "\n")


def replay_candles(cs_1h, cs_15m, cfg, floor):
    """Every bar where a trigger is true. Cooldown is not applied.

    Returned times are the candle open in Europe/Madrid. `close` is that bar's close.
    """
    events = []
    for i, candle in enumerate(cs_1h or []):
        stats = _bar_stats(cs_1h, i, "1h", cfg)
        if not stats:
            continue
        esc = escalera_at(cs_1h, i, cfg)
        names = []
        if _is_hit(stats, "1h", cfg, floor):
            names.append("1h_atr")
        if _legacy_hit(stats, cfg):
            names.append("1h_legacy")
        if esc and esc["hit"]:
            names.append("3h_escalera")
        if not names:
            continue
        events.append({
            "interval": "1h",
            "madrid": stats["bar_open_madrid"],
            "open": float(candle["o"]),
            "close": float(candle["c"]),
            "pct": round(stats["pct"], 2),
            "vol_mult": None if stats["vol_mult"] is None else round(stats["vol_mult"], 2),
            "atr_pct": None if stats["atr_pct"] is None else round(stats["atr_pct"], 2),
            "pct_3h": None if esc is None else round(esc["pct"], 2),
            "triggers": names,
        })
    for i, candle in enumerate(cs_15m or []):
        stats = _bar_stats(cs_15m, i, "15m", cfg)
        if not stats or not _is_hit(stats, "15m", cfg, floor):
            continue
        events.append({
            "interval": "15m",
            "madrid": stats["bar_open_madrid"],
            "open": float(candle["o"]),
            "close": float(candle["c"]),
            "pct": round(stats["pct"], 2),
            "vol_mult": None if stats["vol_mult"] is None else round(stats["vol_mult"], 2),
            "triggers": ["15m_early"],
        })
    return events


def backtest(hours=48):
    cfg = load_cfg()
    assets = json.loads(PERPS.read_text(encoding="utf-8"))["assets"]
    fires = []
    bars = hours + cfg["avg_window_hours"] + cfg["atr_period"] + 2
    for a in assets:
        if not passes_liquidity(a, cfg):
            continue
        try:
            cs = candles(a["coin"], "1h", bars)
        except Exception as e:
            print("err", a["coin"], redact(e))
            continue
        floor = floor_for(a, cfg)
        cut = time.time() * 1000 - hours * H * 1000
        for i in range(len(cs)):
            if cs[i].get("t", 0) < cut:
                continue
            stats = _bar_stats(cs, i, "1h", cfg)
            if stats and _is_hit(stats, "1h", cfg, floor):
                fires.append({"coin": a["coin"], "dex": a["dex"], "floor_pct": floor, **{
                    k: stats[k] for k in ("pct", "vol", "vol_mult", "atr_pct", "bar_open_madrid")
                }})
        time.sleep(0.05)
    fires.sort(key=lambda x: x["bar_open_madrid"])
    for x in fires:
        print({k: x[k] for k in x if k != "vol_mult" or x[k] is None or abs(x[k]) < 1e9})
    print(f"TOTAL fires {len(fires)} en {hours}h sobre {len(assets)} perps -> {len(fires) * 24 / hours:.1f}/dia")
    atomic_write(ROOT / "data" / "detector_backtest.json", json.dumps(
        {"hours": hours, "fires": fires}, indent=1, ensure_ascii=False, default=str))


def _mark_ready(summary_line):
    atomic_write(STATE.parent / "run_summary.txt", summary_line + "\n")
    atomic_write(STATE.parent / "state_ready", "ok\n")


def _side_from_moves(pct_3h, pct_1h, pct_15m, fallback=None):
    ranked = [(abs(v), v) for v in (pct_3h, pct_1h, pct_15m) if isinstance(v, (int, float)) and v]
    if not ranked:
        return fallback
    value = max(ranked)[1]
    return "long" if value > 0 else "short"


def _bar_extreme(cs):
    if not cs:
        return None
    last = cs[-1]
    h, l, c = _num(last.get("h")), _num(last.get("l")), _num(last.get("c"))
    if h is None or l is None or c is None:
        return None
    return h, l, c


def evaluate_early(cs_1h, cs_15m, asset, old_snap, cvd_stats, cfg, btc_up=None, fear_greed=None):
    """Score one liquid name. Returns the confluence dict (hit may be false)."""
    if (asset.get("dayNtlVlm") or 0) < cfg["early_min_day_ntl_usd"]:
        return {"hit": False, "score": 0, "triggers": [], "side": None}
    parts = {}
    side = None
    if cs_15m and len(cs_15m) > int(cfg["vol_accel_avg_bars"]):
        got = early_signals.vol_accel(cs_15m, len(cs_15m) - 1, cfg, cfg["vol_accel_min_notional_15m"])
        if got.get("hit"):
            parts["vol_accel"] = True
    if not parts.get("vol_accel") and cs_1h and len(cs_1h) > int(cfg["vol_accel_avg_bars"]):
        got = early_signals.vol_accel(cs_1h, len(cs_1h) - 1, cfg, cfg["vol_accel_min_notional_1h"])
        if got.get("hit"):
            parts["vol_accel"] = True
    if cs_1h and len(cs_1h) > int(cfg["squeeze_bb_period"]) + int(cfg["squeeze_min_bars_inside"]):
        sq = early_signals.squeeze_breakout(cs_1h, len(cs_1h) - 1, cfg)
        if sq and sq.get("hit"):
            parts["squeeze"] = True
            side = sq.get("side")
    px_now = _num(asset.get("mark"))
    px_then = None
    oi_then = None
    fund_then = None
    if old_snap and px_now:
        px_then = _num((old_snap.get("m") or {}).get(asset["coin"]))
        oi_then = _num((old_snap.get("oi") or {}).get(asset["coin"]))
        fund_then = _num((old_snap.get("funding") or {}).get(asset["coin"]))
    price_pct = ((px_now / px_then - 1) * 100) if px_now and px_then else None
    oi = early_signals.oi_buildup(asset.get("oi_usd"), oi_then, px_now, px_then, cfg)
    if oi.get("hit"):
        parts["oi_buildup"] = True
    cvd = early_signals.cvd_absorption(cvd_stats, price_pct, cfg)
    if cvd.get("hit"):
        parts["cvd"] = True
        side = side or cvd.get("side")
    absorbed = {"hit": False}
    if cs_1h and len(cs_1h) > int(cfg["absorption_median_bars"]):
        # The live hour is still filling. The spike we care about is usually
        # the hour that just closed, so look at both, the same way the
        # explosion rules do.
        delta_side = cvd.get("side") if cvd.get("hit") else None
        candidates = [len(cs_1h) - 1]
        if len(cs_1h) >= 2:
            candidates.insert(0, len(cs_1h) - 2)
        for idx in candidates:
            got = early_signals.absorption(cs_1h, idx, cfg, delta_side=delta_side)
            if not got.get("hit"):
                continue
            rank = (1 if got.get("repeated") else 0, got.get("rvol") or 0)
            best = (1 if absorbed.get("repeated") else 0, absorbed.get("rvol") or 0)
            if not absorbed.get("hit") or rank > best:
                absorbed = got
        if absorbed.get("hit"):
            parts["absorption"] = True
            if absorbed.get("repeated"):
                parts["absorption_repeat"] = True
            # A breakout that already chose a side keeps it. Otherwise the
            # close location (or the taker imbalance) supplies the side.
            if side is None:
                side = absorbed.get("side")
    fund = early_signals.funding_shift(_funding_pct(asset), fund_then, cfg)
    if fund.get("hit"):
        parts["funding"] = True
    if side and early_signals.cvd_divergence(cvd_stats, price_pct, cfg):
        # Only count divergence when price itself moved against the flow.
        if price_pct is not None and abs(price_pct) >= float(cfg["cvd_max_price_pct"]):
            parts["divergence"] = True
    scored = early_signals.confluence(parts, cfg, side, btc_up, fear_greed)
    scored["absorption"] = absorbed if absorbed.get("hit") else None
    return scored


def _ingest_cvd(st, coins, now, cfg):
    """Fold the latest prints into a 2h window. recentTrades is only the last few fills."""
    window_ms = float(cfg["cvd_window_hours"]) * H * 1000
    books = st.setdefault("cvd", {})
    stats = {}
    for coin in coins[: int(cfg["cvd_max_coins"])]:
        try:
            trades = post({"type": "recentTrades", "coin": coin})
        except Exception as exc:
            log(f"cvd {coin}: {exc}")
            continue
        if not isinstance(trades, list):
            continue
        book = books.setdefault(coin, {"trades": []})
        seen = {t.get("tid") for t in book["trades"]}
        for t in trades:
            tid = t.get("tid")
            px, sz = _num(t.get("px")), _num(t.get("sz"))
            if tid in seen or px is None or sz is None or t.get("side") not in ("A", "B"):
                continue
            book["trades"].append({
                "tid": tid, "time": t.get("time") or int(now * 1000),
                "side": t["side"], "px": px, "sz": sz,
            })
            seen.add(tid)
        cutoff = now * 1000 - window_ms
        book["trades"] = [t for t in book["trades"] if (t.get("time") or 0) >= cutoff][-400:]
        stats[coin] = early_signals.cvd_from_trades(book["trades"])
    return stats


def _entry_payload(watch, nowm):
    payload = {
        "type": "entry_ready",
        "coin": watch["coin"],
        "dex": watch.get("dex"),
        "side": watch["side"],
        "entry": round(watch["fill"], 8),
        "sl": watch["sl"],
        "tp1": watch["tp1"],
        "tp2": watch["tp2"],
        "rr": round(watch["rr"], 2),
        "trigger": watch.get("trigger"),
        "detected_at_madrid": nowm.strftime("%Y-%m-%d %H:%M"),
    }
    if watch.get("urgency"):
        payload["urgency"] = watch["urgency"]
    if watch.get("kind"):
        payload["kind"] = watch["kind"]
    return payload


def run_cycle(cfg):
    """One live cycle. Returns True only after a good scan has been saved locally."""
    try:
        snap = scan()
    except Exception as e:
        log(f"lectura de perps fallida, estado NO modificado: {e}")
        return False
    assets = snap.get("assets") or []
    if not any(a.get("mark") for a in assets):
        log(f"sin precios utilizables, estado NO modificado: errores={snap.get('errors')}")
        return False
    if snap.get("errors"):
        log(f"lectura parcial, se continúa: errores={snap.get('errors')}")
    dexs = snap.get("dexs") or []
    log(f"perps={snap.get('count')} dexs={len(dexs)} nombres={','.join(map(str, dexs))}")

    st = load_state()
    now = time.time()
    nowm = datetime.now(MADRID)
    st["marks"] = [s for s in st["marks"] if now - s["ts"] < 25 * H]
    old = _pick_mark(
        st["marks"], now, cfg["prefilter_1h_target_sec"],
        cfg["prefilter_1h_min_age_sec"], cfg["prefilter_1h_max_age_sec"],
    )
    prev = _pick_mark(
        st["marks"], now, cfg["prefilter_15m_target_sec"],
        cfg["prefilter_15m_min_age_sec"], cfg["prefilter_15m_max_age_sec"],
    )
    old3 = _pick_mark(
        st["marks"], now, cfg["prefilter_3h_target_sec"],
        cfg["prefilter_3h_min_age_sec"], cfg["prefilter_3h_max_age_sec"],
    )
    ranked = []
    for a in assets:
        if not passes_liquidity(a, cfg):
            continue
        p1 = _pct_between(a["mark"], (old or {}).get("m", {}).get(a["coin"]))
        p15 = _pct_between(a["mark"], (prev or {}).get("m", {}).get(a["coin"]))
        p3 = _pct_between(a["mark"], (old3 or {}).get("m", {}).get(a["coin"]))
        hot = (
            (p15 is not None and abs(p15) >= cfg["prefilter_pct_15m"])
            or (p1 is not None and abs(p1) >= cfg["prefilter_pct_1h"])
            or (p3 is not None and abs(p3) >= cfg["prefilter_pct_3h"])
            or abs(a.get("pct_24h") or 0) >= cfg["prefilter_pct_24h"]
        )
        if not hot:
            continue
        score = max(abs(p15 or 0), abs(p1 or 0), abs(p3 or 0), abs(a.get("pct_24h") or 0))
        ranked.append((score, a))
    fresh_m, fresh_oi, fresh_f = {}, {}, {}
    for a in assets:
        if a.get("mark") and _finite_num(a["mark"]):
            fresh_m[a["coin"]] = a["mark"]
        if a.get("oi_usd") and _finite_num(a["oi_usd"]):
            fresh_oi[a["coin"]] = a["oi_usd"]
        fp = _funding_pct(a)
        if fp is not None:
            fresh_f[a["coin"]] = fp
    st["marks"].append({"ts": now, "m": fresh_m, "oi": fresh_oi, "funding": fresh_f})
    if len(st["marks"]) > 200:
        st["marks"] = st["marks"][-200:]
    ranked.sort(key=lambda x: x[0], reverse=True)
    top = ranked[: int(cfg["max_candle_calls"])]
    bars_1h = int(cfg["avg_window_hours"]) + int(cfg["atr_period"]) + 3
    bars_15 = int(cfg["avg_window_15m"]) + 8

    rows = []
    pending = []  # alerts that passed a trigger and are not in cooldown
    impulse = {}  # coin -> 1h candles used to build the watch
    extremes = {}
    n_series = 0
    liquid = [a for a in assets if (a.get("dayNtlVlm") or 0) >= cfg["early_min_day_ntl_usd"]]
    liquid.sort(key=lambda a: a.get("dayNtlVlm") or 0, reverse=True)
    seen_coins = {a["coin"] for _, a in top}
    early_pool = list(top)
    for a in liquid:
        if a["coin"] in seen_coins:
            continue
        if len(early_pool) >= len(top) + int(cfg["early_extra_names"]):
            break
        early_pool.append((0, a))
        seen_coins.add(a["coin"])
    # Flat names never enter the price prefilter. Rotate through every book
    # at or above 1M$ so a thin name like SOPH is still checked about once an hour.
    hour_only = set()
    rest = [a for a in liquid if a["coin"] not in seen_coins]
    rest.sort(key=lambda a: a["coin"])
    rotate_n = int(cfg["early_rotate_names"])
    if rest and rotate_n > 0:
        cursor = int(st.get("early_cursor") or 0) % len(rest)
        for k in range(min(rotate_n, len(rest))):
            picked = rest[(cursor + k) % len(rest)]
            early_pool.append((0, picked))
            hour_only.add(picked["coin"])
        st["early_cursor"] = (cursor + min(rotate_n, len(rest))) % len(rest)
    cvd_stats = {}
    try:
        cvd_coins = [a["coin"] for a in liquid[: int(cfg["cvd_max_coins"])]]
        cvd_stats = _ingest_cvd(st, cvd_coins, now, cfg)
    except Exception as exc:
        log(f"cvd: {exc}")
    for _, a in early_pool:
        floor = floor_for(a, cfg)
        view_1h = view_15 = None
        cs_h = cs_m = None
        try:
            need_1h = max(
                bars_1h,
                int(cfg["squeeze_bb_period"]) + int(cfg["squeeze_min_bars_inside"]) + 2,
                int(cfg["absorption_median_bars"]) + int(cfg["absorption_repeat_bars"]) + 2,
            )
            cs_h = candles(a["coin"], "1h", need_1h)
            view_1h = series_view(cs_h, "1h", cfg, floor)
            n_series += 1
        except Exception as e:
            log(f"velas 1h {a['coin']}: {e}")
        if a["coin"] not in hour_only:
            try:
                cs_m = candles(a["coin"], "15m", max(bars_15, int(cfg["vol_accel_avg_bars"]) + 2))
                view_15 = series_view(cs_m, "15m", cfg, floor)
                n_series += 1
                ext = _bar_extreme(cs_m)
                if ext:
                    extremes[a["coin"]] = ext
            except Exception as e:
                log(f"velas 15m {a['coin']}: {e}")
        early = evaluate_early(cs_h, cs_m, a, old, cvd_stats.get(a["coin"]), cfg)
        names = trigger_names(view_1h, view_15)
        if early.get("hit"):
            names = names + [n for n in early["triggers"] if n not in names]
        explosive = [n for n in names if n != "3h_escalera"]
        cool = cfg["cooldown_hours"] * H
        explosive_ready = bool(explosive) and now - st["last_fire"].get(a["coin"], 0) >= cool
        escalera_ready = "3h_escalera" in names and now - st["last_fire_escalera"].get(a["coin"], 0) >= cool
        if view_1h is None and view_15 is None:
            result = "sin velas"
        elif not names:
            result = "no llega"
        elif not explosive_ready and not escalera_ready:
            result = "pausa"
            log(f"en pausa {a['coin']}")
        else:
            result = "alerta"
        row = _row_from(a, floor, view_1h, view_15, result)
        if names:
            row["disparo"] = "+".join(names)
            row["_triggers"] = "+".join(names)
        rows.append(row)
        if result == "alerta":
            enriched = dict(a)
            enriched["_exhaustion"] = cfg["exhaustion_wick_pct"]
            enriched["_tradable_min"] = cfg["tradable_min_day_ntl_usd"]
            enriched["_early_floor"] = cfg["pct_15m_min"]
            alert = build_alert(enriched, floor, view_1h, view_15, _oi_change_pct(old, a["coin"], a.get("oi_usd")), nowm)
            if names:
                alert["trigger"] = "+".join(names)
            alert["_side"] = early.get("side") if early.get("hit") else None
            alert["_bars"] = (cs_h or cs_m or [])[-int(cfg["watch_impulse_bars"]):]
            names_set = set(names)
            impulse = names_set & {"1h_atr", "1h_legacy", "15m_early", "3h_escalera", "squeeze_breakout"}
            if early.get("hit") and "absorption" in names_set and not impulse:
                alert["urgency"] = "baja"
                alert["side"] = early.get("side")
                absorbed = early.get("absorption") or {}
                if absorbed.get("rvol") is not None:
                    alert["absorption_rvol"] = round(absorbed["rvol"], 2)
                alert["absorption_repeats"] = absorbed.get("repeats")
                alert["_range_low"] = absorbed.get("range_low")
                alert["_range_high"] = absorbed.get("range_high")
            pending.append(alert)
    done = {row["coin"] for row in rows}
    for _, a in ranked[int(cfg["max_candle_calls"]):]:
        if a["coin"] in done:
            continue
        rows.append(_row_from(a, floor_for(a, cfg), None, None, "fuera del top"))

    today = nowm.strftime("%Y-%m-%d")
    hour_key = nowm.strftime("%Y-%m-%dT%H")
    quiet = cfg["quiet_start_hour"] <= nowm.hour < cfg["quiet_end_hour"]
    day_n = st["wakes"].get(today, 0)
    hour_n = st["wakes_hour"].get(hour_key, 0)
    def _quiet_blocks(alert):
        if not quiet:
            return False
        names = set((alert.get("trigger") or "").split("+")) - {""}
        if names == {"3h_escalera"} and cfg.get("escalera_ignore_quiet_hours"):
            return False
        return True

    status = "none"
    sent_n = 0
    counted = 0
    sendable = [p for p in pending if not _quiet_blocks(p)]
    silenced = [p for p in pending if _quiet_blocks(p)]
    if silenced:
        silenced_coins = {p["coin"] for p in silenced}
        for row in rows:
            if row["coin"] in silenced_coins and row["resultado"] == "alerta":
                row["resultado"] = "silencio"
    logged = []
    if pending and not sendable:
        status = "quiet_hours_logged"
        logged = pending
    elif sendable:
        capped = day_n >= cfg["max_alerts_per_day"] or hour_n >= cfg["max_alerts_per_hour"]
        if capped:
            status = "hourly_cap_logged" if hour_n >= cfg["max_alerts_per_hour"] else "daily_cap_logged"
            for row in rows:
                if row["resultado"] == "alerta":
                    row["resultado"] = "tope"
        else:
            status = send([{k: v for k, v in p.items() if not k.startswith("_")} for p in sendable], cfg)
            if status in ("posted", "dry_run"):
                st["wakes"][today] = day_n + 1
                st["wakes_hour"][hour_key] = hour_n + 1
                for p in sendable:
                    kinds = set((p.get("trigger") or "").split("+"))
                    if kinds - {"3h_escalera"}:
                        st["last_fire"][p["coin"]] = now
                    if "3h_escalera" in kinds:
                        st["last_fire_escalera"][p["coin"]] = now
                sent_n = len(sendable) if status == "posted" else 0
                counted = len(sendable)
            else:
                counted = 0
        logged = sendable
    if logged:
        append_jsonl(ALERTS, {
            "at": nowm.isoformat(timespec="seconds"), "status": status,
            "alerts": [{k: v for k, v in p.items() if not k.startswith("_")} for p in logged],
        })
        for p in logged:
            log(
                f"alerta {p['coin']} dex={p['dex']} trigger={p['trigger']} "
                f"pct_1h={p['pct_1h']} pct_15m={p['pct_15m']} pct_3h={p.get('pct_3h')} "
                f"floor={p['floor_pct']} atr={p['atr_pct']} tradable={p['tradable']} "
                f"label={p['label']} exhaustion={p['exhaustion_flag']}"
            )
    # Phase 2 reads watches opened on earlier scans. New watches are added after,
    # so the impulse bar itself is not treated as the retest.
    allow_entry = not (quiet and cfg.get("entry_ready_respect_quiet_hours", True))
    entry_payloads = []
    kept = []
    for w in st.get("watches") or []:
        if w.get("status") != "open":
            if w.get("status") == "entry_sent" and now - w.get("entry_sent_at", now) < 7 * 86400:
                kept.append(w)
            continue
        ext = extremes.get(w["coin"])
        mark = fresh_m.get(w["coin"])
        if ext:
            high, low, close = ext
        elif mark is not None:
            high = low = close = mark
        else:
            high = low = close = None
        w, event = watchmod.advance_watch(w, high, low, close, now, cfg, allow_entry=allow_entry)
        if event == "entry_ready":
            entry_payloads.append(_entry_payload(w, nowm))
        if event == "quiet" and not w.get("quiet_noted"):
            log(f"entrada en silencio {w['coin']} {w['side']}")
            w["quiet_noted"] = True
        if w.get("status") in ("open", "entry_sent"):
            kept.append(w)
    open_keys = {(w["coin"], w["side"]): w for w in kept if w.get("status") == "open"}
    for alert in pending:
        if alert.get("urgency") == "baja" and alert.get("_side") in ("long", "short"):
            # The range has not broken yet. Direction is the close location
            # (or the taker imbalance), not the largest wiggle inside the range.
            side = alert["_side"]
        else:
            side = _side_from_moves(alert.get("pct_3h"), alert.get("pct_1h"), alert.get("pct_15m"), alert.get("_side"))
        if not side:
            continue
        absorption_watch = alert.get("urgency") == "baja" and alert.get("_range_low") is not None
        existing = open_keys.get((alert["coin"], side))
        if existing and not (absorption_watch and existing.get("kind") == "absorption"):
            continue
        if absorption_watch:
            built = watchmod.build_absorption_watch(
                alert["coin"], alert.get("dex"), side, alert.get("trigger"),
                alert.get("_range_low"), alert.get("_range_high"), now, cfg,
            )
        else:
            bars = alert.get("_bars") or []
            if len(bars) < 1:
                continue
            built = watchmod.build_watch(alert["coin"], alert.get("dex"), side, alert.get("trigger"), bars, now, cfg)
        if not built:
            continue
        if existing and existing.get("kind") == "absorption":
            # A later spike in the same campaign widens the range and restarts the clock.
            built["created_at"] = existing.get("created_at", now)
            for key, value in built.items():
                if key != "created_at":
                    existing[key] = value
            continue
        kept.append(built)
        open_keys[(alert["coin"], side)] = built
    st["watches"] = kept[-80:]
    if entry_payloads:
        entry_status = send(entry_payloads, cfg)
        append_jsonl(ALERTS, {
            "at": nowm.isoformat(timespec="seconds"), "status": entry_status, "alerts": entry_payloads,
        })
        for item in entry_payloads:
            log(
                f"entry_ready {item['coin']} {item['side']} entrada={item['entry']} "
                f"sl={item['sl']} tp1={item['tp1']} rr={item['rr']} trigger={item['trigger']}"
            )
    st["wakes"] = {k: v for k, v in st["wakes"].items() if k >= today}
    st["wakes_hour"] = {k: v for k, v in st["wakes_hour"].items() if k == hour_key}
    cooldown_s = cfg["cooldown_hours"] * H
    st["last_fire"] = {k: v for k, v in st["last_fire"].items() if now - v < cooldown_s}
    st["last_fire_escalera"] = {
        k: v for k, v in st["last_fire_escalera"].items() if now - v < cooldown_s
    }
    atomic_write(STATE, json.dumps(st, ensure_ascii=False, separators=(",", ":"), allow_nan=False))

    when = nowm.strftime("%Y-%m-%d %H:%M")
    mode = _webhook_mode_label()
    markdown = render_scan_markdown(
        when, mode, snap.get("count"), len(ranked), n_series, len(pending), sent_n, status, rows,
    )
    print(markdown, flush=True)
    write_summary(markdown.splitlines())
    try:
        write_scans(when, today, markdown, rows, cfg["scan_keep_days"])
    except Exception as e:
        log(f"no se pudo escribir el historial de scans: {e}")
    line = (
        f"candidatos={len(ranked)} velas={n_series} hits={len(pending)} "
        f"enviados={counted if pending else 0} status={status} {[p['coin'] for p in pending]}"
    )
    log(line)
    _mark_ready(line)
    return True


def send(payload, cfg):
    url = (os.environ.get("EXPLOSION_WEBHOOK_URL") or "").strip()
    key = (os.environ.get("EXPLOSION_WEBHOOK_KEY") or "").strip()
    auth = (os.environ.get("EXPLOSION_WEBHOOK_AUTH") or "").strip()
    if not url or not (key or auth):
        log("dry-run: faltan EXPLOSION_WEBHOOK_URL y/o EXPLOSION_WEBHOOK_AUTH; no se hace POST")
        return "dry_run"
    hdr = {"Content-Type": "application/json"}
    if auth:
        name, _, val = auth.partition(":")
        if val:
            hdr[name.strip()] = val.strip()
        else:
            hdr["Authorization"] = auth
    else:
        hdr[cfg["webhook_auth_header"]] = cfg["webhook_auth_format"].format(key=key)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    delay = 1.5
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, data=data, headers=hdr, method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                log(f"webhook HTTP {r.status}")
                return "posted"
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 3:
                log(f"webhook error: HTTP {e.code}")
                return "post_failed"
            log(f"webhook HTTP {e.code}, reintento {attempt}/3")
        except Exception as e:
            if attempt == 3:
                log(f"webhook error: {e}")
                return "post_failed"
            log(f"webhook error transitorio, reintento {attempt}/3: {e}")
        time.sleep(delay)
        delay = min(delay * 2, 15)
    log("webhook error: agotados los reintentos")
    return "post_failed"


def send_test_post(cfg):
    """Manual end-to-end POST. Does not spend caps, quiet hours, or a cooldown."""
    nowm = datetime.now(MADRID)
    payload = [{
        "test": True,
        "source": "hl-explosion-detector",
        "message": "POST de prueba manual",
        "detected_at_madrid": nowm.strftime("%Y-%m-%d %H:%M"),
    }]
    log("prueba manual: se intenta un POST aunque sea horario de silencio, tope o pausa")
    status = send(payload, cfg)
    log(f"force_test status={status}")
    try:
        append_jsonl(ALERTS, {
            "at": nowm.isoformat(timespec="seconds"),
            "status": f"test_{status}",
            "alerts": payload,
        })
    except Exception as e:
        log(f"no se pudo anotar la prueba en el historial: {e}")
    write_summary([f"- Prueba manual: `{status}`"])
    if status == "post_failed":
        return 1
    return 0


def live(force_test_post=False):
    log(f"inicio force_test={'si' if force_test_post else 'no'} python={sys.version.split()[0]}")
    ok = False
    cfg = None
    try:
        cfg = load_cfg()
        log_webhook_mode()
        ok = run_cycle(cfg)
        if not ok:
            write_summary([
                "## Detector de explosiones",
                "",
                "- No se han podido leer precios de Hyperliquid. El estado guardado no se ha modificado.",
            ])
    except Exception as e:
        log(f"fallo del ciclo, estado NO modificado: {e}")
        write_summary([
            "## Detector de explosiones",
            "",
            "- No se ha podido completar el ciclo. El estado guardado no se ha modificado.",
            f"- Detalle: {redact(e)}",
        ])
        ok = False
    test_rc = 0
    if force_test_post:
        if cfg is None:
            log("prueba manual cancelada: no se pudo leer la configuración")
            test_rc = 1
        else:
            test_rc = send_test_post(cfg)
    if not ok:
        return 1
    return test_rc


def env_flag(name):
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if "--backtest" in argv:
        arg = argv[-1]
        backtest(int(arg) if arg.isdigit() else 48)
        return 0
    force = env_flag("FORCE_TEST_POST") or "--force-test" in argv
    return live(force_test_post=force)


if __name__ == "__main__":
    sys.exit(main())
