#!/usr/bin/env bash
# One paper pass: trend4h, squeeze4h and trapped_shorts1h.
# Secrets stay in the environment and are never printed.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/.."
export PYTHONUNBUFFERED=1

rm -f data/state_ready
bash scripts/restore_state.sh

set +e
python3 paper_run.py
rc=$?
set -e

if [[ -f data/state_ready ]]; then
  bash scripts/save_state.sh
else
  echo "Sin estado nuevo. La rama detector-state no se toca."
fi
exit "$rc"
