#!/usr/bin/env bash
# Several scans inside one Actions job. GitHub often starts the cron only
# about once an hour on this public repo; the loop fills the quarter-hours.
# Secrets are in the environment for detector.py and are never printed.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/.."
export PYTHONUNBUFFERED=1

START=$(date +%s)
MAX_RUNTIME=3600
ITERATIONS=$(python3 scan_schedule.py iterations)
echo "Pasadas previstas: ${ITERATIONS} (la prueba del webhook es una sola)."

# Aggressor tape. Coverage starts on the subscription ack. A dead tape
# only leaves confluence rows at sin_delta; the scan still runs.
python3 trade_tape.py &
TAPE_PID=$!
cleanup_tape() {
  if [[ -n "${TAPE_PID:-}" ]]; then
    kill "$TAPE_PID" 2>/dev/null || true
    wait "$TAPE_PID" 2>/dev/null || true
  fi
}
trap cleanup_tape EXIT

any_ok=0
last_rc=0

for ((i=1; i<=ITERATIONS; i++)); do
  now=$(date +%s)
  elapsed=$((now - START))
  if (( i > 1 && elapsed >= MAX_RUNTIME )); then
    echo "Parada: han pasado ${elapsed}s. No empieza otra pasada."
    break
  fi
  echo "=== Pasada ${i} de ${ITERATIONS} ($(date -u +%H:%M:%S) UTC) ==="
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '\n### Pasada %s de %s\n\n' "$i" "$ITERATIONS" >> "$GITHUB_STEP_SUMMARY"
  fi

  # A failed scan must not reuse the previous success marker.
  rm -f data/state_ready

  bash scripts/restore_state.sh

  set +e
  python3 detector.py
  last_rc=$?
  set -e

  if [[ -f data/state_ready ]]; then
    bash scripts/save_state.sh
    any_ok=1
    rm -f data/state_ready
  else
    echo "Pasada ${i}: sin estado nuevo. La rama detector-state no se toca."
  fi

  if (( i >= ITERATIONS )); then
    break
  fi
  if [[ "$(python3 scan_schedule.py newer-waiting)" == "yes" ]]; then
    echo "Hay una ejecución más nueva en espera. Esta guarda y deja el sitio."
    break
  fi
  next_in=$(python3 scan_schedule.py sleep-seconds)
  now=$(date +%s)
  if (( now + next_in - START >= MAX_RUNTIME )); then
    echo "La siguiente pasada caería después de 60 min. Se para aquí."
    break
  fi
  echo "Espera ${next_in}s hasta el siguiente cuarto de hora."
  sleep "$next_in"
done

if [[ "${FORCE_TEST_POST:-}" == "true" || "${FORCE_TEST_POST:-}" == "1" ]]; then
  exit "$last_rc"
fi
if (( any_ok == 0 )); then
  echo "Ninguna pasada pudo guardar estado." >&2
  exit 1
fi
exit 0
