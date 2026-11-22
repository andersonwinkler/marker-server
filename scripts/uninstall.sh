#!/usr/bin/env bash
# Remove marker-server systemd units and install tree.
# Does NOT remove Marker itself (/opt/marker/…).
#
# Usage:
#   sudo ./scripts/uninstall.sh            # stop services, remove this VERSION prefix
#   sudo ./scripts/uninstall.sh --all      # remove every /opt/marker-server/* version
#   sudo ./scripts/uninstall.sh --purge    # also drop user, state dir, and Marker ACLs
#   sudo ./scripts/uninstall.sh --all --purge
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
      sed -n '2,10p' "$0"
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

echo "==> Stopping marker-server services"
systemctl stop marker-server-api.service marker-server-ui.service 2>/dev/null || true
systemctl disable marker-server-api.service marker-server-ui.service 2>/dev/null || true

echo "==> Removing systemd units"
rm -f /etc/systemd/system/marker-server-api.service \
      /etc/systemd/system/marker-server-ui.service
systemctl daemon-reload
systemctl reset-failed marker-server-api.service marker-server-ui.service 2>/dev/null || true

MARKER_HOME="$(resolve_marker_home)"

if [[ "$REMOVE_ALL" -eq 1 ]]; then
  echo "==> Removing all installs under /opt/marker-server"
  rm -rf /opt/marker-server
else
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
  echo "==> Purging state dir $MARKER_STATE_DIR"
  rm -rf "$MARKER_STATE_DIR"

  if [[ -n "$MARKER_HOME" && -d "$MARKER_HOME" ]]; then
    echo "==> Removing ACL entries for $MARKER_USER on $MARKER_HOME"
    if command -v setfacl >/dev/null 2>&1; then
      setfacl -R -x "u:${MARKER_USER}" "$MARKER_HOME" 2>/dev/null || true
      find "$MARKER_HOME" -type d -exec setfacl -d -x "u:${MARKER_USER}" {} + 2>/dev/null || true
    fi
  else
    echo "==> (no Marker home found to strip ACLs from)"
  fi

  if id -u "$MARKER_USER" >/dev/null 2>&1; then
    echo "==> Removing system user $MARKER_USER"
    userdel "$MARKER_USER" 2>/dev/null || userdel -r "$MARKER_USER" 2>/dev/null || true
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
