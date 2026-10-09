#!/bin/bash
# VidTransfer installer for macOS – double-click it in Finder (it sits next to
# VidTransfer.app in the downloaded folder).
#
# - copies VidTransfer.app to /Applications (asks for an admin password if needed)
# - removes the quarantine flag, because the app is not notarized by Apple
# - asks for the Google Drive folder, this Mac's name, the start mode and whether
#   the app should start at login, and stores them via the app's own
#   "config" command (so this script never edits settings files itself)
#
# Written for the bash 3.2 that ships with macOS (no `set -u`, no associative
# arrays, no ${var,,}).
#
# Testing without a Mac (prints macOS-only commands instead of running them):
#   VT_DRY_RUN=1 VT_SRC_APP=<any folder> VT_DEST_DIR=<folder> VT_APP_BIN=<vidtransfer command> \
#     bash "VidTransfer installieren.command"

cd "$(dirname "$0")" || exit 1

DRY="${VT_DRY_RUN:-}"
SRC_APP="${VT_SRC_APP:-$PWD/VidTransfer.app}"
DEST_DIR="${VT_DEST_DIR:-/Applications}"
DEST_APP="$DEST_DIR/VidTransfer.app"
NEED_SUDO=""

say() { printf '%s\n' "$*"; }

finish() {
    say ""
    printf 'Zum Schließen Enter drücken … '
    read -r _ || true
    exit "${1:-0}"
}

fail() {
    say ""
    say "FEHLER: $*"
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
        fail "Eingabe beendet – Installation abgebrochen."
    fi
    REPLY="${REPLY%$'\r'}"          # pasted text may carry a Windows line ending
    [ -z "$REPLY" ] && REPLY="$2"
}

# yes_no "Question" "j|n" -> exit status 0 for yes
yes_no() {
    ask "$1 (j/n)" "$2"
    case "$REPLY" in
        [jJyY]*) return 0 ;;
        *) return 1 ;;
    esac
}

# Run a command that changes the system; only printed in a dry run
run() {
    if [ -n "$DRY" ]; then
        say "  [Probelauf] $*"
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

say "VidTransfer installieren"
say "========================"
say ""

if [ -z "$DRY" ] && [ "$(uname)" != "Darwin" ]; then
    fail "Dieses Skript ist für macOS gedacht."
fi
[ -d "$SRC_APP" ] || fail "VidTransfer.app wurde neben diesem Skript nicht gefunden ($SRC_APP)."

# ---------------------------------------------------------------- copy the app
if [ -z "$DRY" ] && pgrep -x VidTransfer >/dev/null 2>&1; then
    say "VidTransfer läuft noch und wird beendet …"
    osascript -e 'quit app "VidTransfer"' >/dev/null 2>&1
    sleep 2
fi

if [ -z "$DRY" ] && [ ! -w "$DEST_DIR" ]; then
    say "Zum Kopieren nach $DEST_DIR wird das Passwort eines Administrators benötigt."
    NEED_SUDO=1
fi

say "Kopiere VidTransfer nach $DEST_DIR …"
if [ -e "$DEST_APP" ]; then
    priv rm -rf "$DEST_APP" || fail "Die alte Version konnte nicht entfernt werden."
fi
priv ditto "$SRC_APP" "$DEST_APP" || fail "Kopieren fehlgeschlagen."

# Not notarized: without this, macOS would refuse to open the app ("beschädigt")
priv xattr -dr com.apple.quarantine "$DEST_APP" 2>/dev/null
say "Fertig kopiert."

# ---------------------------------------------------------------- Google Drive folder
say ""
say "1) Google-Drive-Ordner für die Lieferungen"
say "   Wichtig: Auf allen Macs muss derselbe Ordner gewählt werden. Bei verschiedenen"
say "   Google-Konten einen Ordner freigeben (Bearbeiter) und auf den anderen Konten"
# shellcheck disable=SC1111  # German quotation marks in the text
say "   zu „Meine Ablage“ hinzufügen – oder eine geteilte Ablage verwenden."

LOCATIONS=()
while IFS= read -r line; do
    line="${line%$'\r'}"
    [ -n "$line" ] && LOCATIONS+=("$line")
done < <(vt config --list-drives 2>/dev/null)

CURRENT="$(vt_get drive)"
if [ ${#LOCATIONS[@]} -eq 0 ]; then
    say "   Kein Google-Drive-Ordner gefunden. Läuft Google Drive und ist es angemeldet?"
else
    i=1
    for loc in "${LOCATIONS[@]}"; do
        say "   $i) $loc"
        i=$((i + 1))
    done
fi
[ -n "$CURRENT" ] && say "   Aktuell eingestellt: $CURRENT"

DEFAULT="$CURRENT"
[ -z "$DEFAULT" ] && [ ${#LOCATIONS[@]} -gt 0 ] && DEFAULT="1"
while true; do
    ask "   Nummer wählen oder Pfad eingeben" "$DEFAULT"
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
    say "   Diesen Ordner gibt es nicht – bitte noch einmal."
done

DRIVE="$BASE"
if [ -n "$FROM_LIST" ]; then
    ask "   Unterordner für die Lieferungen (leer = direkt hier)" "VidTransfer"
    if [ -n "$REPLY" ]; then
        DRIVE="$BASE/$REPLY"
        mkdir -p "$DRIVE" || fail "Ordner konnte nicht angelegt werden: $DRIVE"
    fi
fi
vt config --drive "$DRIVE" || fail "Der Ordner konnte nicht eingestellt werden."
say "   → $DRIVE"

# ---------------------------------------------------------------- name, mode, inbox, login
say ""
say "2) Name dieses Macs (wird beim Empfänger angezeigt)"
ask "   Name" "$(vt_get sender)"
vt config --sender "$REPLY" || fail "Der Name konnte nicht gespeichert werden."

say ""
say "3) Wofür wird dieser Mac hauptsächlich genutzt?"
if [ "$(vt_get mode)" = "receive" ]; then MODE_DEFAULT="e"; else MODE_DEFAULT="s"; fi
ask "   s = Senden, e = Empfangen" "$MODE_DEFAULT"
case "$REPLY" in
    [eE]*) MODE="receive" ;;
    *) MODE="send" ;;
esac
vt config --mode "$MODE" || fail "Der Modus konnte nicht gespeichert werden."

if [ "$MODE" = "receive" ]; then
    say ""
    say "4) Wo sollen empfangene Lieferungen gespeichert werden?"
    ask "   Ordner" "$(vt_get inbox)"
    vt config --inbox "$(expand_home "$REPLY")" || fail "Die Ablage konnte nicht eingestellt werden."
    LOGIN_DEFAULT="j"
else
    LOGIN_DEFAULT="n"
fi

say ""
if yes_no "VidTransfer beim Anmelden automatisch starten? (empfohlen für den Mac, der empfängt)" "$LOGIN_DEFAULT"; then
    vt config --login on
else
    vt config --login off
fi

# ---------------------------------------------------------------- done
say ""
say "VidTransfer ist installiert (Programme › VidTransfer)."
if yes_no "Jetzt öffnen?" "j"; then
    run open "$DEST_APP"
fi
finish 0
