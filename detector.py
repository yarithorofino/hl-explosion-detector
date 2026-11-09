#!/usr/bin/env python3
"""Explosion detector for Hyperliquid perps (main + HIP-3). Analysis only, never trades.

Live:     python3 detector.py
Backtest: python3 detector.py --backtest [hours]
Test POST: python3 detector.py --force-test

Trigger: |1h change| >= pct_1h_min AND 1h notional >= vol_mult_min x avg hourly
notional (prior avg_window_hours) AND 1h notional >= vol_1h_min_usd.
Config: data/detector_config.json.

Webhook URL and auth come from the environment (never from this file):
  EXPLOSION_WEBHOOK_URL
  EXPLOSION_WEBHOOK_AUTH   full header line, e.g. "Authorization: Bearer ..."
If either is missing the cycle still runs, but no POST is made (dry-run).
A failed read of Hyperliquid does not rewrite data/detector_state.json.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from perps_all import post, scan

ROOT = Path(__file__).resolve().parent
CFG = ROOT / "data" / "detector_config.json"
STATE = ROOT / "data" / "detector_state.json"
PERPS = ROOT / "data" / "perps_all.json"
ALERTS = ROOT / "signals" / "explosion_alerts.jsonl"
LOG = ROOT / "logs" / "detector.log"
MADRID = ZoneInfo("Europe/Madrid")
H = 3600
ALERT_KEEP = 300
_CFG_KEYS = (
    "pct_1h_min", "vol_mult_min", "vol_1h_min_usd", "avg_window_hours",
    "prefilter_pct_1h", "prefilter_pct_24h", "prefilter_min_day_ntl_usd",
    "max_candle_calls", "cooldown_hours", "max_wakes_per_day",
    "quiet_start_hour", "quiet_end_hour",
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


def load_cfg():
    if not CFG.exists():
        raise RuntimeError(f"falta {CFG}")
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    missing = [k for k in _CFG_KEYS if k not in cfg]
    if missing:
        raise RuntimeError("config sin " + ", ".join(missing))
    return cfg


def load_state():
    if not STATE.exists() or STATE.stat().st_size == 0:
        return {"marks": [], "last_fire": {}, "wakes": {}}
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
        if clean:
            marks.append({"ts": float(ts), "m": clean})
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
    return {"marks": marks, "last_fire": last_fire, "wakes": wakes}


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


def candles(coin, hours):
    end = int(time.time() * 1000)
    data = post({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h",
                 "startTime": end - hours * H * 1000, "endTime": end}})
    if not isinstance(data, list):
        raise RuntimeError("respuesta de velas no es una lista")
    return data


def ntl(c):  # notional of a candle ~ base volume x typical price
    return float(c["v"]) * (float(c["h"]) + float(c["l"]) + float(c["c"])) / 3


def eval_bar(cs, i, cfg):
    """Evaluate bar i against prior window. Returns metrics dict or None."""
    w = cfg["avg_window_hours"]
    if i < 6: return None
    prior = cs[max(0, i - w):i]
    avg = sum(ntl(c) for c in prior) / len(prior)
    o, c = float(cs[i]["o"]), float(cs[i]["c"])
    pct = (c / o - 1) * 100 if o else 0
    v = ntl(cs[i]); mult = v / avg if avg > 0 else float("inf")
    hit = abs(pct) >= cfg["pct_1h_min"] and mult >= cfg["vol_mult_min"] and v >= cfg["vol_1h_min_usd"]
    return {"pct_1h": round(pct, 2), "vol_1h_usd": round(v), "vol_mult": round(mult, 1), "hit": hit,
            "bar_open_madrid": datetime.fromtimestamp(cs[i]["t"] / 1000, MADRID).strftime("%Y-%m-%d %H:%M")}


def append_jsonl(path, record, limit=ALERT_KEEP):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if path.exists():
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    lines.append(json.dumps(record, ensure_ascii=False))
    if len(lines) > limit:
        lines = lines[-limit:]
    atomic_write(path, "\n".join(lines) + "\n")


def backtest(hours=48):
    cfg = load_cfg()
    assets = json.loads(PERPS.read_text(encoding="utf-8"))["assets"]
    fires = []
    for a in assets:
        try:
            cs = candles(a["coin"], hours + cfg["avg_window_hours"] + 1)
        except Exception as e:
            print("err", a["coin"], redact(e))
            continue
        cut = time.time() * 1000 - hours * H * 1000
        for i in range(len(cs)):
            if cs[i]["t"] < cut:
                continue
            m = eval_bar(cs, i, cfg)
            if m and m["hit"]:
                fires.append({"coin": a["coin"], **m})
        time.sleep(0.05)
    fires.sort(key=lambda x: x["bar_open_madrid"])
    for x in fires:
        print(x)
    print(f"TOTAL fires {len(fires)} en {hours}h sobre {len(assets)} perps -> {len(fires)*24/hours:.1f}/dia")
    out = ROOT / "data" / "detector_backtest.json"
    atomic_write(out, json.dumps({"hours": hours, "cfg": cfg, "fires": fires}, indent=1, ensure_ascii=False))


def _mark_ready(summary_line):
    # Follow STATE so tests that redirect the state file do not write into the repo.
    atomic_write(STATE.parent / "run_summary.txt", summary_line + "\n")
    atomic_write(STATE.parent / "state_ready", "ok\n")


def run_cycle(cfg):
    """One live cycle. Returns True only after a good scan has been saved locally.

    A scan with no usable prices leaves the state file untouched.
    """
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
    old = min(
        (s for s in st["marks"] if now - s["ts"] >= 50 * 60),
        key=lambda s: abs(now - s["ts"] - H),
        default=None,
    )
    cands = []
    for a in assets:
        if not a["mark"] or (a["dayNtlVlm"] or 0) < cfg["prefilter_min_day_ntl_usd"]:
            continue
        p1 = None
        if old and old["m"].get(a["coin"]):
            p1 = (a["mark"] / old["m"][a["coin"]] - 1) * 100
        hot = (p1 is not None and abs(p1) >= cfg["prefilter_pct_1h"]) or abs(a["pct_24h"] or 0) >= cfg["prefilter_pct_24h"]
        if hot:
            cands.append((abs(p1 or 0), abs(a["pct_24h"] or 0), a))
    fresh = {}
    for a in assets:
        if a.get("mark") and _finite_num(a["mark"]):
            fresh[a["coin"]] = a["mark"]
    st["marks"].append({"ts": now, "m": fresh})
    if len(st["marks"]) > 200:
        st["marks"] = st["marks"][-200:]
    cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
    hits = []
    n_velas = min(len(cands), int(cfg["max_candle_calls"]))
    for _, _, a in cands[: int(cfg["max_candle_calls"])]:
        try:
            cs = candles(a["coin"], cfg["avg_window_hours"] + 3)
        except Exception as e:
            log(f"velas {a['coin']}: {e}")
            continue
        best = None
        for i in (len(cs) - 2, len(cs) - 1):  # last closed bar + bar in progress
            m = eval_bar(cs, i, cfg) if i >= 0 else None
            if m and m["hit"]:
                best = m
        if best:
            hits.append((a, best))
    today = nowm.strftime("%Y-%m-%d")
    quiet = cfg["quiet_start_hour"] <= nowm.hour < cfg["quiet_end_hour"]
    payload = []
    for a, m in hits:
        if now - st["last_fire"].get(a["coin"], 0) < cfg["cooldown_hours"] * H:
            log(f"en pausa {a['coin']}")
            continue
        payload.append({
            "dex": a["dex"], "coin": a["coin"], "pct_1h": m["pct_1h"], "vol_1h_usd": m["vol_1h_usd"],
            "vol_mult": m["vol_mult"], "mark": a["mark"], "dayNtlVlm": a["dayNtlVlm"], "oi_usd": a["oi_usd"],
            "maxLeverage": a["maxLeverage"], "detected_at_madrid": nowm.strftime("%Y-%m-%d %H:%M"),
        })
    status = "none"
    if payload:
        if quiet:
            status = "quiet_hours_logged"
        elif st["wakes"].get(today, 0) >= cfg["max_wakes_per_day"]:
            status = "daily_cap_logged"
        else:
            status = send(payload, cfg)
            if status in ("posted", "dry_run"):
                st["wakes"][today] = st["wakes"].get(today, 0) + 1
                for p in payload:
                    st["last_fire"][p["coin"]] = now
        append_jsonl(ALERTS, {
            "at": nowm.isoformat(timespec="seconds"), "status": status, "alerts": payload,
        })
        for p in payload:
            log(f"alerta {p['coin']} dex={p['dex']} pct_1h={p['pct_1h']} vol_1h_usd={p['vol_1h_usd']} vol_mult={p['vol_mult']}")
    cooldown_s = cfg["cooldown_hours"] * H
    st["wakes"] = {k: v for k, v in st["wakes"].items() if k >= today}
    st["last_fire"] = {k: v for k, v in st["last_fire"].items() if now - v < cooldown_s}
    atomic_write(STATE, json.dumps(st, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    line = (
        f"candidatos={len(cands)} velas={n_velas} hits={len(hits)} "
        f"enviados={len(payload)} status={status} {[p['coin'] for p in payload]}"
    )
    log(line)
    _mark_ready(line)
    _summary(_webhook_mode_label(), snap.get("count"), cands, n_velas, hits, payload, status)
    return True


def _webhook_mode_label():
    url = (os.environ.get("EXPLOSION_WEBHOOK_URL") or "").strip()
    auth = (os.environ.get("EXPLOSION_WEBHOOK_AUTH") or "").strip()
    key = (os.environ.get("EXPLOSION_WEBHOOK_KEY") or "").strip()
    if url and (auth or key):
        return "webhook configurado (secrets ocultos)"
    return "dry-run (faltan secrets; no se envía nada)"


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
    """Manual end-to-end POST. Does not spend the daily wake budget or a cooldown."""
    nowm = datetime.now(MADRID)
    payload = [{
        "test": True,
        "source": "hl-explosion-detector",
        "message": "POST de prueba manual",
        "detected_at_madrid": nowm.strftime("%Y-%m-%d %H:%M"),
    }]
    log("prueba manual: se intenta un POST aunque sea horario de silencio o no haya señales")
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


def _summary(mode_label, snap_count, cands, n_velas, hits, payload, status):
    lines = [
        "## Detector de explosiones",
        "",
        f"- Modo: {mode_label}",
        f"- Perps leídos: {snap_count}",
        f"- Candidatos: {len(cands)}",
        f"- Revisados con velas: {n_velas}",
        f"- Disparos: {len(hits)}",
        f"- En el aviso: {len(payload)}",
        f"- Estado del envío: `{status}`",
    ]
    if payload:
        lines.append("- Activos: " + ", ".join(p["coin"] for p in payload))
    else:
        lines.append("- Activos: ninguno")
    write_summary(lines)


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
