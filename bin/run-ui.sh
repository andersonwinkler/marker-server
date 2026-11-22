#!/usr/bin/env bash
# Launch the Marker browser UI (proxies to the API).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${MARKER_SERVER_CONFIG:-$ROOT/config.json}"
export MARKER_SERVER_CONFIG="$CONFIG"

API_BASE=http://127.0.0.1:8001
HOST=127.0.0.1
PORT=8002
PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY=python3.12

if [[ -f "$CONFIG" ]]; then
  eval "$(python3.12 - "$CONFIG" <<'PY'
import json, os, shlex, sys
with open(sys.argv[1], encoding='utf-8') as fp:
    cfg = json.load(fp)
rt = cfg.get('runtime') or {}
marker = cfg.get('marker') or {}
host = str(rt.get('host', '127.0.0.1'))
api_port = int(rt.get('port', 8001))
ui_port = int(rt.get('ui_port', 8002))
home = marker.get('home') or ''
py = marker.get('python') or (
    os.path.join(home, 'venv', 'bin', 'python') if home else '')
print(f'API_BASE={shlex.quote(f"http://{host}:{api_port}")}')
print(f'HOST={shlex.quote(host)}')
print(f'PORT={shlex.quote(str(ui_port))}')
if py and os.path.isfile(py):
    print(f'PY={shlex.quote(py)}')
PY
)"
fi

export MARKER_API_BASE="$API_BASE"
export MARKER_UI_HOST="$HOST"
export MARKER_UI_PORT="$PORT"
cd "$ROOT/server"
exec "$PY" ui_server.py
