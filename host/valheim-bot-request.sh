#!/bin/bash
# Runs on the HOST as the valheim user, started by valheim-bot-request.path when the
# Discord monitor drops a request file. It can only do four things:
#   check                    -> run check_update.sh now
#   restart                  -> restart the server (the one command valheim's sudoers
#                               rule allows); the unit's ExecStartPre installs any update
#   set <kind> <key> [value] -> change one world setting in world-settings.env, via
#                               valheim-world-settings.py, which only accepts known
#                               presets/modifiers/setkeys and values
#   restore <14 digits>      -> put back one of Valheim's own world backups (named by
#                               its date and time): stop the server, keep the current
#                               world as <world>_backup_prerestore-<now>, copy the backup
#                               in, start again. Needs sudoers to allow stop and start.
set -euo pipefail

# (The VDM_* variables only exist for the repository's tests; systemd never sets them.)
BOT_DIR="${VDM_BOT_DIR:-/home/valheim/bot}"
REQUEST="$BOT_DIR/request"
LOG_FILE="${VDM_LOG_FILE:-/home/valheim/update_check.log}"
CHECK_SCRIPT="/home/valheim/check_update.sh"
SETTINGS_SCRIPT="/home/valheim/valheim-world-settings.py"
SETTINGS_FILE="/home/valheim/world-settings.env"
SERVICE="valheimserver.service"
WORLDS="${VDM_WORLDS:-/home/valheim/valheim_save_data/worlds_local}"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"
}

# The backup whose date and time (the digits after "_backup_") are $1, as a folder
# name (Valheim 1.0) or a .db/.fwl stem (older servers). Prints nothing unless exactly one.
find_backup() {
    local found=() entry name stem digits
    for entry in "$WORLDS"/*_backup_*; do
        [ -e "$entry" ] || continue
        name=$(basename "$entry")
        stem="$name"
        if [ -f "$entry" ]; then
            case "$name" in *.db|*.fwl) stem="${name%.*}" ;; *) continue ;; esac
        fi
        digits=$(echo "${stem#*_backup_}" | tr -cd '0-9')
        digits="${digits:0:14}"
        if [ "$digits" = "$1" ] && [[ ! " ${found[*]:-} " == *" $stem "* ]]; then
            found+=("$stem")
        fi
    done
    [ "${#found[@]}" -eq 1 ] && echo "${found[0]}"
    return 0
}

restore() {
    local id="$1" stem world safe legacy=0 ext
    if ! [[ "$id" =~ ^[0-9]{14}$ ]]; then
        log "ERROR: restore: bad backup id '$id'"
        return
    fi
    stem=$(find_backup "$id")
    if [ -z "$stem" ]; then
        log "ERROR: restore: no single backup in $WORLDS matches $id"
        return
    fi
    world="${stem%%_backup_*}"
    if [ -d "$WORLDS/$stem" ] && [ -d "$WORLDS/$world" ]; then
        legacy=0
    elif [ -f "$WORLDS/$stem.db" ] && [ -f "$WORLDS/$stem.fwl" ] && [ -f "$WORLDS/$world.db" ]; then
        legacy=1
    else
        log "ERROR: restore: $stem doesn't match the live world $world in $WORLDS"
        return
    fi
    safe="${world}_backup_prerestore-$(date '+%Y%m%d-%H%M%S')"
    log "Restore requested from Discord: $stem. Stopping the server."
    if ! sudo /bin/systemctl stop "$SERVICE"; then
        log "ERROR: restore: couldn't stop the server (does sudoers allow 'systemctl stop $SERVICE'?); nothing changed"
        return
    fi
    for _ in $(seq 1 60); do
        systemctl is-active --quiet "$SERVICE" || break
        sleep 2
    done
    if systemctl is-active --quiet "$SERVICE"; then
        log "ERROR: restore: the server didn't stop; nothing changed"
        return
    fi
    # Copy the backup next to the live world first: if that fails, nothing has changed.
    # Then two renames swap it in, and the old world is kept as a backup of its own. If a
    # rename fails, the old world is put back, so the server never starts on a missing
    # world (Valheim would make a new, empty one with that name).
    local ok=1
    if [ "$legacy" -eq 0 ]; then
        rm -rf "${WORLDS:?}/$world.restoring"
        if ! cp -a "$WORLDS/$stem" "$WORLDS/$world.restoring"; then
            ok=0
        elif ! mv "$WORLDS/$world" "$WORLDS/$safe"; then
            ok=0
        elif ! mv "$WORLDS/$world.restoring" "$WORLDS/$world"; then
            ok=0
            mv "$WORLDS/$safe" "$WORLDS/$world" || true
        fi
        rm -rf "${WORLDS:?}/$world.restoring"
    else
        if cp -a "$WORLDS/$stem.db" "$WORLDS/$world.db.restoring" &&
                cp -a "$WORLDS/$stem.fwl" "$WORLDS/$world.fwl.restoring"; then
            local moved=()
            for ext in db fwl; do
                if [ -f "$WORLDS/$world.$ext" ] && ! mv "$WORLDS/$world.$ext" "$WORLDS/$safe.$ext"; then
                    ok=0; break
                fi
                moved+=("$ext")
                if ! mv "$WORLDS/$world.$ext.restoring" "$WORLDS/$world.$ext"; then
                    ok=0; break
                fi
            done
            if [ "$ok" -eq 0 ]; then              # undo: the old pair back in place
                for ext in "${moved[@]}"; do
                    [ -f "$WORLDS/$safe.$ext" ] && mv -f "$WORLDS/$safe.$ext" "$WORLDS/$world.$ext"
                done
            fi
        else
            ok=0
        fi
        rm -f "$WORLDS/$world.db.restoring" "$WORLDS/$world.fwl.restoring"
    fi
    if [ "$ok" -eq 1 ]; then
        log "Restore done: $stem (the world before it is saved as $safe)"
    elif [ -e "$WORLDS/$world" ] || [ -f "$WORLDS/$world.db" ]; then
        log "ERROR: restore: couldn't copy $stem into place; the world was left as it was"
    else
        log "ERROR: restore: the world $world is missing after a failed restore; the server stays stopped. Rename $safe back to $world in $WORLDS, then start the server"
        return
    fi
    sudo /bin/systemctl start "$SERVICE" || log "ERROR: restore: couldn't start the server again"
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
    restore)
        restore "${ARG1:-}"
        ;;
    set)
        RESULT=$(python3 "$SETTINGS_SCRIPT" --file "$SETTINGS_FILE" set "${ARG1:-}" "${ARG2:-}" "${ARG3:-}" 2>&1 || true)
        log "World setting from Discord ($ARG1 $ARG2 ${ARG3:-}): $RESULT"
        ;;
    *)
        log "WARN: ignored an unknown request from Discord: '$ACTION'"
        ;;
esac
