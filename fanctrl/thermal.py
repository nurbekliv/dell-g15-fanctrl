"""BIOS thermal modes (Dell: dell-wmi-sysman → ThermalManagement).

On some laptops (the Dell G15 included) the firmware ignores pwm writes.
On such systems the reliable way to influence the fans is to switch the
BIOS thermal mode. The setting persists across reboots.
"""

from __future__ import annotations

import glob
import os
from typing import Optional

from .i18n import _

# Possible sources for different vendors (the first one found is used)
SYSMAN_ATTRS = "/sys/class/firmware-attributes/*/attributes"
PLATFORM_PROFILE = "/sys/firmware/acpi/platform_profile"
PLATFORM_PROFILE_CHOICES = "/sys/firmware/acpi/platform_profile_choices"

# Human-readable descriptions of the modes (keys are the firmware values)
DESCRIPTIONS = {
    "Optimized": _("Balanced (factory default)"),
    "Cool": _("Cool — fans spin up earlier and faster"),
    "Quiet": _("Quiet — slower fans, higher temperatures"),
    "UltraPerformance": _("Maximum cooling — fans at full speed"),
    "quiet": _("Quiet"),
    "balanced": _("Balanced"),
    "balanced-performance": _("Balanced+"),
    "performance": _("Maximum performance"),
    "low-power": _("Power saving"),
    "cool": _("Cool"),
}


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


class ThermalProfiles:
    """BIOS thermal mode: read and switch it (writing requires root)."""

    def __init__(self) -> None:
        self.kind: Optional[str] = None      # "sysman" | "platform_profile"
        self.path: Optional[str] = None      # the file that is written
        self.values: list[str] = []
        self.name: str = ""
        self._discover()

    def _discover(self) -> None:
        # 1) Dell / Lenovo BIOS attributes
        for attr_dir in sorted(glob.glob(SYSMAN_ATTRS)):
            for candidate in ("ThermalManagement", "ThermalMode", "FanSpeed"):
                path = os.path.join(attr_dir, candidate)
                if not os.path.isdir(path):
                    continue
                raw = _read(os.path.join(path, "possible_values"))
                if not raw:
                    continue
                values = [v for v in raw.replace(",", ";").split(";") if v]
                if not values:
                    continue
                self.kind = "sysman"
                self.path = os.path.join(path, "current_value")
                self.values = values
                self.name = _read(os.path.join(path, "display_name")) or candidate
                return

        # 2) Standard ACPI platform profile
        if os.path.exists(PLATFORM_PROFILE):
            raw = _read(PLATFORM_PROFILE_CHOICES) or ""
            values = raw.split()
            if values:
                self.kind = "platform_profile"
                self.path = PLATFORM_PROFILE
                self.values = values
                self.name = "Platform profile"

    @property
    def available(self) -> bool:
        return self.path is not None

    def current(self) -> Optional[str]:
        """The current mode. On Dell only root can read this file."""
        if not self.path:
            return None
        return _read(self.path)

    def set(self, value: str) -> str:
        if not self.path:
            raise OSError(_("thermal modes are not available on this system"))
        if value not in self.values:
            raise ValueError(
                _("invalid mode: {value} (allowed: {allowed})", value=value, allowed=", ".join(self.values))
            )
        with open(self.path, "w") as fh:
            fh.write(value)
        return self.current() or value

    def describe(self, value: str) -> str:
        return DESCRIPTIONS.get(value, value)

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "kind": self.kind,
            "name": self.name,
            "values": list(self.values),
            "descriptions": {v: self.describe(v) for v in self.values},
        }
