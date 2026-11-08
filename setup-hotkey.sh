#!/usr/bin/env bash
# Keyboard shortcut that opens the Fan Control window from anywhere.
# Default: Super+Shift+F.   Usage:  ./setup-hotkey.sh ['<Super><Shift>f']
# No sudo and no session restart needed.
set -euo pipefail

BINDING="${1:-<Super><Shift>f}"
COMMAND="/usr/local/bin/fanctrl-gui"
NAME="Fan Control"
PATH_ID="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/fanctrl/"
SCHEMA="org.gnome.settings-daemon.plugins.media-keys"

if [[ $EUID -eq 0 ]]; then
    echo "Run this script without sudo (the shortcut is added to your own session)." >&2
    exit 1
fi

# Append to the existing list of shortcuts (without duplicates)
CURRENT="$(gsettings get "$SCHEMA" custom-keybindings)"
NEW="$(python3 - "$CURRENT" "$PATH_ID" <<'EOF'
import ast, sys
try:
    items = ast.literal_eval(sys.argv[1])
    if not isinstance(items, (list, tuple)):
        items = []
except (ValueError, SyntaxError):
    items = []
items = [str(i) for i in items]
if sys.argv[2] not in items:
    items.append(sys.argv[2])
print("[" + ", ".join(f"'{i}'" for i in items) + "]")
EOF
)"

gsettings set "$SCHEMA" custom-keybindings "$NEW"
gsettings set "$SCHEMA.custom-keybinding:$PATH_ID" name "$NAME"
gsettings set "$SCHEMA.custom-keybinding:$PATH_ID" command "$COMMAND"
gsettings set "$SCHEMA.custom-keybinding:$PATH_ID" binding "$BINDING"

echo "✔ Shortcut set: $BINDING → $COMMAND"
echo "  Check it in: Settings → Keyboard → Keyboard Shortcuts → Custom Shortcuts"
echo "  Remove:      gsettings set $SCHEMA custom-keybindings \"[]\""
