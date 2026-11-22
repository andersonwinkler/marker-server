#!/usr/bin/env bash
# Launch the Marker API using the vanilla Marker interpreter from config.json.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${MARKER_SERVER_CONFIG:-$ROOT/config.json}"
export MARKER_SERVER_CONFIG="$CONFIG"

if [[ ! -f "$CONFIG" ]]; then
  echo "Missing config: $CONFIG" >&2
  echo "Copy config.example.json to config.json and set marker.home + llm.*" >&2
  exit 1
fi

# Prefer python3.12, then python3 (stdlib-only probe via config.py).
if [[ -n "${PYTHON_BIN:-}" ]]; then
  PROBE_PY="$PYTHON_BIN"
elif command -v python3.12 >/dev/null 2>&1; then
  PROBE_PY=python3.12
else
  PROBE_PY=python3
fi

# Single source of truth: server/config.py (same env app.py applies).
eval "$(
  cd "$ROOT/server"
  MARKER_SERVER_CONFIG="$CONFIG" "$PROBE_PY" <<'PY'
import os, shlex, sys
from config import marker_python, runtime_settings, surya_env_from_config

try:
    env = surya_env_from_config()
except RuntimeError as exc:
    sys.stderr.write(str(exc) + '\n')
    sys.exit(2)
rt = runtime_settings()
py = marker_python()
if not os.path.isfile(py):
    sys.stderr.write(f'Marker python not found: {py}\n')
    sys.exit(2)
env['MARKER_SERVER_CONFIG'] = os.environ.get('MARKER_SERVER_CONFIG', '')
for k, v in env.items():
    print(f'export {k}={shlex.quote(str(v))}')
print(f'MARKER_PY={shlex.quote(py)}')
print(f'API_HOST={shlex.quote(str(rt["host"]))}')
print(f'API_PORT={shlex.quote(str(int(rt["port"])))}')
PY
)"

cd "$ROOT/server"
exec "$MARKER_PY" app.py --host "$API_HOST" --port "$API_PORT" "$@"
