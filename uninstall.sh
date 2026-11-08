#!/usr/bin/env bash
# Remove fanctrl.  Usage:  sudo ./uninstall.sh [--purge]
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root:  sudo $0" >&2
    exit 1
fi

echo ">> Stopping the service (the fans return to automatic control)"
systemctl disable --now fanctrld 2>/dev/null || true
rm -f /etc/systemd/system/fanctrld.service
systemctl daemon-reload

rm -f /usr/local/bin/fanctrl /usr/local/bin/fanctrl-gui
rm -f /usr/share/applications/fanctrl.desktop
rm -rf /opt/fanctrl
rm -f /run/fanctrld.sock

rm -f /etc/modules-load.d/acpi_call.conf

if [[ "${1:-}" == "--purge" ]]; then
    rm -f /etc/fanctrld.conf
    rm -rf /var/lib/fanctrld
    echo ">> Config and state files removed as well"
else
    echo ">> /etc/fanctrld.conf kept (use --purge to remove it)"
fi

echo "✔ removed"
