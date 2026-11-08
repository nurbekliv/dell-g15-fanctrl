#!/usr/bin/env bash
# fanctrl installer. Usage:  sudo ./install.sh
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX=/opt/fanctrl
BIN=/usr/local/bin
CONFIG=/etc/fanctrld.conf
UNIT=/etc/systemd/system/fanctrld.service
DESKTOP=/usr/share/applications/fanctrl.desktop

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root:  sudo $0" >&2
    exit 1
fi

# Who we are installing for: sudo, pkexec or plain root
if [[ -n "${FANCTRL_USER:-}" ]]; then
    TARGET_USER="$FANCTRL_USER"
elif [[ -n "${SUDO_USER:-}" ]]; then
    TARGET_USER="$SUDO_USER"
elif [[ -n "${PKEXEC_UID:-}" ]]; then
    TARGET_USER="$(id -nu "$PKEXEC_UID")"
else
    TARGET_USER=root
fi
TARGET_GROUP="$(id -gn "$TARGET_USER")"

echo ">> Checks"
command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || { echo "Python 3.9+ is required" >&2; exit 1; }
if ! python3 -c 'import PyQt5.QtWidgets' 2>/dev/null; then
    echo "   ! PyQt5 not found — the GUI will not work (the CLI will)."
    echo "     Install:  sudo apt install python3-pyqt5"
fi

echo ">> acpi_call (for Dell/Alienware AWCC boost)"
if modprobe acpi_call 2>/dev/null && [[ -e /proc/acpi/call ]]; then
    echo "   module loaded"
    echo "acpi_call" > /etc/modules-load.d/acpi_call.conf   # load automatically on every boot
else
    echo "   ! acpi_call not found — AWCC boost (manual fan speed) will not work."
    echo "     Install:  sudo apt install acpi-call-dkms   (then: sudo ./install.sh)"
fi

echo ">> Files: $PREFIX"
install -d -m 0755 "$PREFIX"
rm -rf "$PREFIX/fanctrl"
cp -r "$SRC/fanctrl" "$PREFIX/fanctrl"
[[ -f "$SRC/README.md" ]] && install -m 0644 "$SRC/README.md" "$PREFIX/README.md"
find "$PREFIX" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
chmod -R a+rX "$PREFIX"

echo ">> Commands: $BIN/fanctrl, $BIN/fanctrl-gui"
cat > "$BIN/fanctrl" <<EOF
#!/bin/sh
exec env PYTHONPATH=$PREFIX /usr/bin/python3 -m fanctrl.cli "\$@"
EOF
cat > "$BIN/fanctrl-gui" <<EOF
#!/bin/sh
exec env PYTHONPATH=$PREFIX /usr/bin/python3 -m fanctrl.gui "\$@"
EOF
chmod 0755 "$BIN/fanctrl" "$BIN/fanctrl-gui"

if [[ -f "$CONFIG" ]]; then
    echo ">> Config already exists, left untouched: $CONFIG"
else
    echo ">> Config: $CONFIG (socket group: $TARGET_GROUP)"
    cat > "$CONFIG" <<EOF
# fanctrld settings. After editing:  sudo systemctl restart fanctrld

[daemon]
socket_path = /run/fanctrld.sock
# Group allowed to connect to the socket (members of this group can control the fans)
socket_group = $TARGET_GROUP
# Sensor polling interval, seconds
poll_interval = 2.0
# Order in which fans are shown in the UI (e.g. "2,1" swaps them).
# Only affects the display; fan numbers do not change.
fan_order =

[safety]
# Above this temperature (°C) the manual setting is suspended and the fans go to 100%
warn_temp = 85
# The manual setting resumes once the temperature drops by this much
warn_hysteresis = 6
# Critical limit: manual mode is cancelled entirely and control returns to the BIOS
critical_temp = 93
# How many seconds manual mode stays blocked after a critical event
lockout_seconds = 120
# Lowest pwm allowed in manual mode (0 = allow stopping the fans completely)
min_manual_pwm = 0

[state]
# Restore the last manual setting when the daemon restarts
restore_on_start = false
EOF
    chmod 0644 "$CONFIG"
fi

echo ">> systemd service"
install -m 0644 "$SRC/systemd/fanctrld.service" "$UNIT"
systemctl daemon-reload
systemctl enable fanctrld
# Always restart so the new code takes effect
systemctl restart fanctrld
sleep 2

if [[ -f "$SRC/share/fanctrl.desktop" ]]; then
    install -m 0644 "$SRC/share/fanctrl.desktop" "$DESKTOP"
    update-desktop-database /usr/share/applications 2>/dev/null || true
fi

echo
if systemctl is-active --quiet fanctrld; then
    echo "✔ fanctrld is running."
    echo
    echo "  fanctrl status          — show the current status"
    echo "  fanctrl watch           — live monitoring"
    echo "  fanctrl set all 70%     — set both fans to 70%"
    echo "  fanctrl auto            — hand control back to the BIOS"
    echo "  fanctrl-gui             — graphical window (also in the app menu: \"Fan Control\")"
else
    echo "✖ fanctrld failed to start. Log:" >&2
    journalctl -u fanctrld -n 20 --no-pager >&2 || true
    exit 1
fi
