#!/bin/bash
# Runs on the HOST as the valheim user, started by valheim-bot-request.path when the
# Discord monitor drops a request file. It can only do three things:
#   check                    -> run check_update.sh now
#   restart                  -> restart the server (the one command valheim's sudoers
#                               rule allows); the unit's ExecStartPre installs any update
#   set <kind> <key> [value] -> change one world setting in world-settings.env, via
#                               valheim-world-settings.py, which only accepts known
#                               presets/modifiers/setkeys and values
set -euo pipefail

BOT_DIR="/home/valheim/bot"
REQUEST="$BOT_DIR/request"
LOG_FILE="/home/valheim/update_check.log"
CHECK_SCRIPT="/home/valheim/check_update.sh"
SETTINGS_SCRIPT="/home/valheim/valheim-world-settings.py"
SETTINGS_FILE="/home/valheim/world-settings.env"
SERVICE="valheimserver.service"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"
}

[ -f "$REQUEST" ] || exit 0
# Read one short line of lowercase words and delete the file first, so a bad request
# can't loop. Anything but a-z, 0-9 and spaces is dropped before it's used.
LINE=$(head -n 1 "$REQUEST" | head -c 100 | tr -cd 'a-z0-9 ')
rm -f "$REQUEST"
read -r ACTION ARG1 ARG2 ARG3 _ <<< "$LINE" || true

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
    set)
        RESULT=$(python3 "$SETTINGS_SCRIPT" --file "$SETTINGS_FILE" set "${ARG1:-}" "${ARG2:-}" "${ARG3:-}" 2>&1 || true)
        log "World setting from Discord ($ARG1 $ARG2 ${ARG3:-}): $RESULT"
        ;;
    *)
        log "WARN: ignored an unknown request from Discord: '$ACTION'"
        ;;
esac
