#!/bin/bash
# Runs on the HOST as the valheim user, started by valheim-bot-request.path when the
# Discord monitor drops a request file. It can only do two things:
#   check    -> run check_update.sh now
#   restart  -> restart the server (the one command valheim's sudoers rule allows);
#               the unit's ExecStartPre installs any waiting update.
set -euo pipefail

BOT_DIR="/home/valheim/bot"
REQUEST="$BOT_DIR/request"
LOG_FILE="/home/valheim/update_check.log"
CHECK_SCRIPT="/home/valheim/check_update.sh"
SERVICE="valheimserver.service"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"
}

[ -f "$REQUEST" ] || exit 0
# Read one short word and delete the file first, so a bad request can't loop.
ACTION=$(head -c 16 "$REQUEST" | tr -cd 'a-z')
rm -f "$REQUEST"

case "$ACTION" in
    check)
        log "Update check requested from Discord."
        exec "$CHECK_SCRIPT"
        ;;
    restart)
        log "Restart requested from Discord."
        sudo /bin/systemctl restart "$SERVICE"
        log "Restart issued."
        ;;
    *)
        log "WARN: ignored an unknown request from Discord: '$ACTION'"
        ;;
esac
