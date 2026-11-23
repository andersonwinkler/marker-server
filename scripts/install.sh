#!/usr/bin/env bash
# Install marker-server into /opt/marker-server/<VERSION>.
# Does NOT install Marker itself — set marker.home in config.json.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
PREFIX="${INSTALL_PREFIX:-/opt/marker-server/$VERSION}"
MARKER_HOME_DEFAULT="${MARKER_HOME:-/opt/marker/2.2.0}"
MARKER_USER="${MARKER_USER:-marker}"
MARKER_STATE_DIR="${MARKER_STATE_DIR:-/var/lib/marker-server}"
MARKER_DOCKER="${MARKER_DOCKER:-}"
if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON_BIN="$PYTHON_BIN"
elif command -v python3.12 >/dev/null 2>&1; then
  PYTHON_BIN=python3.12
else
  PYTHON_BIN=python3
fi

echo "Installing marker-server $VERSION → $PREFIX"
echo "  (will point at Marker home: $MARKER_HOME_DEFAULT)"
echo "  (service user: $MARKER_USER, state: $MARKER_STATE_DIR)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Re-running with sudo…"
  exec sudo env \
    INSTALL_PREFIX="$PREFIX" \
    MARKER_HOME="$MARKER_HOME_DEFAULT" \
    MARKER_USER="$MARKER_USER" \
    MARKER_STATE_DIR="$MARKER_STATE_DIR" \
    MARKER_DOCKER="$MARKER_DOCKER" \
    PYTHON_BIN="$PYTHON_BIN" \
    MARKER_OPENAI_BASE_URL="${MARKER_OPENAI_BASE_URL:-}" \
    MARKER_OPENAI_MODEL="${MARKER_OPENAI_MODEL:-}" \
    MARKER_OPENAI_API_KEY="${MARKER_OPENAI_API_KEY:-}" \
    MARKER_SERVER_API_KEY="${MARKER_SERVER_API_KEY:-}" \
    LLAMA_CPP_BINARY="${LLAMA_CPP_BINARY:-}" \
    bash "$0" "$@"
fi

MARKER_HOME="${MARKER_HOME:-$MARKER_HOME_DEFAULT}"

mkdir -p "$MARKER_STATE_DIR"
chmod 755 "$MARKER_STATE_DIR"

# Dedicated unprivileged account (fixed home across version upgrades).
if ! id -u "$MARKER_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$MARKER_STATE_DIR" --shell /sbin/nologin \
    --comment 'marker-server' "$MARKER_USER"
  echo "Created system user $MARKER_USER"
else
  usermod -d "$MARKER_STATE_DIR" "$MARKER_USER" 2>/dev/null || true
fi
chown "$MARKER_USER:$MARKER_USER" "$MARKER_STATE_DIR"
chmod 700 "$MARKER_STATE_DIR"

# GPU device nodes (not Docker — that is effectively root; see below).
for grp in video render; do
  if getent group "$grp" >/dev/null 2>&1; then
    usermod -aG "$grp" "$MARKER_USER" || true
  fi
done

mkdir -p "$PREFIX"/{bin,server,uploads,apache,docs}
cp -a "$ROOT/server/." "$PREFIX/server/"
cp -a "$ROOT/bin/." "$PREFIX/bin/"
chmod 755 "$PREFIX/bin/"*.sh
chmod 644 "$PREFIX/server/"*.py

CONFIG="$PREFIX/config.json"
ensure_api_key() {
  # Fill auth.api_key when missing/empty (new install or upgrade).
  "$PYTHON_BIN" - "$1" "${MARKER_SERVER_API_KEY:-}" <<'PY'
import json, secrets, sys
path, override = sys.argv[1:3]
with open(path, encoding='utf-8') as fp:
    data = json.load(fp)
auth = data.setdefault('auth', {})
changed = False
if override.strip():
    if auth.get('api_key') != override:
        auth['api_key'] = override
        changed = True
elif not str(auth.get('api_key') or '').strip():
    auth['api_key'] = secrets.token_urlsafe(32)
    changed = True
if changed:
    with open(path, 'w', encoding='utf-8') as fp:
        json.dump(data, fp, indent=2)
        fp.write('\n')
    print('updated')
else:
    print('unchanged')
PY
}

if [[ ! -f "$CONFIG" ]]; then
  cp "$ROOT/config.example.json" "$CONFIG"
  "$PYTHON_BIN" - "$CONFIG" "$MARKER_HOME" "${LLAMA_CPP_BINARY:-}" \
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
data.setdefault('auth', {})
with open(path, 'w', encoding='utf-8') as fp:
    json.dump(data, fp, indent=2)
    fp.write('\n')
PY
  ensure_api_key "$CONFIG" >/dev/null
  echo "Wrote $CONFIG (auth.api_key set; keep config mode 640)"
else
  echo "Keeping existing $CONFIG"
  KEY_STATE="$(ensure_api_key "$CONFIG")"
  if [[ "$KEY_STATE" == "updated" ]]; then
    echo "  Set auth.api_key (was missing/empty, or MARKER_SERVER_API_KEY override)"
  fi
fi
cp -f "$ROOT/config.example.json" "$PREFIX/config.example.json"
cp -f "$ROOT/README.md" "$PREFIX/README.md"
cp -f "$ROOT/VERSION" "$PREFIX/VERSION"
cp -a "$ROOT/docs/." "$PREFIX/docs/"
cp -f "$ROOT/apache/"*.example "$PREFIX/apache/" 2>/dev/null || true

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
  "$PYTHON_BIN" -m venv "$PREFIX/venv"
fi
# shellcheck disable=SC1091
source "$PREFIX/venv/bin/activate"
pip install -U pip setuptools wheel
pip install "${SERVER_DEPS[@]}"
deactivate

# --- permissions: root owns code; marker owns runtime dirs only ---
chown -R root:root "$PREFIX"
chmod -R u=rwX,go=rX "$PREFIX"
chown -R "$MARKER_USER:$MARKER_USER" "$PREFIX/uploads"
chmod 700 "$PREFIX/uploads"
# Config readable by service, not writable.
chown root:"$MARKER_USER" "$CONFIG"
chmod 640 "$CONFIG"

if [[ -d "$MARKER_HOME" ]]; then
  # Undo older installers that ran chmod -R o-rwx on the whole Marker tree
  # (CLI became unusable for everyone except root/marker). Skip cache/hf.
  find "$MARKER_HOME" \
    \( -path "$MARKER_HOME/cache" -o -path "$MARKER_HOME/hf" \) -prune \
    -o -exec chmod o+rX {} + 2>/dev/null || true
  # Grant the service user read/execute via ACL (+ default ACL for new files),
  # or group fallback when setfacl is unavailable.
  if command -v setfacl >/dev/null 2>&1; then
    setfacl -R -m "u:${MARKER_USER}:rX" "$MARKER_HOME" || true
    find "$MARKER_HOME" -type d -exec setfacl -d -m "u:${MARKER_USER}:rX" {} + 2>/dev/null || true
    # New files under marker.home should stay other-readable (not other::---).
    setfacl -d -m 'o::rX' "$MARKER_HOME" 2>/dev/null || true
  else
    chgrp -R "$MARKER_USER" "$MARKER_HOME" || true
    chmod -R g+rX "$MARKER_HOME" || true
  fi
  # Secrets live under cache/ and hf/ — lock those only.
  mkdir -p "$MARKER_HOME/cache" "$MARKER_HOME/hf"
  chown -R "$MARKER_USER:$MARKER_USER" "$MARKER_HOME/cache" "$MARKER_HOME/hf"
  chmod 700 "$MARKER_HOME/cache" "$MARKER_HOME/hf"
fi

# Docker group ≈ root. Opt out with MARKER_DOCKER=0; force with MARKER_DOCKER=1.
NEED_DOCKER=0
if [[ "$MARKER_DOCKER" == "0" ]]; then
  NEED_DOCKER=0
elif [[ "$MARKER_DOCKER" == "1" ]]; then
  NEED_DOCKER=1
elif [[ -f "$CONFIG" ]]; then
  BACKEND="$("$PYTHON_BIN" - "$CONFIG" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as fp:
    cfg = json.load(fp)
backend = ((cfg.get('surya') or {}).get('inference_backend'))
print('' if backend is None else str(backend).strip().lower())
PY
)"
  if [[ -z "$BACKEND" || "$BACKEND" == "vllm" ]]; then
    NEED_DOCKER=1
  fi
fi
if [[ "$NEED_DOCKER" -eq 1 ]]; then
  if getent group docker >/dev/null 2>&1; then
    usermod -aG docker "$MARKER_USER" || true
    echo "NOTE: $MARKER_USER added to docker (needed for Surya vLLM)."
    echo "      Members of docker can effectively become root via containers."
    echo "      Prefer MARKER_DOCKER=0 + surya.inference_backend=llamacpp to avoid this."
  else
    echo "WARNING: docker group missing; vLLM backend may fail until Docker is installed."
  fi
else
  echo "Skipping docker group (MARKER_DOCKER=0 or non-vLLM backend)."
fi

UNIT_DIR=/etc/systemd/system
sed -e "s|/opt/marker-server/0.1.0|$PREFIX|g" \
    -e "s|^User=marker$|User=$MARKER_USER|" \
    -e "s|^Group=marker$|Group=$MARKER_USER|" \
    -e "s|/var/lib/marker-server|$MARKER_STATE_DIR|g" \
  "$ROOT/systemd/marker-server-api.service" > "$UNIT_DIR/marker-server-api.service"
sed -e "s|/opt/marker-server/0.1.0|$PREFIX|g" \
    -e "s|^User=marker$|User=$MARKER_USER|" \
    -e "s|^Group=marker$|Group=$MARKER_USER|" \
    -e "s|/var/lib/marker-server|$MARKER_STATE_DIR|g" \
  "$ROOT/systemd/marker-server-ui.service" > "$UNIT_DIR/marker-server-ui.service"

ln -sfn "$PREFIX" /opt/marker-server/current
systemctl daemon-reload

AUTH_SET="$("$PYTHON_BIN" - "$CONFIG" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as fp:
    data = json.load(fp)
key = str(((data.get('auth') or {}).get('api_key') or '')).strip()
print('yes' if key else 'no')
PY
)"

echo
echo "Installed marker-server $VERSION"
echo "  Prefix:  $PREFIX"
echo "  Config:  $CONFIG (root:$MARKER_USER mode 640)"
if [[ "$AUTH_SET" == "yes" ]]; then
  echo "  Auth:    API key enabled (config.json → auth.api_key)"
  echo "           Clients / UI: Authorization: Bearer <key>"
  echo "           UI stores the key in the browser (session by default)"
  echo "           Off localhost: serve over HTTPS"
  if "$PYTHON_BIN" - "$CONFIG" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as fp:
    auth = (json.load(fp).get('auth') or {})
sys.exit(0 if auth.get('ui_password') not in (None, '') else 1)
PY
  then
    echo "           NOTE: auth.ui_password is present but ignored (UI uses api_key only)"
  fi
  echo "           Show key: sudo python3 -c \"import json;print(json.load(open('$CONFIG'))['auth']['api_key'])\""
else
  echo "  Auth:    WARNING — auth.api_key is empty; API is unauthenticated"
fi
echo "  User:    $MARKER_USER (home $MARKER_STATE_DIR)"
echo "  Marker:  marker.home → $MARKER_HOME"
if [[ ! -x "$MARKER_HOME/venv/bin/python" ]]; then
  echo "  WARNING: Marker python missing at $MARKER_HOME/venv/bin/python"
  echo "           Install vanilla Marker first, then set marker.home in config.json."
fi
echo "  Edit llm.* and runtime.cuda_visible_devices in config.json, then:"
echo "    sudo systemctl enable --now marker-server-api.service marker-server-ui.service"
