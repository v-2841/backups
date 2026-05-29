#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-backups}"
RUN_USER="${RUN_USER:-${SUDO_USER:-$(id -un)}}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
CONFIG_PATH="${CONFIG_PATH:-$PROJECT_DIR/config.toml}"
DRY_RUN="${DRY_RUN:-0}"

if [[ -z "$PYTHON_BIN" ]]; then
  echo "ERROR: python3 not found in PATH." >&2
  exit 1
fi

if [[ ! -f "$PROJECT_DIR/backup.py" ]]; then
  echo "ERROR: backup.py not found in project directory: $PROJECT_DIR" >&2
  exit 1
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "ERROR: config file not found: $CONFIG_PATH" >&2
  exit 1
fi

PROJECT_DIR_SH="$(printf "%q" "$PROJECT_DIR")"
PYTHON_BIN_SH="$(printf "%q" "$PYTHON_BIN")"
CONFIG_PATH_SH="$(printf "%q" "$CONFIG_PATH")"

tmp_service="$(mktemp)"
trap 'rm -f "$tmp_service"' EXIT

sed_replacement() {
  printf "%s" "$1" | sed -e 's/[\/&]/\\&/g' -e 's/#/\\#/g'
}

sed \
  -e "s#__RUN_USER__#$(sed_replacement "$RUN_USER")#g" \
  -e "s#__PROJECT_DIR__#$(sed_replacement "$PROJECT_DIR")#g" \
  -e "s#__PROJECT_DIR_SH__#$(sed_replacement "$PROJECT_DIR_SH")#g" \
  -e "s#__PYTHON_BIN_SH__#$(sed_replacement "$PYTHON_BIN_SH")#g" \
  -e "s#__CONFIG_PATH_SH__#$(sed_replacement "$CONFIG_PATH_SH")#g" \
  "$PROJECT_DIR/systemd/backups.service" > "$tmp_service"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY_RUN=1, not installing systemd units."
  echo
  echo "Would install /etc/systemd/system/${SERVICE_NAME}.service:"
  cat "$tmp_service"
  echo
  echo "Would install /etc/systemd/system/${SERVICE_NAME}.timer:"
  cat "$PROJECT_DIR/systemd/backups.timer"
  exit 0
fi

sudo install -m 0644 "$tmp_service" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo install -m 0644 "$PROJECT_DIR/systemd/backups.timer" "/etc/systemd/system/${SERVICE_NAME}.timer"
sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE_NAME}.timer"

echo "Installed ${SERVICE_NAME}.service and ${SERVICE_NAME}.timer"
systemctl list-timers "${SERVICE_NAME}.timer" --all --no-pager
