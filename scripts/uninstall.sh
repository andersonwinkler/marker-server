#!/usr/bin/env bash
# Remove marker-server systemd units and install tree.
# Does NOT remove Marker itself (/opt/marker/…).
#
# Usage:
#   sudo ./scripts/uninstall.sh            # stop services, remove this VERSION prefix
#   sudo ./scripts/uninstall.sh --all      # remove every /opt/marker-server/* version
#   sudo ./scripts/uninstall.sh --purge    # also drop user, state dir, and Marker ACLs
#   sudo ./scripts/uninstall.sh --all --purge
#
# Notes:
#   --all always targets /opt/marker-server (not a custom INSTALL_PREFIX).
#   A custom INSTALL_PREFIX must sit under /opt/marker-server/ and contain VERSION.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
PREFIX="${INSTALL_PREFIX:-/opt/marker-server/$VERSION}"
MARKER_USER="${MARKER_USER:-marker}"
MARKER_STATE_DIR="${MARKER_STATE_DIR:-/var/lib/marker-server}"
MARKER_HOME_DEFAULT="${MARKER_HOME:-}"
REMOVE_ALL=0
PURGE=0

for arg in "$@"; do
  case "$arg" in
    --all) REMOVE_ALL=1 ;;
    --purge) PURGE=1 ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      echo "Usage: $0 [--all] [--purge]" >&2
      exit 2
      ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "Re-running with sudo…"
  exec sudo env \
    INSTALL_PREFIX="$PREFIX" \
    MARKER_USER="$MARKER_USER" \
    MARKER_STATE_DIR="$MARKER_STATE_DIR" \
    MARKER_HOME="${MARKER_HOME:-}" \
    bash "$0" "$@"
fi

resolve_marker_home() {
  local home cfg
  if [[ -n "${MARKER_HOME_DEFAULT}" && -d "${MARKER_HOME_DEFAULT}" ]]; then
    echo "${MARKER_HOME_DEFAULT}"
    return 0
  fi
  for cfg in "$PREFIX/config.json" /opt/marker-server/current/config.json; do
    [[ -f "$cfg" ]] || continue
    home="$(python3 - "$cfg" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as fp:
    data = json.load(fp)
print(((data.get('marker') or {}).get('home') or '').strip())
PY
)"
    if [[ -n "$home" ]]; then
      echo "$home"
      return 0
    fi
  done
  echo ""
}

assert_safe_prefix() {
  local p
  # Resolve .. and symlinks before the allowlist check.
  p="$(realpath -m -- "$1")"
  case "$p" in
    /opt/marker-server/*) ;;
    *)
      echo "Refusing to delete INSTALL_PREFIX outside /opt/marker-server/: $p" >&2
      exit 1
      ;;
  esac
  if [[ "$p" == "/opt/marker-server" || "$p" == "/opt/marker-server/" ]]; then
    echo "Refusing to delete the parent /opt/marker-server (use --all)." >&2
    exit 1
  fi
  if [[ ! -f "$p/VERSION" ]]; then
    echo "Refusing to delete $p (no VERSION file — not a marker-server prefix)." >&2
    exit 1
  fi
}

stop_ocr_servers() {
  local cleanup=""
  for cleanup in \
    "$PREFIX/server/surya_cleanup.py" \
    /opt/marker-server/current/server/surya_cleanup.py \
    "$ROOT/server/surya_cleanup.py"
  do
    [[ -f "$cleanup" ]] || continue
    echo "==> Stopping Surya OCR keep-alive servers (via $cleanup)"
    if id -u "$MARKER_USER" >/dev/null 2>&1; then
      # Run as the service user so HOME points at state and docker group applies.
      sudo -u "$MARKER_USER" env HOME="$MARKER_STATE_DIR" \
        XDG_CACHE_HOME="$MARKER_STATE_DIR/.cache" \
        python3 "$cleanup" || true
    else
      HOME="$MARKER_STATE_DIR" python3 "$cleanup" || true
    fi
    return 0
  done
  echo "==> (no surya_cleanup.py found; OCR keep-alive may still be running)"
}

stop_user_processes() {
  if ! id -u "$MARKER_USER" >/dev/null 2>&1; then
    return 0
  fi
  if ! pgrep -u "$MARKER_USER" >/dev/null 2>&1; then
    return 0
  fi
  echo "==> Stopping remaining processes for $MARKER_USER"
  pgrep -u "$MARKER_USER" -a || true
  pkill -u "$MARKER_USER" 2>/dev/null || true
  sleep 1
  if pgrep -u "$MARKER_USER" >/dev/null 2>&1; then
    pkill -9 -u "$MARKER_USER" 2>/dev/null || true
    sleep 1
  fi
  if pgrep -u "$MARKER_USER" >/dev/null 2>&1; then
    echo "WARNING: processes still running for $MARKER_USER:" >&2
    pgrep -u "$MARKER_USER" -a >&2 || true
  fi
}

MARKER_HOME="$(resolve_marker_home)"

echo "==> Stopping marker-server services"
systemctl stop marker-server-api.service marker-server-ui.service 2>/dev/null || true
systemctl disable marker-server-api.service marker-server-ui.service 2>/dev/null || true

# Before deleting PREFIX / state: stop detached OCR servers while sentinels exist.
stop_ocr_servers

echo "==> Removing systemd units"
rm -f /etc/systemd/system/marker-server-api.service \
      /etc/systemd/system/marker-server-ui.service
systemctl daemon-reload
systemctl reset-failed marker-server-api.service marker-server-ui.service 2>/dev/null || true

if [[ "$REMOVE_ALL" -eq 1 ]]; then
  echo "==> Removing all installs under /opt/marker-server"
  if [[ -n "${INSTALL_PREFIX:-}" && "$PREFIX" != /opt/marker-server/* ]]; then
    echo "NOTE: --all removes /opt/marker-server only; custom INSTALL_PREFIX=$PREFIX is not deleted."
  fi
  rm -rf /opt/marker-server
else
  assert_safe_prefix "$PREFIX"
  PREFIX="$(realpath -m -- "$PREFIX")"
  echo "==> Removing install prefix $PREFIX"
  rm -rf "$PREFIX"
  if [[ -L /opt/marker-server/current ]]; then
    target="$(readlink -f /opt/marker-server/current 2>/dev/null || true)"
    if [[ -z "$target" || "$target" == "$PREFIX" || ! -e /opt/marker-server/current ]]; then
      rm -f /opt/marker-server/current
    fi
  fi
  if [[ -d /opt/marker-server ]] && [[ -z "$(find /opt/marker-server -mindepth 1 -maxdepth 1 2>/dev/null)" ]]; then
    rmdir /opt/marker-server 2>/dev/null || true
  fi
fi

if [[ "$PURGE" -eq 1 ]]; then
  stop_user_processes

  if [[ -n "$MARKER_HOME" && -d "$MARKER_HOME" ]]; then
    echo "==> Reclaiming Marker cache/hf ownership and stripping $MARKER_USER ACLs"
    if [[ -d "$MARKER_HOME/cache" ]]; then
      chown -R root:root "$MARKER_HOME/cache" || true
    fi
    if [[ -d "$MARKER_HOME/hf" ]]; then
      chown -R root:root "$MARKER_HOME/hf" || true
    fi
    # Undo group fallback from installs without setfacl (only files still
    # grouped as the service user — leave other admin groups alone).
    find "$MARKER_HOME" -group "$MARKER_USER" -exec chgrp root {} + 2>/dev/null || true
    if command -v setfacl >/dev/null 2>&1; then
      setfacl -R -x "u:${MARKER_USER}" "$MARKER_HOME" 2>/dev/null || true
      find "$MARKER_HOME" -type d -exec setfacl -d -x "u:${MARKER_USER}" {} + 2>/dev/null || true
    fi
  else
    echo "==> (no Marker home found to reclaim)"
  fi

  echo "==> Purging state dir $MARKER_STATE_DIR"
  rm -rf "$MARKER_STATE_DIR"

  if id -u "$MARKER_USER" >/dev/null 2>&1; then
    echo "==> Removing system user $MARKER_USER"
    if ! userdel "$MARKER_USER"; then
      echo "WARNING: userdel $MARKER_USER failed; leftover processes:" >&2
      pgrep -u "$MARKER_USER" -a >&2 || true
      exit 1
    fi
  fi
else
  echo "==> Keeping user $MARKER_USER and state $MARKER_STATE_DIR (pass --purge to remove)"
fi

echo
echo "Uninstalled marker-server."
echo "  Marker install under ${MARKER_HOME:-/opt/marker/…} was left untouched."
if [[ "$PURGE" -eq 0 ]]; then
  echo "  Re-install: ./scripts/install.sh"
fi
