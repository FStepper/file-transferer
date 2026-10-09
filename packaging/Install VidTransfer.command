#!/bin/bash
# VidTransfer installer for macOS – double-click it in Finder (it sits next to
# VidTransfer.app in the downloaded folder).
#
# - copies VidTransfer.app to /Applications (asks for an admin password if needed)
# - removes the quarantine flag, because the app is not notarized by Apple
# - asks for the cloud folder (default: Google Drive), this Mac's name, the start mode and whether
#   the app should start at login, and stores them via the app's own
#   "config" command (so this script never edits settings files itself)
#
# Written for the bash 3.2 that ships with macOS (no `set -u`, no associative
# arrays, no ${var,,}).
#
# Testing without a Mac (prints macOS-only commands instead of running them):
#   VT_DRY_RUN=1 VT_SRC_APP=<any folder> VT_DEST_DIR=<folder> VT_APP_BIN=<vidtransfer command> \
#     bash "Install VidTransfer.command"

cd "$(dirname "$0")" || exit 1

DRY="${VT_DRY_RUN:-}"
SRC_APP="${VT_SRC_APP:-$PWD/VidTransfer.app}"
DEST_DIR="${VT_DEST_DIR:-/Applications}"
DEST_APP="$DEST_DIR/VidTransfer.app"
NEED_SUDO=""

say() { printf '%s\n' "$*"; }

finish() {
    say ""
    printf 'Press Enter to close … '
    read -r _ || true
    exit "${1:-0}"
}

fail() {
    say ""
    say "ERROR: $*"
    finish 1
}

# ask "Question" "default" -> answer in REPLY
ask() {
    if [ -n "$2" ]; then
        printf '%s [%s]: ' "$1" "$2"
    else
        printf '%s: ' "$1"
    fi
    if ! IFS= read -r REPLY; then
        # End of input (e.g. Ctrl-D): stop instead of asking the same question forever
        say ""
        fail "Input ended – installation cancelled."
    fi
    REPLY="${REPLY%$'\r'}"          # pasted text may carry a Windows line ending
    [ -z "$REPLY" ] && REPLY="$2"
}

# yes_no "Question" "y|n" -> exit status 0 for yes
yes_no() {
    ask "$1 (y/n)" "$2"
    case "$REPLY" in
        [yY]*) return 0 ;;
        *) return 1 ;;
    esac
}

# Run a command that changes the system; only printed in a dry run
run() {
    if [ -n "$DRY" ]; then
        say "  [dry run] $*"
    else
        "$@"
    fi
}

# Like run, but with sudo if the destination is not writable for this user
priv() {
    if [ -n "$NEED_SUDO" ]; then
        run sudo "$@"
    else
        run "$@"
    fi
}

# The installed app's command line (settings are changed through the app itself)
vt() {
    if [ -n "${VT_APP_BIN:-}" ]; then
        # shellcheck disable=SC2086  # VT_APP_BIN may be a command with arguments
        $VT_APP_BIN "$@"
    else
        "$DEST_APP/Contents/MacOS/VidTransfer" "$@"
    fi
}

# A setting from the app, without a trailing carriage return
vt_get() {
    local value
    value="$(vt config --get "$1" 2>/dev/null)"
    printf '%s' "${value%$'\r'}"
}

expand_home() {
    # shellcheck disable=SC2088  # matching a literal "~" typed by the user is the point
    case "$1" in
        "~") printf '%s' "$HOME" ;;
        "~/"*) printf '%s/%s' "$HOME" "${1#\~/}" ;;
        *) printf '%s' "$1" ;;
    esac
}

say "Install VidTransfer"
say "==================="
say ""

if [ -z "$DRY" ] && [ "$(uname)" != "Darwin" ]; then
    fail "This script is meant for macOS."
fi
[ -d "$SRC_APP" ] || fail "VidTransfer.app was not found next to this script ($SRC_APP)."

# ---------------------------------------------------------------- copy the app
if [ -z "$DRY" ] && pgrep -x VidTransfer >/dev/null 2>&1; then
    say "VidTransfer is still running and will be quit …"
    osascript -e 'quit app "VidTransfer"' >/dev/null 2>&1
    sleep 2
fi

if [ -z "$DRY" ] && [ ! -w "$DEST_DIR" ]; then
    say "Copying to $DEST_DIR needs an administrator password."
    NEED_SUDO=1
fi

say "Copying VidTransfer to $DEST_DIR …"
if [ -e "$DEST_APP" ]; then
    priv rm -rf "$DEST_APP" || fail "The old version could not be removed."
fi
priv ditto "$SRC_APP" "$DEST_APP" || fail "Copying failed."

# Not notarized: without this, macOS would refuse to open the app ("damaged")
priv xattr -dr com.apple.quarantine "$DEST_APP" 2>/dev/null
say "Copied."

# ---------------------------------------------------------------- cloud folder (default: Google Drive)
say ""
say "1) Cloud folder for deliveries"
say "   Important: choose the same folder on every Mac. With different accounts,"
say "   share one folder (editor) and add it to \"My Drive\" on the other accounts –"
say "   or use a shared drive."

LOCATIONS=()
while IFS= read -r line; do
    line="${line%$'\r'}"
    [ -n "$line" ] && LOCATIONS+=("$line")
done < <(vt config --list-drives 2>/dev/null)

CURRENT="$(vt_get drive)"
if [ ${#LOCATIONS[@]} -eq 0 ]; then
    say "   No Google Drive folder found. Please enter the path of the cloud folder."
else
    i=1
    for loc in "${LOCATIONS[@]}"; do
        say "   $i) $loc"
        i=$((i + 1))
    done
fi
[ -n "$CURRENT" ] && say "   Currently set: $CURRENT"

DEFAULT="$CURRENT"
[ -z "$DEFAULT" ] && [ ${#LOCATIONS[@]} -gt 0 ] && DEFAULT="1"
while true; do
    ask "   Choose a number or enter a path" "$DEFAULT"
    CHOICE="$REPLY"
    BASE=""
    FROM_LIST=""
    case "$CHOICE" in
        ''|*[!0-9]*) BASE="$(expand_home "$CHOICE")" ;;
        *)
            if [ "$CHOICE" -ge 1 ] && [ "$CHOICE" -le ${#LOCATIONS[@]} ]; then
                BASE="${LOCATIONS[$((CHOICE - 1))]}"
                FROM_LIST=1
            fi
            ;;
    esac
    if [ -n "$BASE" ] && [ -d "$BASE" ]; then
        break
    fi
    say "   This folder does not exist – please try again."
done

DRIVE="$BASE"
if [ -n "$FROM_LIST" ]; then
    ask "   Subfolder for deliveries (empty = right here)" "VidTransfer"
    if [ -n "$REPLY" ]; then
        DRIVE="$BASE/$REPLY"
        mkdir -p "$DRIVE" || fail "Folder could not be created: $DRIVE"
    fi
fi
vt config --drive "$DRIVE" || fail "The folder could not be set."
say "   → $DRIVE"

# ---------------------------------------------------------------- name, mode, inbox, login
say ""
say "2) Name of this Mac (shown to the receiver)"
ask "   Name" "$(vt_get sender)"
vt config --sender "$REPLY" || fail "The name could not be saved."

say ""
say "3) What is this Mac mainly used for?"
if [ "$(vt_get mode)" = "receive" ]; then MODE_DEFAULT="r"; else MODE_DEFAULT="s"; fi
ask "   s = send, r = receive" "$MODE_DEFAULT"
case "$REPLY" in
    [rR]*) MODE="receive" ;;
    *) MODE="send" ;;
esac
vt config --mode "$MODE" || fail "The mode could not be saved."

if [ "$MODE" = "receive" ]; then
    say ""
    say "4) Where should received deliveries be saved?"
    ask "   Folder" "$(vt_get inbox)"
    vt config --inbox "$(expand_home "$REPLY")" || fail "The folder could not be set."
    LOGIN_DEFAULT="y"
else
    LOGIN_DEFAULT="n"
fi

say ""
if yes_no "Start VidTransfer automatically at login? (recommended for the receiving Mac)" "$LOGIN_DEFAULT"; then
    vt config --login on
else
    vt config --login off
fi

# ---------------------------------------------------------------- done
say ""
say "VidTransfer is installed (Applications › VidTransfer)."
if yes_no "Open it now?" "y"; then
    run open "$DEST_APP"
fi
finish 0
