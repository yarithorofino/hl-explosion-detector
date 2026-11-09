#!/usr/bin/env python3
"""Full perp scan: main dex + every HIP-3 dex (perpDexs). One metaAndAssetCtxs call per dex.
Writes data/perps_all.json. Perps only, analysis only.

Info requests share 1200 weight/minute per IP. GitHub runners share that IP with
other jobs, so calls are spaced and 429/5xx are retried with backoff.
"""
import http.client
import json
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "perps_all.json"
HL = "https://api.hyperliquid.xyz/info"
MADRID = ZoneInfo("Europe/Madrid")
UA = "hl-explosion-detector/1.0 (public info API)"
# Stay under the documented 1200/min. The runner IP is shared, so leave headroom.
BUDGET = 800
WINDOW = 60.0
MIN_GAP = 0.15

_spent = []  # (monotonic time, weight)
_blocked_until = 0.0
_next_ok = 0.0


def _body_weight(body):
    # candleSnapshot is 20 plus 1 per 60 candles. A ~27h window is one extra unit.
    if body.get("type") == "candleSnapshot":
        return 22
    return 20


def _label(body):
    kind = body.get("type") or "info"
    if kind == "candleSnapshot":
        coin = (body.get("req") or {}).get("coin")
        return f"candleSnapshot {coin}" if coin else kind
    if body.get("dex"):
        return f"{kind} {body['dex']}"
    return kind


def _acquire(weight):
    """One wait at most, then send. Never loops if the clock does not move."""
    global _blocked_until, _spent, _next_ok
    now = time.time()
    if _blocked_until > now:
        time.sleep(min(_blocked_until - now, 30))
        now = time.time()
    if now < _next_ok:
        time.sleep(_next_ok - now)
        now = time.time()
    _next_ok = now + MIN_GAP
    _spent = [(t, w) for t, w in _spent if now - t < WINDOW]
    used = sum(w for _, w in _spent)
    if used + weight > BUDGET and _spent:
        wait = min(WINDOW - (now - _spent[0][0]) + 0.2, WINDOW)
        if wait > 0:
            print(f"hyperliquid: pausa {wait:.0f}s para no pasar el cupo de peso", flush=True)
            time.sleep(wait)
            now = time.time()
            _spent = [(t, w) for t, w in _spent if now - t < WINDOW]
    _spent.append((time.time(), weight))


def _retry_wait(exc, delay):
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
        ra = exc.headers.get("Retry-After") if exc.headers else None
        if ra:
            try:
                return min(float(ra), 30) + random.uniform(0, 0.5)
            except ValueError:
                pass
        return min(max(delay, 5.0), 30) + random.uniform(0, 0.5)
    return delay + random.uniform(0, 0.4)


def post(body, timeout=30, attempts=4):
    data = json.dumps(body).encode()
    delay = 1.5
    last = None
    label = _label(body)
    for attempt in range(1, attempts + 1):
        _acquire(_body_weight(body))
        req = urllib.request.Request(
            HL,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": UA},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.load(r)
            if isinstance(payload, dict):
                err = payload.get("error")
                if not err and payload.get("status") == "err":
                    err = payload.get("response") or "error"
                if err:
                    raise RuntimeError(f"hyperliquid: {err}"[:300])
            return payload
        except urllib.error.HTTPError as e:
            last = e
            retryable = e.code in (429, 500, 502, 503, 504)
            if not retryable or attempt == attempts:
                raise
            wait = _retry_wait(e, delay)
            if e.code == 429:
                global _blocked_until
                _blocked_until = time.time() + wait
            print(
                f"hyperliquid {label} HTTP {e.code}, espero {wait:.1f}s ({attempt}/{attempts})",
                flush=True,
            )
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                ConnectionError, OSError, http.client.HTTPException) as e:
            last = e
            if attempt == attempts:
                raise
            wait = _retry_wait(e, delay)
            print(
                f"hyperliquid {label} error de red, espero {wait:.1f}s ({attempt}/{attempts})",
                flush=True,
            )
        time.sleep(wait)
        delay = min(delay * 2, 15)
    raise last


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _write_snap(snap):
    """Persist the snapshot. A failed or empty scan must not erase a previous good file."""
    if not snap["assets"] and snap["errors"]:
        return
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        tmp = OUT.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        tmp.replace(OUT)
    except OSError as e:
        snap["errors"].append(f"escritura perps_all: {e}")


def scan():
    errors, assets = [], []
    try:
        dexs = [""] + [d["name"] for d in post({"type": "perpDexs"}) if d]
    except Exception as e:
        errors.append(f"perpDexs: {e}")
        dexs = [""]
    for dex in dexs:
        body = {"type": "metaAndAssetCtxs"}
        if dex:
            body["dex"] = dex
        try:
            meta, ctxs = post(body)
        except Exception as e:
            errors.append(f"{dex or 'main'}: {e}")
            continue
        if not isinstance(meta, dict) or not isinstance(ctxs, list):
            errors.append(f"{dex or 'main'}: respuesta inesperada")
            continue
        for u, c in zip(meta.get("universe", []), ctxs):
            if u.get("isDelisted"):
                continue
            mark, prev = f(c.get("markPx")), f(c.get("prevDayPx"))
            oi = f(c.get("openInterest"))
            assets.append({
                "dex": dex or "main", "coin": u["name"], "mark": mark, "prevDayPx": prev,
                "pct_24h": round((mark / prev - 1) * 100, 2) if mark and prev else None,
                "dayNtlVlm": f(c.get("dayNtlVlm")), "oi_coins": oi,
                "oi_usd": round(oi * mark, 2) if oi is not None and mark else None,
                "funding": f(c.get("funding")), "maxLeverage": u.get("maxLeverage"),
            })
    now = datetime.now(MADRID)
    snap = {"collected_at": now.isoformat(timespec="seconds"), "ts": int(time.time()),
            "dexs": [d or "main" for d in dexs], "count": len(assets), "errors": errors, "assets": assets}
    _write_snap(snap)
    return snap


if __name__ == "__main__":
    s = scan()
    print(f"perps_all: {s['count']} perps en {len(s['dexs'])} dexs, errores={s['errors']}")
    if not s["assets"]:
        sys.exit(1)
