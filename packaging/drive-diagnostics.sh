#!/bin/bash
# Read-only diagnostics for a delivery folder inside Google Drive (macOS).
#
# Collects what macOS and Google Drive report about the packet files: size,
# file flags (e.g. "dataless" = online only), extended attributes and Spotlight
# metadata. It changes nothing. The result is saved on the Desktop as
# drive-diagnostics.txt, to be sent to the developer.
#
# Run it in Terminal (no installation needed):
#   bash drive-diagnostics.sh "<delivery folder in Google Drive>"
# or without an argument and drag the folder into the Terminal window when asked.
#
# Written for the bash 3.2 that ships with macOS.

OUT="$HOME/Desktop/drive-diagnostics.txt"
folder="$1"

if [ -z "$folder" ]; then
    echo "Drag the delivery folder (inside Google Drive) into this window, then press Enter:"
    IFS= read -r folder || exit 1
fi
# A folder dragged into Terminal arrives with escaped spaces, sometimes quoted
folder="$(printf '%s' "$folder" | sed -e 's/[[:space:]]*$//' -e "s/^'//" -e "s/'$//" -e 's/^"//' -e 's/"$//' -e 's/\\\(.\)/\1/g')"

if [ ! -d "$folder" ]; then
    echo "Folder not found: $folder"
    exit 1
fi

run() {
    echo "\$ $*"
    "$@" 2>&1 | head -n 200
    echo
}

{
    echo "Google Drive diagnostics"
    echo "time: $(date '+%Y-%m-%d %H:%M:%S %z')"
    echo "macOS: $(sw_vers -productVersion 2>/dev/null) ($(uname -m))"
    echo "Google Drive app: $(defaults read "/Applications/Google Drive.app/Contents/Info" CFBundleShortVersionString 2>/dev/null || echo unknown)"
    echo "Google Drive running: $(pgrep -x "Google Drive" >/dev/null 2>&1 && echo yes || echo no)"
    echo "folder: $folder"
    echo "packet files: $(find "$folder" -maxdepth 1 -name '*.vtp' | wc -l | tr -d ' ')"
    echo

    echo "=== All files: size | flags | extended attribute names ==="
    for f in "$folder"/*; do
        [ -f "$f" ] || continue
        flags="$(stat -f '%Sf' "$f" 2>/dev/null)"
        [ -z "$flags" ] && flags="-"
        attrs="$(xattr "$f" 2>/dev/null | tr '\n' ' ')"
        printf '%s | %s bytes | %s | %s\n' "$(basename "$f")" "$(stat -f '%z' "$f")" "$flags" "${attrs:--}"
    done
    echo

    echo "=== Details for the manifest, the oldest and the newest packets ==="
    samples="$folder/manifest.json"
    oldest="$(ls -tr "$folder"/*.vtp 2>/dev/null | head -n 2)"
    newest="$(ls -t "$folder"/*.vtp 2>/dev/null | head -n 3)"
    samples="$(printf '%s\n%s\n%s\n' "$samples" "$oldest" "$newest" | awk 'NF && !seen[$0]++')"
    printf '%s\n' "$samples" | while IFS= read -r f; do
        [ -f "$f" ] || continue
        echo "--- $(basename "$f") ---"
        run ls -lO@e "$f"
        run xattr -l "$f"
        run mdls "$f"
    done

    echo "=== File providers ==="
    run fileproviderctl listproviders
} > "$OUT"

echo "Done. Saved to: $OUT"
echo "Please send this file."
open -R "$OUT" 2>/dev/null
