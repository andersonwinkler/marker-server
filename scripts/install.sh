#!/usr/bin/env bash
# Install marker-server into /opt/marker-server/<VERSION>.
# Does NOT install Marker itself — set marker.home in config.json.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
PREFIX="${INSTALL_PREFIX:-/opt/marker-server/$VERSION}"
MARKER_HOME_DEFAULT="${MARKER_HOME:-/opt/marker/2.0.0}"

echo "Installing marker-server $VERSION → $PREFIX"
echo "  (will point at Marker home: $MARKER_HOME_DEFAULT)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Re-running with sudo…"
  exec sudo env \
    INSTALL_PREFIX="$PREFIX" \
    MARKER_HOME="$MARKER_HOME_DEFAULT" \
    MARKER_OPENAI_BASE_URL="${MARKER_OPENAI_BASE_URL:-}" \
    MARKER_OPENAI_MODEL="${MARKER_OPENAI_MODEL:-}" \
    MARKER_OPENAI_API_KEY="${MARKER_OPENAI_API_KEY:-}" \
    LLAMA_CPP_BINARY="${LLAMA_CPP_BINARY:-}" \
    bash "$0" "$@"
fi

MARKER_HOME="${MARKER_HOME:-$MARKER_HOME_DEFAULT}"
# Vision LLM is optional at install time — leave use_llm false until config is filled.

mkdir -p "$PREFIX"/{bin,server,uploads,jobs}
cp -a "$ROOT/server/." "$PREFIX/server/"
cp -a "$ROOT/bin/." "$PREFIX/bin/"
chmod 755 "$PREFIX/bin/"*.sh
chmod 644 "$PREFIX/server/"*.py

CONFIG="$PREFIX/config.json"
if [[ ! -f "$CONFIG" ]]; then
  cp "$ROOT/config.example.json" "$CONFIG"
  chmod 600 "$CONFIG"
  python3.12 - "$CONFIG" "$MARKER_HOME" "${LLAMA_CPP_BINARY:-}" \
    "${MARKER_OPENAI_BASE_URL:-}" "${MARKER_OPENAI_MODEL:-}" \
    "${MARKER_OPENAI_API_KEY:-}" <<'PY'
import json, sys
path, home, llama, url, model, key = sys.argv[1:7]
with open(path, encoding='utf-8') as fp:
    data = json.load(fp)
data.setdefault('marker', {})['home'] = home
data['marker']['python'] = None
if llama:
    data.setdefault('surya', {})['llama_cpp_binary'] = llama
llm = data.setdefault('llm', {})
if url:
    llm['openai_base_url'] = url
if model:
    llm['openai_model'] = model
if key:
    llm['openai_api_key'] = key
with open(path, 'w', encoding='utf-8') as fp:
    json.dump(data, fp, indent=2)
    fp.write('\n')
PY
  echo "Wrote $CONFIG (mode 600)"
else
  echo "Keeping existing $CONFIG"
fi
cp -f "$ROOT/config.example.json" "$PREFIX/config.example.json"
cp -f "$ROOT/README.md" "$PREFIX/README.md"
cp -f "$ROOT/VERSION" "$PREFIX/VERSION"
mkdir -p "$PREFIX/docs"
cp -a "$ROOT/docs/." "$PREFIX/docs/"

# API runs under Marker's interpreter (bin/run-api.sh), so install the thin
# HTTP stack into that venv. Keep a small local venv as UI fallback only.
SERVER_DEPS=(fastapi uvicorn python-multipart httpx click requests pypdf six)
if [[ -x "$MARKER_HOME/venv/bin/pip" ]]; then
  "$MARKER_HOME/venv/bin/pip" install -U pip setuptools wheel
  "$MARKER_HOME/venv/bin/pip" install "${SERVER_DEPS[@]}"
else
  echo "WARNING: $MARKER_HOME/venv missing; API deps not installed into Marker."
fi
if [[ ! -x "$PREFIX/venv/bin/python" ]]; then
  python3.12 -m venv "$PREFIX/venv"
fi
# shellcheck disable=SC1091
source "$PREFIX/venv/bin/activate"
pip install -U pip setuptools wheel
pip install "${SERVER_DEPS[@]}"

UNIT_DIR=/etc/systemd/system
sed "s|/opt/marker-server/0.1.0|$PREFIX|g" \
  "$ROOT/systemd/marker-server-api.service" > "$UNIT_DIR/marker-server-api.service"
sed "s|/opt/marker-server/0.1.0|$PREFIX|g" \
  "$ROOT/systemd/marker-server-ui.service" > "$UNIT_DIR/marker-server-ui.service"

ln -sfn "$PREFIX" /opt/marker-server/current
systemctl daemon-reload

echo
echo "Installed marker-server $VERSION"
echo "  Prefix:  $PREFIX"
echo "  Config:  $CONFIG"
echo "  Marker:  marker.home → $MARKER_HOME"
if [[ ! -x "$MARKER_HOME/venv/bin/python" ]]; then
  echo "  WARNING: Marker python missing at $MARKER_HOME/venv/bin/python"
  echo "           Install vanilla Marker first, then set marker.home in config.json."
fi
mkdir -p "$PREFIX/apache"
cp -f "$ROOT/apache/"*.example "$PREFIX/apache/" 2>/dev/null || true
echo "  Edit llm.* and runtime.cuda_visible_devices in config.json, then:"
echo "    sudo systemctl enable --now marker-server-api.service marker-server-ui.service"
