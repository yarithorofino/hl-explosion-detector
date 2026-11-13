#!/usr/bin/env python3
"""One live alert: three 1h confluences, scored on a rolling hour.

The rolling hour is the last four closed 15-minute candles. It is compared
with the closed 1h candles that end at or before that window starts. At the
top of the hour this is the hour that just closed. At :15, :30 and :45 it is
still a full hour of price and volume, so a breakout is visible within 15
minutes and the 1.5× volume test uses the same notional as the previous 20
hours.

A forming 1h candle with the volume scaled up by 60/elapsed is not used.
Early in the hour that turns one 15-minute burst into a fake hourly climax.

Volume delta is not on candleSnapshot. recentTrades is only the last few
prints, so it is not treated as the hour. The tape (trade_tape.py) stores
aggressor notional from the public trades websocket. Coverage for a coin
starts when that subscription is acknowledged, not when it is sent. If the
tape did not cover the hour, the coin does not pass.
"""
import json
from pathlib import Path

import setup_scanner
import watch as watchmod

H_MS = 3_600_000
M15_MS = 900_000
KEEP_MS = 8 * H_MS
TIME_SLACK_MS = 1_500
GAP_TOLERANCE_MS = 1_000

LOG_STATUSES = {
    "alerta", "extendido", "sin_delta", "sin_presion", "veto_funding",
}


def _f(value):
    return setup_scanner._f(value)


def bars_1h(cfg):
    return (
        int(cfg["confluence_ema_slow"])
        + int(cfg["confluence_donchian"])
        + int(cfg["atr_period"])
        + 10
    )


def bars_15m(cfg):
    return 8


def in_universe(asset, cfg):
    """5M$ 24h notional. HIP-3 also needs 1M$ of open interest. Never spot."""
    if not asset:
        return False
    coin = str(asset.get("coin") or "")
    if not coin or "/" in coin:
        return False
    day = _f(asset.get("dayNtlVlm"))
    if day is None or day < float(cfg["confluence_day_ntl_usd"]):
        return False
    dex = asset.get("dex") or "main"
    if dex != "main":
        oi = _f(asset.get("oi_usd"))
        if oi is None or oi < float(cfg["confluence_hip3_min_oi_usd"]):
            return False
    return True


def funding_allows_short(asset, cfg):
    """Shorts need funding strictly above the floor. A missing rate is a veto."""
    value = _f(None if asset is None else asset.get("funding_pct"))
    if value is None:
        return False
    return value > float(cfg["short_funding_min_pct"])


def _stamp(candle):
    raw = candle.get("t") if isinstance(candle, dict) else None
    value = _f(raw)
    if value is None:
        return None
    return int(value)


def _ordered(candles):
    rows = []
    for candle in candles or []:
        if not isinstance(candle, dict):
            continue
        stamp = _stamp(candle)
        if stamp is None:
            continue
        rows.append((stamp, candle))
    rows.sort(key=lambda item: item[0])
    out = []
    for stamp, candle in rows:
        if out and _stamp(out[-1]) == stamp:
            out[-1] = candle
        else:
            out.append(candle)
    return out


def rolling_hour(cs_15m, now_ms):
    """The last four closed 15m candles, or None when they are not a full hour."""
    now_ms = int(now_ms)
    closed = []
    for candle in _ordered(cs_15m):
        stamp = _stamp(candle)
        if stamp + M15_MS <= now_ms + TIME_SLACK_MS:
            closed.append(candle)
    if len(closed) < 4:
        return None
    last = closed[-4:]
    start = _stamp(last[0])
    for i, candle in enumerate(last):
        if abs(_stamp(candle) - (start + i * M15_MS)) > GAP_TOLERANCE_MS:
            return None
    end = _stamp(last[-1]) + M15_MS
    if now_ms - end > 20 * 60 * 1000:
        return None
    high = low = None
    notionals = []
    for candle in last:
        h, lo = _f(candle.get("h")), _f(candle.get("l"))
        ntl = setup_scanner.bar_notional(candle)
        if h is None or lo is None or ntl is None:
            return None
        high = h if high is None else max(high, h)
        low = lo if low is None else min(low, lo)
        notionals.append(ntl)
    opened, close = _f(last[0].get("o")), _f(last[-1].get("c"))
    if opened is None or close is None or high is None or low is None:
        return None
    return {
        "t": start, "end": end, "o": opened, "h": high, "l": low, "c": close,
        "ntl": sum(notionals),
    }


def prior_hours(cs_1h, window_start_ms):
    """Closed 1h candles that finish at or before the rolling window starts."""
    window_start_ms = int(window_start_ms)
    out = []
    for candle in _ordered(cs_1h):
        stamp = _stamp(candle)
        if stamp + H_MS <= window_start_ms + TIME_SLACK_MS:
            out.append(candle)
    return out


def _contiguous(candles, step_ms):
    stamps = [_stamp(c) for c in candles]
    for prev, nxt in zip(stamps, stamps[1:]):
        if abs((nxt - prev) - step_ms) > GAP_TOLERANCE_MS:
            return False
    return True


def _donchian(candles, period, kind):
    if len(candles) < period:
        return None
    vals = []
    for candle in candles[-period:]:
        value = _f(candle.get("h") if kind == "high" else candle.get("l"))
        if value is None:
            return None
        vals.append(value)
    return max(vals) if kind == "high" else min(vals)


def _notional(candle, fallback=None):
    if isinstance(candle, dict) and candle.get("ntl") is not None:
        return _f(candle.get("ntl"))
    got = setup_scanner.bar_notional(candle)
    return got if got is not None else fallback


def merge_intervals(intervals):
    rows = []
    for pair in intervals or []:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        start, end = _f(pair[0]), _f(pair[1])
        if start is None or end is None or end <= start:
            continue
        rows.append((start, end))
    rows.sort()
    merged = []
    for start, end in rows:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def coverage_stats(intervals, start, end):
    """Return (ratio, max_gap_ms) of [start, end] covered by the intervals."""
    start, end = _f(start), _f(end)
    if start is None or end is None or end <= start:
        return 0.0, 0.0
    span = end - start
    covered = 0.0
    cursor = start
    max_gap = 0.0
    for left, right in merge_intervals(intervals):
        left = max(left, start)
        right = min(right, end)
        if right <= start or left >= end:
            continue
        if left > cursor:
            max_gap = max(max_gap, left - cursor)
        covered += max(0.0, right - left)
        cursor = max(cursor, right)
    if cursor < end:
        max_gap = max(max_gap, end - cursor)
    return covered / span, max_gap


def empty_delta():
    return {"cov": {}, "flow": {}}


def _as_run(value):
    text = str(value or "").strip()
    if not text or len(text) > 40:
        return None
    allowed = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-"
    if any(ch not in allowed for ch in text):
        return None
    return text


def _clean_span(pair):
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return None
    start, end = _f(pair[0]), _f(pair[1])
    if start is None or end is None or end <= start:
        return None
    return [start, end]


def clean_delta(raw):
    """Drop anything that is not coverage plus per-run aggressor notional."""
    if not isinstance(raw, dict):
        return empty_delta()
    cov = {}
    for coin, runs in (raw.get("cov") or {}).items():
        if not isinstance(runs, dict):
            continue
        cleaned = {}
        for run, spans in runs.items():
            run_id = _as_run(run)
            if run_id is None or not isinstance(spans, list):
                continue
            kept = [span for span in (_clean_span(item) for item in spans) if span]
            if kept:
                cleaned[run_id] = kept[-8:]
        if cleaned:
            cov[str(coin)] = cleaned
    flow = {}
    for coin, buckets in (raw.get("flow") or {}).items():
        if not isinstance(buckets, dict):
            continue
        cleaned_buckets = {}
        for key, runs in buckets.items():
            bucket = _f(key)
            if bucket is None or bucket < 0 or not isinstance(runs, dict):
                continue
            cleaned_runs = {}
            for run, row in runs.items():
                run_id = _as_run(run)
                if run_id is None or not isinstance(row, dict):
                    continue
                buy, sell = _f(row.get("buy")), _f(row.get("sell"))
                if buy is None or sell is None or buy < 0 or sell < 0:
                    continue
                cleaned_runs[run_id] = {"buy": buy, "sell": sell}
            if cleaned_runs:
                cleaned_buckets[str(int(bucket))] = cleaned_runs
        if cleaned_buckets:
            flow[str(coin)] = cleaned_buckets
    return {"cov": cov, "flow": flow}


def merge_tape(state_delta, tape, now_ms):
    """Fold one tape snapshot in. The same run replaces; another run adds."""
    now_ms = int(now_ms)
    merged = clean_delta(state_delta)
    if not isinstance(tape, dict):
        return _prune_delta(merged, now_ms)
    run = _as_run(tape.get("run"))
    updated = _f(tape.get("updated_ms"))
    if run is None or updated is None:
        return _prune_delta(merged, now_ms)
    updated = min(updated, now_ms + 60_000)
    for coin, spans in (tape.get("cov") or {}).items():
        if not isinstance(spans, list):
            continue
        kept = []
        for span in spans:
            cleaned = _clean_span(span)
            if cleaned is None:
                continue
            start, end = cleaned
            end = min(end, updated)
            if end > start:
                kept.append([start, end])
        if kept:
            merged["cov"].setdefault(str(coin), {})[run] = kept[-8:]
    for coin, buckets in (tape.get("flow") or {}).items():
        if not isinstance(buckets, dict):
            continue
        dest = merged["flow"].setdefault(str(coin), {})
        for key, row in buckets.items():
            bucket = _f(key)
            if bucket is None or bucket < 0 or not isinstance(row, dict):
                continue
            buy, sell = _f(row.get("buy")), _f(row.get("sell"))
            if buy is None or sell is None or buy < 0 or sell < 0:
                continue
            dest.setdefault(str(int(bucket)), {})[run] = {"buy": buy, "sell": sell}
    return _prune_delta(merged, now_ms)


def _prune_delta(delta, now_ms):
    cutoff = int(now_ms) - KEEP_MS
    cov = {}
    for coin, runs in delta.get("cov", {}).items():
        cleaned = {}
        for run, spans in runs.items():
            kept = [span for span in spans if span[1] >= cutoff]
            if kept:
                cleaned[run] = kept
        if cleaned:
            cov[coin] = cleaned
    flow = {}
    for coin, buckets in delta.get("flow", {}).items():
        cleaned = {}
        for key, runs in buckets.items():
            if _f(key) is None or _f(key) < cutoff:
                continue
            cleaned[key] = runs
        if cleaned:
            flow[coin] = cleaned
    return {"cov": cov, "flow": flow}


def measure_delta(delta, coin, start, end, cfg):
    """Aggressor notional inside [start, end), if the tape actually watched it."""
    delta = clean_delta(delta)
    spans = []
    for run_spans in (delta["cov"].get(coin) or {}).values():
        spans.extend(run_spans)
    ratio, gap = coverage_stats(spans, start, end)
    min_ratio = float(cfg["confluence_delta_min_coverage"])
    max_gap = float(cfg["confluence_delta_max_gap_sec"]) * 1000.0
    buy = sell = 0.0
    book = delta["flow"].get(coin) or {}
    bucket = (int(start) // M15_MS) * M15_MS
    while bucket + M15_MS <= int(end) + GAP_TOLERANCE_MS:
        if bucket >= int(start) - GAP_TOLERANCE_MS:
            for part in (book.get(str(int(bucket))) or {}).values():
                buy += part["buy"]
                sell += part["sell"]
        bucket += M15_MS
    ok = bool(spans) and ratio + 1e-12 >= min_ratio and gap <= max_gap + 1e-6
    reason = ""
    if not spans:
        reason = "sin cinta"
    elif not ok:
        bits = []
        if ratio + 1e-12 < min_ratio:
            bits.append(f"cobertura {ratio:.2f}")
        if gap > max_gap + 1e-6:
            bits.append(f"hueco {gap / 1000:.0f}s")
        reason = ", ".join(bits)
    return {
        "ok": ok, "buy": buy, "sell": sell, "delta": buy - sell,
        "coverage": ratio, "max_gap_ms": gap, "reason": reason,
    }


class Tape:
    """In-memory aggressor tape for one process. Snapshots go to disk."""

    def __init__(self, run_id):
        self.run = _as_run(run_id) or "run"
        self.cov = {}
        self.flow = {}
        self.open_since = {}

    def note_subscribed(self, coin, now_ms):
        """Start coverage. Call this on the subscription ack, not on send."""
        coin = str(coin or "")
        if not coin or coin in self.open_since:
            return
        now_ms = int(now_ms)
        self.open_since[coin] = now_ms
        self.cov.setdefault(coin, []).append([now_ms, now_ms])

    def touch(self, now_ms):
        now_ms = int(now_ms)
        for coin, start in self.open_since.items():
            spans = self.cov.get(coin) or []
            if spans:
                spans[-1][1] = max(spans[-1][1], now_ms, start)

    def disconnect(self, now_ms):
        self.touch(now_ms)
        self.open_since.clear()

    def add_trade(self, coin, time_ms, side, px, sz, now_ms):
        coin = str(coin or "")
        if coin not in self.open_since:
            return False
        price, size = _f(px), _f(sz)
        when = _f(time_ms)
        if price is None or size is None or when is None or price <= 0 or size <= 0:
            return False
        if abs(when - int(now_ms)) > KEEP_MS:
            return False
        if side == "B":
            key = "buy"
        elif side == "A":
            key = "sell"
        else:
            return False
        # A print from before the ack is not part of the watched interval.
        if when + 1_000 < self.open_since[coin]:
            return False
        bucket = str(int(when // M15_MS) * M15_MS)
        row = self.flow.setdefault(coin, {}).setdefault(bucket, {"buy": 0.0, "sell": 0.0})
        row[key] += price * size
        return True

    def snapshot(self, now_ms):
        self.touch(now_ms)
        flow = {}
        for coin, buckets in self.flow.items():
            flow[coin] = {
                key: {"buy": row["buy"], "sell": row["sell"]}
                for key, row in buckets.items()
            }
        return {
            "run": self.run,
            "updated_ms": int(now_ms),
            "cov": {coin: [list(span) for span in spans] for coin, spans in self.cov.items()},
            "flow": flow,
        }


def read_tape_file(path):
    file = Path(path)
    if not file.exists():
        return None
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _price_side(close, ema_fast, ema_slow, high_level, low_level):
    long_trend = close > ema_fast > ema_slow
    short_trend = close < ema_fast < ema_slow
    long_break = high_level is not None and close > high_level
    short_break = low_level is not None and close < low_level
    if long_trend and long_break and not short_break:
        return "long"
    if short_trend and short_break and not long_break:
        return "short"
    return None


def _volume_multiple(prior, bar, lookback):
    current = _notional(bar)
    if current is None or len(prior) < lookback:
        return None, None
    samples = [_notional(candle) for candle in prior[-lookback:]]
    if any(sample is None for sample in samples):
        return None, None
    average = sum(samples) / lookback
    if average <= 0:
        return None, None
    return current / average, average


def _stop(side, entry, atr, level, swing, atr_mult):
    if atr is None or atr <= 0 or entry is None:
        return None, None
    dist = float(atr_mult) * atr
    kind = "2atr"
    if side == "long":
        if level is not None and level < entry and entry - level > dist:
            dist = entry - level
            kind = "ruptura"
        if swing is not None and swing < entry and entry - swing > dist:
            dist = entry - swing
            kind = "giro"
        return entry - dist, kind
    if side == "short":
        if level is not None and level > entry and level - entry > dist:
            dist = level - entry
            kind = "ruptura"
        if swing is not None and swing > entry and swing - entry > dist:
            dist = swing - entry
            kind = "giro"
        return entry + dist, kind
    return None, None


def _targets(prior, side, entry, sl, cfg):
    risk = abs(entry - sl)
    if risk <= 0:
        return None, None, None, None, None
    sign = 1 if side == "long" else -1
    measured1 = entry + sign * 2 * risk
    measured2 = entry + sign * 3 * risk
    # Shorts take a fixed 2R. The 60-day replay was positive on shorts at 2R
    # in both halves and negative on every long book, so the live target is
    # no longer the next structural level. TP2 stays at 3R for the payload.
    if side == "short":
        return measured1, measured2, "2r", 2.0, 3.0
    structural1, structural2 = watchmod.structural_targets(
        prior, side, entry,
        wing=int(cfg.get("swing_wing", 2)),
        day_bars=int(cfg.get("avg_window_hours", 24)),
    )
    if structural1 is None or watchmod.reward_risk(side, entry, sl, structural1) is None:
        return measured1, measured2, "medido", 2.0, 3.0
    tp1 = structural1
    kind = "estructural"
    if structural2 is not None and watchmod.reward_risk(side, entry, sl, structural2) is not None:
        tp2 = structural2
    else:
        further = measured2 if (sign > 0 and measured2 > tp1) or (sign < 0 and measured2 < tp1) else tp1
        tp2 = further
        kind = "mixto"
    rr1 = watchmod.reward_risk(side, entry, sl, tp1)
    rr2 = watchmod.reward_risk(side, entry, sl, tp2)
    return tp1, tp2, kind, rr1, rr2


def _sig(value):
    number = _f(value)
    if number is None:
        return None
    return float(f"{number:.8g}")


def _close_location(bar):
    """Williams/Arms close location. +1 is the high, -1 is the low.

    None when the bar has no range, so a doji is not called pressure.
    """
    high, low, close = _f(bar.get("h")), _f(bar.get("l")), _f(bar.get("c"))
    if high is None or low is None or close is None:
        return None
    span = high - low
    if span <= 0:
        return None
    return (2 * close - high - low) / span


def _location_agrees(side, location):
    if location is None or side not in ("long", "short"):
        return False
    if side == "long":
        return location > 0
    return location < 0


def _delta_fields(delta):
    if not delta:
        return {"delta": None, "buy_ntl": None, "sell_ntl": None, "delta_coverage": None}
    return {
        "delta": _f(delta.get("delta")),
        "buy_ntl": _f(delta.get("buy")),
        "sell_ntl": _f(delta.get("sell")),
        "delta_coverage": _f(delta.get("coverage")),
    }


def score_bar(prior, bar, entry, asset, delta, cfg, require_delta=True):
    """Score one completed hour against the closed hours before it.

    `require_delta` is false only for the historical replay. Live leaves it
    true. A full hour of tape is used when the socket covered it. The Actions
    job exits every hour, so that coverage is often missing: the pressure
    test then uses the bar's close location and says so. A missing tape is
    not treated as a dollar delta of zero.
    """
    slow = int(cfg["confluence_ema_slow"])
    fast_n = int(cfg["confluence_ema_fast"])
    period = int(cfg["confluence_donchian"])
    if len(prior) < slow or not _contiguous(prior[-slow:], H_MS):
        return {"status": "historia"}
    closes = setup_scanner.closes(list(prior) + [{"c": bar.get("c")}])
    if closes is None:
        return {"status": "historia"}
    fast = setup_scanner.ema_series(closes, fast_n)
    slow_ema = setup_scanner.ema_series(closes, slow)
    if not fast or not slow_ema or fast[-1] is None or slow_ema[-1] is None:
        return {"status": "historia"}
    close = closes[-1]
    high_level = _donchian(prior, period, "high")
    low_level = _donchian(prior, period, "low")
    side = _price_side(close, fast[-1], slow_ema[-1], high_level, low_level)
    multiple, _average = _volume_multiple(prior, bar, period)
    if side is None or multiple is None or multiple < float(cfg["confluence_vol_mult"]):
        return {"status": "no"}
    level = high_level if side == "long" else low_level
    atrs = setup_scanner.wilder_atr(prior, int(cfg["atr_period"]))
    atr = None if not atrs else atrs[-1]
    entry = _f(entry)
    if entry is None or entry <= 0 or atr is None or atr <= 0 or level is None:
        return {"status": "historia"}
    extension = (entry - level) / atr if side == "long" else (level - entry) / atr
    if extension <= 0:
        return {"status": "dentro"}
    wing = int(cfg.get("swing_wing", 2))
    swing = setup_scanner.last_swing(prior, len(prior) - 1, wing, "low" if side == "long" else "high")
    sl, sl_kind = _stop(side, entry, atr, level, swing, cfg["confluence_atr_stop"])
    tp1 = tp2 = tp_kind = rr1 = rr2 = None
    if sl is not None:
        tp1, tp2, tp_kind, rr1, rr2 = _targets(prior, side, entry, sl, cfg)
    base = {
        "side": side, "ema20": fast[-1], "ema50": slow_ema[-1], "donchian": level,
        "vol_mult": multiple, "atr": atr, "extension_atr": extension, "entry": entry,
        "sl": sl, "sl_kind": sl_kind, "tp1": tp1, "tp2": tp2, "rr": rr1,
        "rr_tp2": rr2, "tp_kind": tp_kind, "close": close,
    }
    base["close_location"] = _close_location(bar)
    if extension > float(cfg["confluence_extension_atr"]):
        return {"status": "extendido", **base}
    if require_delta:
        tape_ok = bool(delta and delta.get("ok"))
        buy = sell = None
        if tape_ok:
            buy, sell = _f(delta.get("buy")), _f(delta.get("sell"))
            tape_ok = buy is not None and sell is not None
        if tape_ok:
            pressure = buy > sell if side == "long" else sell > buy
            base.update(_delta_fields(delta))
            base["delta_source"] = "tape"
            if not pressure:
                base["delta_reason"] = "cinta en contra"
                return {"status": "sin_presion", **base}
        else:
            # The hour was not on the tape. Do not invent a dollar delta from
            # the fragment that was seen. The close location is the stand-in,
            # and the payload names it.
            reason = (delta or {}).get("reason") or "sin cinta"
            fields = _delta_fields(delta)
            fields["delta"] = None
            fields["buy_ntl"] = None
            fields["sell_ntl"] = None
            base.update(fields)
            base["delta_source"] = "close_location"
            if not _location_agrees(side, base["close_location"]):
                base["delta_reason"] = reason if base["close_location"] is None else "cierre en la mitad contraria"
                status = "sin_delta" if base["close_location"] is None else "sin_presion"
                return {"status": status, **base}
            base["delta_reason"] = reason
    else:
        base.update(_delta_fields(delta))
        base["delta_source"] = None
    if side == "short" and not funding_allows_short(asset, cfg):
        return {"status": "veto_funding", **base}
    if sl is None or tp1 is None or rr1 is None:
        return {"status": "historia"}
    return {"status": "alerta", **base}


def score_from_candles(cs_1h, cs_15m, now_ms, asset, delta_state, cfg, require_delta=True):
    """Live path: rolling 15m hour, then the same bar score as the replay."""
    if not in_universe(asset, cfg):
        return {"status": "fuera"}
    window = rolling_hour(cs_15m, now_ms)
    if window is None:
        return {"status": "sin_vela"}
    prior = prior_hours(cs_1h, window["t"])
    entry = _f(asset.get("mark"))
    if entry is None or entry <= 0:
        entry = window["c"]
    measured = None
    if require_delta:
        measured = measure_delta(delta_state, asset.get("coin"), window["t"], window["end"], cfg)
    scored = score_bar(prior, window, entry, asset, measured, cfg, require_delta=require_delta)
    scored["window_start"] = window["t"]
    scored["window_end"] = window["end"]
    return scored


def public_payload(scored, asset, now_label):
    """Webhook object. Only called for a real alert, which always has a delta."""
    side = scored["side"]
    return {
        "type": "confluence_1h",
        "coin": asset.get("coin"),
        "dex": asset.get("dex") or "main",
        "side": side,
        "side_label": "largo" if side == "long" else "corto",
        "ema20": _sig(scored.get("ema20")),
        "ema50": _sig(scored.get("ema50")),
        "donchian": _sig(scored.get("donchian")),
        "vol_mult": None if scored.get("vol_mult") is None else round(scored["vol_mult"], 2),
        "delta": _sig(scored.get("delta")),
        "delta_source": scored.get("delta_source"),
        "close_location": None if scored.get("close_location") is None else round(scored["close_location"], 3),
        "buy_ntl": _sig(scored.get("buy_ntl")),
        "sell_ntl": _sig(scored.get("sell_ntl")),
        "delta_coverage": None if scored.get("delta_coverage") is None else round(scored["delta_coverage"], 3),
        "mark": _sig(asset.get("mark")),
        "dayNtlVlm": _sig(asset.get("dayNtlVlm")),
        "oi_usd": _sig(asset.get("oi_usd")),
        "funding_pct": _sig(asset.get("funding_pct")),
        "premium_pct": _sig(asset.get("premium_pct")),
        "atr": _sig(scored.get("atr")),
        "entry": _sig(scored.get("entry")),
        "sl": _sig(scored.get("sl")),
        "sl_kind": scored.get("sl_kind"),
        "tp1": _sig(scored.get("tp1")),
        "tp2": _sig(scored.get("tp2")),
        "rr": None if scored.get("rr") is None else round(scored["rr"], 2),
        "rr_tp2": None if scored.get("rr_tp2") is None else round(scored["rr_tp2"], 2),
        "tp_kind": scored.get("tp_kind"),
        "extension_atr": None if scored.get("extension_atr") is None else round(scored["extension_atr"], 2),
        "detected_at_madrid": now_label,
    }


def walk_outcome(candles, entry_index, side, entry, sl, tp1, horizon=48):
    """What the next hours do. A bar that trades the stop and the target is a stop."""
    entry, sl, tp1 = _f(entry), _f(sl), _f(tp1)
    if entry is None or sl is None or tp1 is None or side not in ("long", "short"):
        return "open"
    if entry_index is None or entry_index < 0 or entry_index >= len(candles or []):
        return "open"
    last = min(len(candles) - 1, int(entry_index) + int(horizon))
    for candle in candles[int(entry_index) + 1:last + 1]:
        high, low = _f(candle.get("h")), _f(candle.get("l"))
        if high is None or low is None:
            continue
        if side == "long":
            hit_sl = low <= sl
            hit_tp = high >= tp1
        else:
            hit_sl = high >= sl
            hit_tp = low <= tp1
        if hit_sl:
            return "sl"
        if hit_tp:
            return "tp1"
    return "open"
