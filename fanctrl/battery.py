"""Battery status and charge thresholds.

Two sources are used:

  * `/sys/class/power_supply/BAT*` — status, percentage, capacity, "health" (the
    ratio of charge_full to charge_full_design). Regular users can read these too.

  * BIOS attributes (Dell: dell-wmi-sysman → PrimaryBattChargeCfg,
    CustomChargeStart, CustomChargeStop) — the charging policy. The setting is
    stored in firmware, so it stays in effect across OS reboots. Both reading
    and writing require root.

  * Other vendors (Lenovo, ASUS) expose `charge_control_end_threshold` —
    that is supported as well.
"""

from __future__ import annotations

import glob
import os
from typing import Optional

from .i18n import _

SYSMAN_ATTRS = "/sys/class/firmware-attributes/*/attributes"

# Dell BIOS attributes
ATTR_MODE = "PrimaryBattChargeCfg"
ATTR_START = "CustomChargeStart"
ATTR_STOP = "CustomChargeStop"
ATTR_ADVANCED = "AdvBatteryChargeCfg"

CUSTOM = "Custom"
# In Dell "Custom" mode, start and stop must be at least this many percent apart
CUSTOM_MIN_GAP = 5

# Human-readable descriptions of the modes (keys are the BIOS values)
MODE_DESCRIPTIONS = {
    "Adaptive": _("Adaptive — the BIOS picks the policy based on your usage habits"),
    "Standard": _("Standard — always charges to 100%"),
    "Express": _("Express — fastest charging, the battery wears out faster"),
    "PrimAcUse": _("Always plugged in — charge is held around 50%, least wear"),
    "Custom": _("Custom limit — the start/stop percentages below are used"),
}

MODE_SHORT = {
    "Adaptive": _("Adaptive"),
    "Standard": _("Standard"),
    "Express": _("Express"),
    "PrimAcUse": _("Always plugged in"),
    "Custom": _("Custom limit"),
}


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _read_int(path: str) -> Optional[int]:
    raw = _read(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _write(path: str, value: str) -> None:
    with open(path, "w") as fh:
        fh.write(value)


class ChargeConfig:
    """BIOS charging policy (if available)."""

    def __init__(self) -> None:
        self.kind: Optional[str] = None      # "sysman" | "threshold"
        self.dir: Optional[str] = None       # sysman attributes directory
        self.values: list[str] = []
        self.threshold_path: Optional[str] = None
        self.limits: dict[str, int] = {}
        self._discover()

    def _discover(self) -> None:
        for attr_dir in sorted(glob.glob(SYSMAN_ATTRS)):
            mode_dir = os.path.join(attr_dir, ATTR_MODE)
            raw = _read(os.path.join(mode_dir, "possible_values"))
            if not raw:
                continue
            values = [v for v in raw.replace(",", ";").split(";") if v]
            if not values:
                continue
            self.kind = "sysman"
            self.dir = attr_dir
            self.values = values
            for key, attr in (("start", ATTR_START), ("stop", ATTR_STOP)):
                base = os.path.join(attr_dir, attr)
                low = _read_int(os.path.join(base, "min_value"))
                high = _read_int(os.path.join(base, "max_value"))
                if low is not None:
                    self.limits[f"{key}_min"] = low
                if high is not None:
                    self.limits[f"{key}_max"] = high
            return

        # Simple Lenovo/ASUS-style threshold
        for path in sorted(glob.glob("/sys/class/power_supply/BAT*/charge_control_end_threshold")):
            self.kind = "threshold"
            self.threshold_path = path
            self.limits = {"stop_min": 20, "stop_max": 100}
            return

    @property
    def available(self) -> bool:
        return self.kind is not None

    # -------------------------------------------------------------------- reading
    def _attr(self, name: str) -> str:
        return os.path.join(self.dir or "", name, "current_value")

    def current(self) -> Optional[str]:
        if self.kind == "sysman":
            return _read(self._attr(ATTR_MODE))
        if self.kind == "threshold":
            stop = _read_int(self.threshold_path or "")
            return CUSTOM if stop is not None and stop < 100 else "Standard"
        return None

    def custom_range(self) -> dict:
        if self.kind == "sysman":
            return {
                "start": _read_int(self._attr(ATTR_START)),
                "stop": _read_int(self._attr(ATTR_STOP)),
            }
        if self.kind == "threshold":
            return {"start": None, "stop": _read_int(self.threshold_path or "")}
        return {"start": None, "stop": None}

    def advanced_enabled(self) -> Optional[bool]:
        """Whether Dell "Advanced Battery Charge" (scheduled charging) is enabled."""
        if self.kind != "sysman":
            return None
        value = _read(self._attr(ATTR_ADVANCED))
        return None if value is None else value.lower() == "enabled"

    # -------------------------------------------------------------------- writing
    def set_mode(self, value: str) -> str:
        if self.kind != "sysman":
            raise OSError(_("the charging mode cannot be changed on this system"))
        if value not in self.values:
            raise ValueError(
                _("invalid mode: {value} (allowed: {allowed})", value=value, allowed=", ".join(self.values))
            )
        if self.advanced_enabled():
            # With Advanced mode enabled the BIOS ignores the regular policy
            raise OSError(
                _("\"Advanced Battery Charge\" is enabled in the BIOS — disable it first")
            )
        _write(self._attr(ATTR_MODE), value)
        return self.current() or value

    def set_custom(self, start: Optional[int], stop: int) -> dict:
        """Custom limit: charging starts when the level drops below `start` percent
        and stops at `stop` percent."""
        if self.kind == "threshold":
            low = self.limits.get("stop_min", 20)
            high = self.limits.get("stop_max", 100)
            if not low <= stop <= high:
                raise ValueError(_("the stop threshold must be within {low}–{high}", low=low, high=high))
            _write(self.threshold_path or "", str(stop))
            return self.custom_range()

        if self.kind != "sysman":
            raise OSError(_("a charge limit cannot be set on this system"))

        current = self.custom_range()
        if start is None:
            start = current.get("start") or self.limits.get("start_min", 50)

        start_min = self.limits.get("start_min", 50)
        start_max = self.limits.get("start_max", 95)
        stop_min = self.limits.get("stop_min", 55)
        stop_max = self.limits.get("stop_max", 100)
        if not start_min <= start <= start_max:
            raise ValueError(
                _("the start threshold must be within {low}–{high}", low=start_min, high=start_max)
            )
        if not stop_min <= stop <= stop_max:
            raise ValueError(
                _("the stop threshold must be within {low}–{high}", low=stop_min, high=stop_max)
            )
        if stop - start < CUSTOM_MIN_GAP:
            raise ValueError(
                _("the stop threshold must be at least {gap}% above the start threshold", gap=CUSTOM_MIN_GAP)
            )

        # Switch the mode to "Custom" first — otherwise the BIOS may reject the
        # numbers. Then write the thresholds.
        if self.current() != CUSTOM and CUSTOM in self.values:
            self.set_mode(CUSTOM)
        # Order matters: write the side that widens the range first, so the new
        # values never conflict with the old ones.
        if start <= (current.get("stop") or stop_max):
            _write(self._attr(ATTR_START), str(start))
            _write(self._attr(ATTR_STOP), str(stop))
        else:
            _write(self._attr(ATTR_STOP), str(stop))
            _write(self._attr(ATTR_START), str(start))
        return self.custom_range()

    def as_dict(self) -> dict:
        if not self.available:
            return {"available": False}
        return {
            "available": True,
            "kind": self.kind,
            "current": self.current(),
            "values": list(self.values) or [CUSTOM],
            "descriptions": {v: MODE_DESCRIPTIONS.get(v, v) for v in self.values},
            "short_names": {v: MODE_SHORT.get(v, v) for v in self.values},
            "limits": dict(self.limits),
            "custom": self.custom_range(),
            "advanced": self.advanced_enabled(),
        }


class Battery:
    """Status of a single battery plus the AC adapter."""

    def __init__(self) -> None:
        self.path: Optional[str] = None
        self.ac_paths: list[str] = []
        self.config = ChargeConfig()
        self._discover()

    def _discover(self) -> None:
        for path in sorted(glob.glob("/sys/class/power_supply/*")):
            kind = _read(os.path.join(path, "type"))
            if kind == "Battery" and self.path is None:
                # ucsi/HID devices can also be "Battery" — skip those with scope=Device
                scope = _read(os.path.join(path, "scope"))
                if scope and scope != "System":
                    continue
                self.path = path
            elif kind == "Mains":
                self.ac_paths.append(path)

    @property
    def available(self) -> bool:
        return self.path is not None

    def _energy_wh(self, base: str) -> Optional[float]:
        """Compute Wh from `charge_*` (µAh) or `energy_*` (µWh)."""
        energy = _read_int(os.path.join(self.path or "", f"energy_{base}"))
        if energy is not None:
            return energy / 1_000_000
        charge = _read_int(os.path.join(self.path or "", f"charge_{base}"))
        if charge is None:
            return None
        volts = _read_int(os.path.join(self.path or "", "voltage_min_design")) \
            or _read_int(os.path.join(self.path or "", "voltage_now"))
        if not volts:
            return None
        return charge / 1_000_000 * volts / 1_000_000

    def ac_online(self) -> Optional[bool]:
        for path in self.ac_paths:
            value = _read_int(os.path.join(path, "online"))
            if value is not None:
                return bool(value)
        return None

    def read(self, config: Optional[dict] = None) -> dict:
        """Current status. If `config` is given, the BIOS attributes are not re-read —
        they are WMI calls, so it is better not to query them every second."""
        cfg = self.config.as_dict() if config is None else config
        if not self.available:
            return {"available": False, "config": cfg}

        full = self._energy_wh("full")
        design = self._energy_wh("full_design")
        health = (full / design * 100) if (full and design) else None

        volts = _read_int(os.path.join(self.path, "voltage_now"))
        amps = _read_int(os.path.join(self.path, "current_now"))
        watts = _read_int(os.path.join(self.path, "power_now"))
        if watts is None and volts and amps is not None:
            watts = volts * amps // 1_000_000
        cycles = _read_int(os.path.join(self.path, "cycle_count"))

        return {
            "available": True,
            "name": os.path.basename(self.path),
            "status": _read(os.path.join(self.path, "status")),
            "percent": _read_int(os.path.join(self.path, "capacity")),
            "health_percent": round(health, 1) if health is not None else None,
            "full_wh": round(full, 1) if full else None,
            "design_wh": round(design, 1) if design else None,
            # 0 means the firmware does not report this value
            "cycle_count": cycles if cycles else None,
            "power_w": round(watts / 1_000_000, 2) if watts else 0.0,
            "ac_online": self.ac_online(),
            "manufacturer": _read(os.path.join(self.path, "manufacturer")),
            "model": _read(os.path.join(self.path, "model_name")),
            "config": cfg,
        }
