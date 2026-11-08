"""AWCC (Alienware/Dell G-series) fan boost via acpi_call.

On laptops such as the Dell G15 the firmware blocks hwmon pwm writes, but the
fans can still be given a "boost" through the ACPI method `\\_SB.AMWW.WMAX`.

Key property: the boost is added ON TOP of the BIOS's own fan curve — it can
only speed the fans up, never slow them down. That makes this path safe with
respect to overheating: 0% = the BIOS is in full control.

Requires: the `acpi-call-dkms` package (module: acpi_call, device: /proc/acpi/call).
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Optional

from .i18n import _

log = logging.getLogger("fanctrld")

CALL_PATH = "/proc/acpi/call"
METHOD = "\\_SB.AMWW.WMAX"

# Command groups
GROUP_GET = 0x14
GROUP_SET = 0x15
CMD_GET_FAN_RPM = 0x05
CMD_GET_TEMP = 0x04
CMD_GET_MODE = 0x0B
CMD_SET_BOOST = 0x02

# Fan IDs on the G15 (the first is the CPU side, the second the GPU side)
KNOWN_FAN_IDS = (0x32, 0x33)

MAX_PLAUSIBLE_RPM = 20000
ERROR_VALUE = 0xFFFFFFFF


class AwccError(OSError):
    pass


class Awcc:
    """AWCC interface: read fan speeds and apply boost."""

    def __init__(self) -> None:
        self.available = False
        self.reason = ""
        self.fan_ids: list[int] = []
        self._probe()

    # ----------------------------------------------------------------- low level
    def _raw_call(self, expression: str) -> str:
        try:
            with open(CALL_PATH, "w") as fh:
                fh.write(expression)
            with open(CALL_PATH, "r") as fh:
                result = fh.read()
        except OSError as exc:
            raise AwccError(_("could not access /proc/acpi/call: {error}", error=exc)) from exc
        return result.replace("\0", "").strip()

    def _call(self, group: int, command: int, arg1: int = 0, arg2: int = 0) -> int:
        expression = (
            f"{METHOD} 0 {group:#x} "
            f"{{{command:#x}, {arg1:#x}, {arg2:#x}, 0x00}}"
        )
        result = self._raw_call(expression)
        if not result:
            raise AwccError(_("ACPI returned no response"))
        if result.lower().startswith("error"):
            raise AwccError(_("ACPI error: {result}", result=result))
        try:
            return int(result, 16) if result.startswith("0x") else int(result)
        except ValueError as exc:
            raise AwccError(_("could not parse the ACPI response: {result}", result=result)) from exc

    # -------------------------------------------------------------------- discovery
    def _probe(self) -> None:
        if not os.path.exists(CALL_PATH):
            # If the module is not loaded, try to load it (the daemon runs as root)
            if os.geteuid() == 0:
                try:
                    subprocess.run(
                        ["modprobe", "acpi_call"],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
        if not os.path.exists(CALL_PATH):
            self.reason = _(
                "acpi_call module not available — install it with: sudo apt install acpi-call-dkms"
            )
            return

        found = []
        for fan_id in KNOWN_FAN_IDS:
            try:
                rpm = self._call(GROUP_GET, CMD_GET_FAN_RPM, fan_id)
            except AwccError as exc:
                self.reason = str(exc)
                return
            if rpm == ERROR_VALUE or rpm > MAX_PLAUSIBLE_RPM:
                continue
            found.append(fan_id)

        if not found:
            self.reason = _("no AWCC fans found (this model is not supported)")
            return

        self.fan_ids = found
        self.available = True

    # ----------------------------------------------------------------------- API
    def rpm(self, fan_id: int) -> Optional[int]:
        try:
            value = self._call(GROUP_GET, CMD_GET_FAN_RPM, fan_id)
        except AwccError:
            return None
        return None if value == ERROR_VALUE or value > MAX_PLAUSIBLE_RPM else value

    def temperature(self, sensor_id: int) -> Optional[int]:
        try:
            value = self._call(GROUP_GET, CMD_GET_TEMP, sensor_id)
        except AwccError:
            return None
        return None if value == ERROR_VALUE or value > 150 else value

    def thermal_mode(self) -> Optional[int]:
        try:
            return self._call(GROUP_GET, CMD_GET_MODE)
        except AwccError:
            return None

    def set_boost(self, fan_id: int, percent: int) -> int:
        """Apply boost to a fan (0–100). 0 = the BIOS is in control."""
        percent = max(0, min(100, int(percent)))
        self._call(GROUP_SET, CMD_SET_BOOST, fan_id, percent)
        return percent

    def map_to_hwmon(self, hwmon_fans: list[dict]) -> dict[int, int]:
        """Map hwmon fan numbers to AWCC fan IDs.

        The mapping is verified by comparing RPM readings; fans that cannot be
        matched are mapped in plain order.
        """
        mapping: dict[int, int] = {}
        used: set[int] = set()

        for fan in hwmon_fans:
            target = fan.get("rpm")
            if target is None:
                continue
            for fan_id in self.fan_ids:
                if fan_id in used:
                    continue
                value = self.rpm(fan_id)
                # SMM and AWCC read the same counter — the difference should be tiny
                if value is not None and abs(value - target) <= max(60, target * 0.05):
                    mapping[fan["index"]] = fan_id
                    used.add(fan_id)
                    break

        # Map whatever is left over in order
        spare = [fid for fid in self.fan_ids if fid not in used]
        for fan in hwmon_fans:
            if fan["index"] in mapping or not spare:
                continue
            mapping[fan["index"]] = spare.pop(0)

        return mapping
