#!/usr/bin/env bash
# Installs the GNOME panel extension. sudo is NOT needed — it goes into your home directory.
#   ./install-extension.sh
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UUID="fanctrl@nurbekliv.github.io"
DEST="$HOME/.local/share/gnome-shell/extensions/$UUID"

if [[ $EUID -eq 0 ]]; then
    echo "Run this script as your normal user, without sudo." >&2
    exit 1
fi

if ! command -v gnome-shell >/dev/null; then
    echo "GNOME Shell not found — this extension is for GNOME only." >&2
    exit 1
fi

SHELL_VER="$(gnome-shell --version | awk '{print $3}')"
echo ">> GNOME Shell $SHELL_VER"

echo ">> Copying to $DEST"
mkdir -p "$DEST"
cp -f "$SRC/gnome-extension/$UUID/metadata.json" "$DEST/"
cp -f "$SRC/gnome-extension/$UUID/extension.js" "$DEST/"
cp -f "$SRC/gnome-extension/$UUID/stylesheet.css" "$DEST/"
# Replace the translations as a whole so a removed language does not linger.
rm -rf "$DEST/locales"
if [[ -d "$SRC/gnome-extension/$UUID/locales" ]]; then
    cp -r "$SRC/gnome-extension/$UUID/locales" "$DEST/"
fi

echo ">> Syntax check"
if command -v gjs >/dev/null; then
    gjs -c '
        const GLib = imports.gi.GLib;
        const ByteArray = imports.byteArray;
        const [, bytes] = GLib.file_get_contents(ARGV[0]);
        try { new Function(ByteArray.toString(bytes)); }
        catch (e) { print("SYNTAX ERROR: " + e.message); imports.system.exit(1); }
    ' "$DEST/extension.js" || { echo "extension.js is broken — installation aborted" >&2; exit 1; }
fi

if gnome-extensions enable "$UUID" 2>/dev/null; then
    echo ">> Extension enabled"
else
    # GNOME Shell has not seen this extension yet — add it to the list by hand
    # so it loads automatically after the next login.
    CURRENT="$(gsettings get org.gnome.shell enabled-extensions)"
    NEW="$(python3 - "$CURRENT" "$UUID" <<'EOF'
import ast, sys
try:
    items = list(ast.literal_eval(sys.argv[1]))
except (ValueError, SyntaxError):
    items = []
items = [str(i) for i in items]
if sys.argv[2] not in items:
    items.append(sys.argv[2])
print("[" + ", ".join(f"'{i}'" for i in items) + "]")
EOF
)"
    gsettings set org.gnome.shell enabled-extensions "$NEW"
    echo ">> Extension added to the enabled list (it loads after the next login)"
fi

SESSION="${XDG_SESSION_TYPE:-x11}"
echo
echo "✔ Installed: $DEST"
echo
if [[ "$SESSION" == "wayland" ]]; then
    cat <<EOF
You are in a Wayland session — GNOME Shell cannot be restarted in place.
Log out and log back in for the extension to appear.

If it is still missing from the panel after logging back in:
    gnome-extensions enable $UUID
    gnome-extensions info $UUID     # show its state
To see errors:
    journalctl -f -o cat /usr/bin/gnome-shell
EOF
else
    echo "X11 session: restart GNOME Shell with Alt+F2 → 'r' → Enter."
fi
