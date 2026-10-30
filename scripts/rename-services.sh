#!/usr/bin/env bash
# Forex Factory: one-time rename of the systemd services (safe to run again, does nothing when already renamed).
#
#   flask.service         ->  forexfactory-api.service   (website + API)
#   telegram-bot.service  ->  forexfactory-bot.service   (Telegram bot)
#
# Only units whose WorkingDirectory is this project's folder are touched, so another application's
# flask.service / telegram-bot.service is never renamed. Data, .env and the virtualenv stay where they are.
#
#   sudo bash scripts/rename-services.sh [APP_DIR]        (default APP_DIR: /opt/forexfactory-api)
set -uo pipefail

APP_DIR="${1:-/opt/forexfactory-api}"
SYSTEMD_DIR="${HSM_SYSTEMD_DIR:-/etc/systemd/system}"
[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo "Run as root (sudo)." >&2; exit 1; }

belongs() { [[ -f "$SYSTEMD_DIR/$1.service" ]] && grep -qE "^WorkingDirectory=${APP_DIR}/?$" "$SYSTEMD_DIR/$1.service"; }
changed=0

rename_unit() {   # rename_unit OLD NEW
  local old="$1" new="$2"
  belongs "$old" || return 0
  changed=1
  if [[ ! -f "$SYSTEMD_DIR/$new.service" ]]; then
    sed -e 's/^After=\(.*\)\bflask\.service/After=\1forexfactory-api.service/' \
        "$SYSTEMD_DIR/$old.service" > "$SYSTEMD_DIR/$new.service"
  fi
  systemctl stop "$old.service" 2>/dev/null || true          # same port: the old one must stop first
  systemctl disable "$old.service" 2>/dev/null || true
  rm -f "$SYSTEMD_DIR/$old.service"
  systemctl daemon-reload
  systemctl enable "$new.service" >/dev/null 2>&1 || true
  systemctl restart "$new.service"
  echo "  $old.service  ->  $new.service  ($(systemctl is-active "$new.service" 2>/dev/null))"
}

echo "Forex Factory: renaming services in $APP_DIR ..."
rename_unit flask forexfactory-api
rename_unit telegram-bot forexfactory-bot
if [[ $changed -eq 0 ]]; then
  echo "  nothing to rename (already done, or not installed)."
else
  echo "Done. From now on:"
  echo "  sudo systemctl restart forexfactory-api.service forexfactory-bot.service"
fi
