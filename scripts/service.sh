#!/usr/bin/env bash
# Manage TeleRocket as a systemd *user* service (starts automatically, restarts on failure).
#
#   scripts/service.sh install     install + start, and start on every login/boot
#   scripts/service.sh uninstall   stop and remove the service
#   scripts/service.sh status      show whether it is running
#   scripts/service.sh logs        follow the logs (Ctrl+C to stop following)
#   scripts/service.sh restart     restart (e.g. after editing .env)
#   scripts/service.sh print-unit  show the unit file that install would write
set -euo pipefail

NAME=telerocket
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$UNIT_DIR/$NAME.service"

unit() {
  cat <<UNIT
[Unit]
Description=TeleRocket (Rocket.Chat <-> Telegram bridge)
# Keep retrying forever; the app itself waits for the network/proxy.
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart="$PROJECT_DIR/.venv/bin/telerocket" run
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=15
# Exit code 2 = bad config or rejected credentials: fix .env, then restart.
RestartPreventExitStatus=2
UMask=0077
NoNewPrivileges=yes

[Install]
WantedBy=default.target
UNIT
}

need_env() {
  if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    echo "No .env found in $PROJECT_DIR. Create it first (see README.md)." >&2
    exit 1
  fi
  chmod 600 "$PROJECT_DIR/.env"
}

case "${1:-}" in
  install)
    need_env
    command -v uv >/dev/null || { echo "uv not found in PATH" >&2; exit 1; }
    (cd "$PROJECT_DIR" && uv sync --frozen --quiet)
    mkdir -p "$UNIT_DIR"
    unit > "$UNIT_FILE"
    systemctl --user daemon-reload
    systemctl --user enable --now "$NAME"
    echo "Installed and started. It will start automatically when you log in."
    echo "Logs:   scripts/service.sh logs"
    echo "Status: scripts/service.sh status"
    if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]]; then
      echo
      echo "Tip: to also run it right after boot, before you log in, run once:"
      echo "  sudo loginctl enable-linger $USER"
    fi
    ;;
  uninstall)
    systemctl --user disable --now "$NAME" 2>/dev/null || true
    rm -f "$UNIT_FILE"
    systemctl --user daemon-reload
    echo "Removed."
    ;;
  status)  systemctl --user status "$NAME" --no-pager ;;
  logs)    journalctl --user -u "$NAME" -f -n 50 ;;
  restart) systemctl --user restart "$NAME" && echo "Restarted." ;;
  print-unit) unit ;;
  *)
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
