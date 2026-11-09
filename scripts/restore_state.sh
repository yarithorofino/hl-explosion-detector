#!/usr/bin/env bash
# Copy detector memory from the detector-state branch into this workspace.
# If that branch does not exist yet, leave the workspace empty (first run).
# If the branch exists but cannot be read, abort. A later step must not
# replace a real state file with an empty one.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/.."
export GIT_TERMINAL_PROMPT=0

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 no está instalado" >&2
  exit 1
fi

mkdir -p data signals

set +e
REMOTE=$(git ls-remote --heads origin detector-state)
ls_rc=$?
set -e
if [[ $ls_rc -ne 0 ]]; then
  echo "No se pudo consultar la rama detector-state (rc=$ls_rc). Se aborta para no pisar el estado." >&2
  exit 1
fi

if [[ -z "${REMOTE//[[:space:]]/}" ]]; then
  echo "No existe la rama detector-state. Primera ejecución: se empieza sin memoria."
  exit 0
fi

git fetch --no-tags --depth=1 origin "refs/heads/detector-state:refs/remotes/origin/detector-state"

python3 - <<'PY'
import json, pathlib, subprocess, sys

def show(path):
    p = subprocess.run(["git", "show", f"origin/detector-state:{path}"], capture_output=True)
    if p.returncode != 0:
        return None
    return p.stdout

raw = show("data/detector_state.json")
if not raw:
    print("La rama detector-state no tiene data/detector_state.json. Se aborta; el remoto no se toca.", file=sys.stderr)
    sys.exit(1)
try:
    d = json.loads(raw.decode("utf-8"))
except json.JSONDecodeError:
    print("data/detector_state.json remoto no es JSON válido. Se aborta; el remoto no se toca.", file=sys.stderr)
    sys.exit(1)
if not isinstance(d, dict) or not isinstance(d.get("marks"), list):
    print("Estado remoto con forma inesperada. Se aborta; el remoto no se toca.", file=sys.stderr)
    sys.exit(1)
if not isinstance(d.get("last_fire", {}), dict) or not isinstance(d.get("wakes", {}), dict):
    print("Estado remoto con forma inesperada. Se aborta; el remoto no se toca.", file=sys.stderr)
    sys.exit(1)
path = pathlib.Path("data/detector_state.json")
path.write_bytes(raw)
print(f"Estado restaurado: marcas={len(d['marks'])} cooldowns={len(d.get('last_fire') or {})} dias={len(d.get('wakes') or {})}")

alerts = show("signals/explosion_alerts.jsonl")
if alerts:
    lines = [ln for ln in alerts.decode("utf-8", "replace").splitlines() if ln.strip()]
    if len(lines) > 300:
        lines = lines[-300:]
    pathlib.Path("signals/explosion_alerts.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Historial de alertas restaurado: {len(lines)} lineas")
else:
    print("Sin historial de alertas previo.")
PY
