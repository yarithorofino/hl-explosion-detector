#!/usr/bin/env python3
"""When the next scan starts, and whether another Actions run is waiting.

The cron on this public repo is often started about once an hour. One job
therefore scans, saves state, then sleeps until the next quarter-hour.
Detection rules do not live here.
"""
import json
import os
import sys
import time
import urllib.request

PERIOD_SEC = 15 * 60
SLOT_OFFSET_SEC = 20
MAX_RUNTIME_SEC = 60 * 60
DEFAULT_ITERATIONS = 4
MAX_ITERATIONS = 8
WAITING = {"queued", "pending", "waiting", "requested"}


def next_slot_epoch(now, offset=SLOT_OFFSET_SEC, period=PERIOD_SEC):
    """Next wall-clock quarter (:00/:15/:30/:45) plus a few seconds.

    The offset lets the exchange publish the candle that just closed.
    A timestamp already past this quarter's offset waits for the following one.
    """
    now = float(now)
    base = (int(now) // period) * period + offset
    if base <= now:
        base += period
    return base


def should_start_iteration(elapsed_sec, index, max_runtime=MAX_RUNTIME_SEC):
    """The first scan always starts. Later ones do not start after max_runtime."""
    if index <= 1:
        return True
    return float(elapsed_sec) < float(max_runtime)


def iterations_for(force_test, requested, default=DEFAULT_ITERATIONS, cap=MAX_ITERATIONS):
    """The webhook test is a single scan. Other runs use the requested count."""
    if force_test:
        return 1
    try:
        count = int(str(requested).strip())
    except (TypeError, ValueError):
        count = default
    if count < 1:
        count = 1
    if count > cap:
        count = cap
    return count


def plan_scan_starts(start_epoch, scan_seconds, iterations, max_runtime=MAX_RUNTIME_SEC,
                     offset=SLOT_OFFSET_SEC, period=PERIOD_SEC):
    """Start times if every scan takes scan_seconds and nobody else is waiting."""
    starts = []
    now = float(start_epoch)
    for index in range(1, int(iterations) + 1):
        if not should_start_iteration(now - start_epoch, index, max_runtime):
            break
        starts.append(now)
        if index == int(iterations):
            break
        finish = now + float(scan_seconds)
        nxt = next_slot_epoch(finish, offset, period)
        if nxt - start_epoch >= max_runtime:
            break
        now = nxt
    return starts


def newer_run_waiting(runs, current_run_id):
    """True when a later run of this workflow is queued behind the concurrency lock."""
    try:
        current = int(current_run_id)
    except (TypeError, ValueError):
        return False
    for run in runs or []:
        try:
            run_id = int(run.get("id"))
        except (TypeError, ValueError):
            continue
        if run_id > current and str(run.get("status") or "").lower() in WAITING:
            return True
    return False


def _redact(text):
    token = os.environ.get("GITHUB_TOKEN") or ""
    if len(token) >= 4:
        text = text.replace(token, "[redacted]")
    return text


def fetch_workflow_runs():
    """Return [{id, status}] or None when the queue cannot be read.

    None means "keep scanning": losing the API must not drop the quarter-hours.
    """
    repo = os.environ.get("GITHUB_REPOSITORY") or ""
    token = os.environ.get("GITHUB_TOKEN") or ""
    if not repo or not token:
        return None
    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        "explosion-detector.yml/runs?per_page=30"
    )
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "hl-explosion-detector",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            payload = json.load(response)
    except Exception as exc:
        print(f"No se pudo consultar la cola de Actions: {_redact(str(exc))[:200]}", flush=True)
        return None
    runs = []
    for item in payload.get("workflow_runs") or []:
        runs.append({"id": item.get("id"), "status": item.get("status")})
    return runs


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "sleep-seconds":
        now = time.time()
        target = next_slot_epoch(now)
        # Whole seconds, at least one so the shell sleep is well defined.
        print(max(1, int(target - now)))
        return 0
    if cmd == "iterations":
        force = (os.environ.get("FORCE_TEST_POST") or "").strip().lower() in {"1", "true", "yes", "on"}
        print(iterations_for(force, os.environ.get("SCAN_ITERATIONS")))
        return 0
    if cmd == "newer-waiting":
        runs = fetch_workflow_runs()
        if runs is None:
            print("no")
            return 0
        waiting = newer_run_waiting(runs, os.environ.get("GITHUB_RUN_ID"))
        print("yes" if waiting else "no")
        return 0
    print("uso: scan_schedule.py sleep-seconds|iterations|newer-waiting", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
