#!/usr/bin/env bash
# Publish detector memory as a single commit on branch detector-state.
# The branch is unrelated to main and is rewritten each run, so clone history
# does not grow by one commit every 15 minutes. --force-with-lease refuses
# the update if the remote moved, and this script never pushes to main.
set -euo pipefail

if [[ "${GITHUB_ACTIONS:-}" != "true" ]]; then
  echo "save_state.sh solo se ejecuta en GitHub Actions." >&2
  exit 1
fi

cd "$(dirname "$(readlink -f "$0")")/.."
export GIT_TERMINAL_PROMPT=0

BRANCH="detector-state"
TARGET_REF="refs/heads/detector-state"
if [[ "$BRANCH" != "detector-state" || "$TARGET_REF" != "refs/heads/detector-state" ]]; then
  echo "Destino de guardado no permitido." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 no está instalado" >&2
  exit 1
fi

if [[ ! -s data/detector_state.json ]]; then
  echo "Falta data/detector_state.json; no se guarda el estado." >&2
  exit 1
fi

python3 - <<'PY'
import json, os, sys
from pathlib import Path

def abort_if_secret(text, label):
    for name in ("EXPLOSION_WEBHOOK_URL", "EXPLOSION_WEBHOOK_AUTH", "EXPLOSION_WEBHOOK_KEY"):
        raw = os.environ.get(name) or ""
        parts = [raw, raw.strip()]
        _, _, rest = raw.partition(":")
        if rest.strip():
            parts.append(rest.strip())
        for part in parts:
            if len(part) >= 8 and part in text:
                print(f"{label} contiene el valor de {name}; no se guarda", file=sys.stderr)
                sys.exit(1)

state_text = Path("data/detector_state.json").read_text(encoding="utf-8")
d = json.loads(state_text)
if not isinstance(d, dict) or not isinstance(d.get("marks"), list):
    print("detector_state.json no tiene la forma esperada; no se guarda", file=sys.stderr)
    sys.exit(1)
if not isinstance(d.get("last_fire"), dict) or not isinstance(d.get("wakes"), dict):
    print("detector_state.json no tiene la forma esperada; no se guarda", file=sys.stderr)
    sys.exit(1)
abort_if_secret(state_text, "el estado")
alerts = Path("signals/explosion_alerts.jsonl")
if alerts.exists():
    abort_if_secret(alerts.read_text(encoding="utf-8", errors="replace"), "el historial de alertas")
scans = Path("data/scans")
if scans.exists():
    for child in scans.iterdir():
        if child.is_file():
            abort_if_secret(child.read_text(encoding="utf-8", errors="replace"), f"el scan {child.name}")
shadow = Path("data/shadow")
if shadow.exists():
    for child in shadow.iterdir():
        if child.is_file():
            abort_if_secret(child.read_text(encoding="utf-8", errors="replace"), f"la sombra {child.name}")
setups = Path("data/setups")
if setups.exists():
    for child in setups.iterdir():
        if child.is_file():
            abort_if_secret(child.read_text(encoding="utf-8", errors="replace"), f"la preparacion {child.name}")
conf = Path("data/confluence")
if conf.exists():
    for child in conf.iterdir():
        if child.is_file():
            abort_if_secret(child.read_text(encoding="utf-8", errors="replace"), f"la confluencia {child.name}")

def check_book(name):
    path = Path("data") / name / "open.json"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    abort_if_secret(text, f"{name} open")
    book = json.loads(text)
    if not isinstance(book, dict) or not isinstance(book.get("positions"), list):
        print(f"{name}/open.json no tiene la forma esperada; no se guarda", file=sys.stderr)
        sys.exit(1)
    trades = path.with_name("trades.csv")
    if trades.exists():
        abort_if_secret(trades.read_text(encoding="utf-8", errors="replace"), f"{name} trades")

for book_name in ("trend4h", "squeeze4h", "trapped_shorts1h", "c1_maker"):
    check_book(book_name)
runner = Path("data/paper/runner.json")
if runner.exists():
    abort_if_secret(runner.read_text(encoding="utf-8"), "paper runner")
for label, folder in (
    ("market_log", "data/market_log"),
    ("orderbook_log", "data/orderbook_log"),
    ("liq_proxy_log", "data/liq_proxy_log"),
    ("liq_trades_log", "data/liq_trades_log"),
    ("liq_tape_log", "data/liq_tape_log"),
):
    folder_path = Path(folder)
    if folder_path.exists():
        for child in folder_path.iterdir():
            if child.is_file():
                abort_if_secret(
                    child.read_text(encoding="utf-8", errors="replace"),
                    f"{label} {child.name}",
                )
for extra in ("data/listings_log.csv", "data/listings_snapshot.json"):
    extra_path = Path(extra)
    if extra_path.exists():
        abort_if_secret(
            extra_path.read_text(encoding="utf-8", errors="replace"),
            extra,
        )
print(f"estado valido: marcas={len(d['marks'])} cooldowns={len(d['last_fire'])} dias_wake={len(d['wakes'])}")
PY

export GIT_AUTHOR_NAME="github-actions[bot]"
export GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com"
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"

INDEX=$(mktemp)
LEEME=$(mktemp)
cleanup() {
  rm -f "$INDEX" "$LEEME"
  unset GIT_INDEX_FILE || true
}
trap cleanup EXIT
rm -f "$INDEX"
export GIT_INDEX_FILE="$INDEX"

git read-tree --empty

add_blob() {
  local path="$1"
  local file="$2"
  local blob
  blob=$(git hash-object -w "$file")
  git update-index --add --cacheinfo "100644,${blob},${path}"
}

add_dated_csvs() {
  local dir="$1"
  local label="$2"
  local cutoff="$3"
  if [[ ! -d "$dir" ]]; then
    return
  fi
  while IFS= read -r f; do
    base=$(basename "$f")
    case "$base" in
      [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].csv)
        day=${base%.csv}
        if [[ "$day" < "$cutoff" ]]; then
          echo "${label} antiguo, no se conserva: $base"
          continue
        fi
        add_blob "${dir}/${base}" "$f"
        ;;
      *)
        echo "Fichero de ${label} no permitido: $f" >&2
        exit 1
        ;;
    esac
  done < <(find "$dir" -type f | sort)
}

add_blob "data/detector_state.json" "data/detector_state.json"

if [[ -s signals/explosion_alerts.jsonl ]]; then
  add_blob "signals/explosion_alerts.jsonl" "signals/explosion_alerts.jsonl"
fi

if [[ -d data/scans ]]; then
  while IFS= read -r f; do
    base=$(basename "$f")
    case "$base" in
      latest.md)
        add_blob "data/scans/latest.md" "$f"
        ;;
      [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].csv)
        add_blob "data/scans/$base" "$f"
        ;;
      *)
        echo "Fichero de scan no permitido: $f" >&2
        exit 1
        ;;
    esac
  done < <(find data/scans -type f | sort)
fi

if [[ -d data/shadow ]]; then
  while IFS= read -r f; do
    base=$(basename "$f")
    case "$base" in
      [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].csv)
        add_blob "data/shadow/$base" "$f"
        ;;
      *)
        echo "Fichero de sombra no permitido: $f" >&2
        exit 1
        ;;
    esac
  done < <(find data/shadow -type f | sort)
fi

if [[ -d data/setups ]]; then
  while IFS= read -r f; do
    base=$(basename "$f")
    case "$base" in
      [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].csv)
        add_blob "data/setups/$base" "$f"
        ;;
      *)
        echo "Fichero de preparaciones no permitido: $f" >&2
        exit 1
        ;;
    esac
  done < <(find data/setups -type f | sort)
fi

if [[ -d data/confluence ]]; then
  while IFS= read -r f; do
    base=$(basename "$f")
    case "$base" in
      [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].csv)
        add_blob "data/confluence/$base" "$f"
        ;;
      *)
        echo "Fichero de confluencia no permitido: $f" >&2
        exit 1
        ;;
    esac
  done < <(find data/confluence -type f | sort)
fi

add_book() {
  local name="$1"
  if [[ -s "data/${name}/open.json" ]]; then
    add_blob "data/${name}/open.json" "data/${name}/open.json"
  fi
  if [[ -s "data/${name}/trades.csv" ]]; then
    add_blob "data/${name}/trades.csv" "data/${name}/trades.csv"
  fi
  if [[ -d "data/${name}" ]]; then
    while IFS= read -r f; do
      base=$(basename "$f")
      case "$base" in
        open.json|trades.csv|*.tmp) ;;
        *)
          echo "Fichero de ${name} no permitido: $f" >&2
          exit 1
          ;;
      esac
    done < <(find "data/${name}" -type f | sort)
  fi
}
add_book trend4h
add_book squeeze4h
add_book trapped_shorts1h
add_book c1_maker

if [[ -s data/paper/runner.json ]]; then
  add_blob "data/paper/runner.json" "data/paper/runner.json"
fi

cutoff=$(date -u -d '120 days ago' +%F)
add_dated_csvs data/market_log market_log "$cutoff"
add_dated_csvs data/orderbook_log orderbook_log "$cutoff"
add_dated_csvs data/liq_proxy_log liq_proxy_log "$cutoff"
add_dated_csvs data/liq_trades_log liq_trades_log "$cutoff"
add_dated_csvs data/liq_tape_log liq_tape_log "$cutoff"

if [[ -s data/listings_log.csv ]]; then
  add_blob "data/listings_log.csv" "data/listings_log.csv"
fi
if [[ -s data/listings_snapshot.json ]]; then
  add_blob "data/listings_snapshot.json" "data/listings_snapshot.json"
fi

cat > "$LEEME" <<'EOF'
Esta rama la reescriben GitHub Actions (workflows "Detector de explosiones" y "Paper 4h").
No edites estos ficheros: el siguiente ciclo los sustituye.

- data/detector_state.json
  Memoria del detector: precios e interés abierto de hace unas horas, pausa por activo y contadores de avisos (día y hora).
- signals/explosion_alerts.jsonl
  Historial corto de avisos (una línea JSON por tanda). Si aún no ha habido avisos, este fichero no está.
- data/scans/latest.md
  Tabla del último ciclo, la misma que el resumen de Actions.
- data/scans/AAAA-MM-DD.csv
  Un candidato por fila. Solo se conservan los últimos 14 días.
- data/shadow/AAAA-MM-DD.csv
  Señales tempranas antiguas, si quedara alguna. Ya no se escriben.
- data/confluence/AAAA-MM-DD.csv
  Avisos confluence_1h (enviados, en silencio, sin delta, tope). Recuento hasta el 24 nov.
- data/trend4h/open.json y data/trend4h/trades.csv
  Libro de paper de la tendencia 4h (posiciones abiertas y cierres).
- data/squeeze4h/open.json y data/squeeze4h/trades.csv
  Libro de paper del squeeze 4h.
- data/trapped_shorts1h/open.json y data/trapped_shorts1h/trades.csv
  Libro de paper de trapped shorts 1h.
- data/c1_maker/open.json y data/c1_maker/trades.csv
  Libro de paper del pump-fade corto con límite maker (c1_maker). No hay órdenes reales.
- data/paper/runner.json
  Semana ISO del último resumen semanal enviado (last_weekly_sent, p. ej. 2026-W47). Solo se escribe tras un POST 2xx.
- data/market_log/AAAA-MM-DD.csv
  Snapshot horario de perps. Se conservan 120 días.
- data/orderbook_log/AAAA-MM-DD.csv
  Profundidad del libro (0,5%, 1% y 2%) y spread. Se conservan 120 días.
- data/listings_log.csv y data/listings_snapshot.json
  Altas, bajas y cambios de apalancamiento máximo, y la última foto del universo.
- data/liq_proxy_log/AAAA-MM-DD.csv
  Proxy horario de OI, rango y volumen. No es una liquidación. Se conservan 120 días.
- data/liq_trades_log/AAAA-MM-DD.csv
  Trades públicos que traen marca de liquidación, si el payload la trae.
- data/liq_tape_log/AAAA-MM-DD.csv
  Muestra de la cinta pública: duración, trades vistos y cuántos venían marcados.

No hay secretos aquí. La URL del webhook no se guarda en el repositorio.
EOF
add_blob "LEEME.txt" "$LEEME"

while IFS= read -r f; do
  case "$f" in
    LEEME.txt|data/detector_state.json|signals/explosion_alerts.jsonl|data/scans/latest.md) ;;
    data/scans/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].csv) ;;
    data/shadow/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].csv) ;;
    data/setups/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].csv) ;;
    data/confluence/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].csv) ;;
    data/trend4h/open.json|data/trend4h/trades.csv) ;;
    data/squeeze4h/open.json|data/squeeze4h/trades.csv) ;;
    data/trapped_shorts1h/open.json|data/trapped_shorts1h/trades.csv) ;;
    data/c1_maker/open.json|data/c1_maker/trades.csv) ;;
    data/paper/runner.json) ;;
    data/market_log/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].csv) ;;
    data/orderbook_log/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].csv) ;;
    data/liq_proxy_log/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].csv) ;;
    data/liq_trades_log/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].csv) ;;
    data/liq_tape_log/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].csv) ;;
    data/listings_log.csv|data/listings_snapshot.json) ;;
    *)
      echo "Ruta inesperada en el estado: $f" >&2
      exit 1
      ;;
  esac
done < <(git ls-files)

git ls-files --error-unmatch data/detector_state.json >/dev/null

TREE=$(git write-tree)
unset GIT_INDEX_FILE

MSG="chore: actualizar estado del detector"
if [[ -n "${GITHUB_RUN_ID:-}" ]]; then
  MSG="$MSG (run ${GITHUB_RUN_ID})"
fi
EXTRA=""
if [[ -f data/run_summary.txt ]]; then
  EXTRA=$(head -n 1 data/run_summary.txt | tr -cd '[:alnum:][:space:]=_,.\[\]-' | cut -c1-200)
fi

if [[ -n "$EXTRA" ]]; then
  COMMIT=$(git commit-tree "$TREE" -m "$MSG" -m "$EXTRA")
else
  COMMIT=$(git commit-tree "$TREE" -m "$MSG")
fi

if [[ ! "$COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Commit de estado inválido; no se guarda." >&2
  exit 1
fi

remote_sha() {
  git ls-remote --heads origin detector-state | awk '$2=="refs/heads/detector-state" {print $1; exit}'
}

set +e
REMOTE_LINE=$(git ls-remote --heads origin detector-state)
ls_rc=$?
set -e
if [[ $ls_rc -ne 0 ]]; then
  echo "No se pudo leer la rama remota detector-state. No se guarda." >&2
  exit 1
fi
EXPECTED=$(printf '%s\n' "$REMOTE_LINE" | awk '$2=="refs/heads/detector-state" {print $1; exit}')
if [[ -n "$EXPECTED" && ! "$EXPECTED" =~ ^[0-9a-f]{40}$ ]]; then
  echo "SHA remoto inesperado; no se guarda." >&2
  exit 1
fi

try_push() {
  if [[ -z "$EXPECTED" ]]; then
    git push origin "${COMMIT}:refs/heads/detector-state"
  else
    git push --force-with-lease="refs/heads/detector-state:${EXPECTED}" origin "${COMMIT}:refs/heads/detector-state"
  fi
}

pushed=0
for i in 1 2 3 4; do
  if try_push; then
    pushed=1
    break
  fi
  set +e
  REMOTE_NOW=$(git ls-remote --heads origin detector-state)
  read_rc=$?
  set -e
  if [[ $read_rc -ne 0 ]]; then
    echo "No se pudo releer detector-state tras un fallo de push (intento ${i})." >&2
    if [[ "$i" -eq 4 ]]; then
      exit 1
    fi
    sleep $((i * 2))
    continue
  fi
  now=$(printf '%s\n' "$REMOTE_NOW" | awk '$2=="refs/heads/detector-state" {print $1; exit}')
  if [[ "$now" == "$COMMIT" ]]; then
    echo "El remoto ya tiene este estado."
    pushed=1
    break
  fi
  if [[ -z "$EXPECTED" && -n "$now" ]]; then
    echo "La rama detector-state ya existe con otro commit. No se pisa." >&2
    exit 1
  fi
  if [[ -n "$EXPECTED" && "$now" != "$EXPECTED" ]]; then
    echo "La rama detector-state cambió mientras se guardaba. No se pisa." >&2
    exit 1
  fi
  if [[ "$i" -eq 4 ]]; then
    echo "No se pudo guardar el estado en detector-state." >&2
    exit 1
  fi
  sleep $((i * 2))
done

if [[ "$pushed" -ne 1 ]]; then
  echo "No se pudo guardar el estado en detector-state." >&2
  exit 1
fi

set +e
after=$(remote_sha)
set -e
if [[ -n "$after" && "$after" != "$COMMIT" ]]; then
  echo "El guardado no quedó confirmado en el remoto." >&2
  exit 1
fi

echo "Estado guardado en la rama detector-state (${COMMIT})."
