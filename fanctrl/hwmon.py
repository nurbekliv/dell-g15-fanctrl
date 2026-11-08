"""hwmon sysfs layer — reading/writing fans and temperatures.

This module talks to sysfs only and has no external dependencies.
Writing the pwm* files requires root (the daemon uses this module).
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .i18n import _

HWMON_ROOT = "/sys/class/hwmon"
DMI_ROOT = "/sys/class/dmi/id"

# Drivers that support pwm control (in order of preference)
PREFERRED_DRIVERS = ("dell_smm", "nbfc", "thinkpad", "asus", "acpi_fan")

# Sensor groups taken into account by the safety governor
GOVERNED_GROUPS = ("cpu", "gpu", "smm")


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _read_int(path: str) -> Optional[int]:
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _write_int(path: str, value: int) -> None:
    with open(path, "w") as fh:
        fh.write(str(int(value)))


def machine_model() -> str:
    vendor = _read_text(f"{DMI_ROOT}/sys_vendor") or ""
    product = _read_text(f"{DMI_ROOT}/product_name") or _("Unknown")
    return f"{vendor} {product}".strip()


@dataclass
class Fan:
    """A single fan: its pwm output and (if present) its tachometer input."""

    index: int
    pwm_path: str
    enable_path: Optional[str] = None
    input_path: Optional[str] = None
    label: str = ""
    max_rpm: Optional[int] = None
    min_rpm: Optional[int] = None
    # Calibration result: the pwm values the driver actually accepts
    steps: list[int] = field(default_factory=list)

    def rpm(self) -> Optional[int]:
        if not self.input_path:
            return None
        return _read_int(self.input_path)

    def pwm(self) -> Optional[int]:
        return _read_int(self.pwm_path)

    def enable_mode(self) -> Optional[int]:
        if not self.enable_path:
            return None
        return _read_int(self.enable_path)


@dataclass
class TempSensor:
    """A temperature sensor. `reader` is a function that returns °C."""

    key: str
    label: str
    group: str  # cpu | gpu | smm | nvme | other
    reader: Callable[[], Optional[float]]
    crit: Optional[float] = None

    @property
    def governed(self) -> bool:
        return self.group in GOVERNED_GROUPS

    def read(self) -> Optional[float]:
        return self.reader()


def _sysfs_temp_reader(path: str) -> Callable[[], Optional[float]]:
    def read() -> Optional[float]:
        raw = _read_int(path)
        return None if raw is None else raw / 1000.0

    return read


class NvidiaGpu:
    """GPU temperature via nvidia-smi. The call is expensive, so it is cached."""

    def __init__(self, cache_seconds: float = 2.0) -> None:
        self.cache_seconds = cache_seconds
        self._value: Optional[float] = None
        self._stamp = 0.0
        self.name: Optional[str] = None
        self.available = False
        self._probe()

    def _probe(self) -> None:
        if not any(
            os.access(os.path.join(p, "nvidia-smi"), os.X_OK)
            for p in os.environ.get("PATH", "/usr/bin:/bin").split(":")
            if p
        ):
            return
        out = self._query("name")
        if out:
            self.name = out
            self.available = True

    def _query(self, field_name: str) -> Optional[str]:
        try:
            proc = subprocess.run(
                ["nvidia-smi", f"--query-gpu={field_name}", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=4,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        line = proc.stdout.strip().splitlines()
        return line[0].strip() if line else None

    def temperature(self) -> Optional[float]:
        now = time.monotonic()
        if not self.available:
            return None
        if self._value is not None and now - self._stamp < self.cache_seconds:
            return self._value
        raw = self._query("temperature.gpu")
        self._stamp = now
        try:
            self._value = float(raw) if raw is not None else None
        except ValueError:
            self._value = None
        return self._value


class FanController:
    """Discovers hwmon devices and manages the fans and sensors."""

    def __init__(self) -> None:
        self.model = machine_model()
        self.driver: Optional[str] = None
        self.device_path: Optional[str] = None
        self.fans: list[Fan] = []
        self.temps: list[TempSensor] = []
        self.gpu = NvidiaGpu()
        self._discover()

    # ---------------------------------------------------------------- discovery
    def _hwmon_devices(self) -> list[tuple[str, str]]:
        devices = []
        for path in sorted(glob.glob(f"{HWMON_ROOT}/hwmon*")):
            name = _read_text(os.path.join(path, "name")) or os.path.basename(path)
            devices.append((name, path))
        return devices

    def _discover(self) -> None:
        devices = self._hwmon_devices()

        # 1) pick the device that has pwm* files
        candidates = [(n, p) for n, p in devices if glob.glob(os.path.join(p, "pwm[0-9]*"))]
        # count only "pwmN" itself, not companion files such as pwm1_enable
        candidates = [
            (n, p)
            for n, p in candidates
            if any(re.fullmatch(r"pwm\d+", os.path.basename(f)) for f in glob.glob(os.path.join(p, "pwm*")))
        ]
        if candidates:
            candidates.sort(key=lambda item: _driver_rank(item[0]))
            self.driver, self.device_path = candidates[0]
            self._discover_fans(self.device_path)

        # 2) all temperature sensors
        for name, path in devices:
            self._discover_temps(name, path)

        self._dedupe_labels()

        if self.gpu.available:
            self.temps.append(
                TempSensor(
                    key="gpu",
                    label=_short_gpu_name(self.gpu.name),
                    group="gpu",
                    reader=self.gpu.temperature,
                    crit=90.0,
                )
            )

    def _dedupe_labels(self) -> None:
        """Append a number to sensors that share a name so they can be told apart."""
        counts: dict[str, int] = {}
        for sensor in self.temps:
            counts[sensor.label] = counts.get(sensor.label, 0) + 1
        seen: dict[str, int] = {}
        for sensor in self.temps:
            if counts[sensor.label] > 1:
                seen[sensor.label] = seen.get(sensor.label, 0) + 1
                sensor.label = f"{sensor.label} #{seen[sensor.label]}"

    def _discover_fans(self, device_path: str) -> None:
        for pwm_path in sorted(glob.glob(os.path.join(device_path, "pwm*"))):
            base = os.path.basename(pwm_path)
            match = re.fullmatch(r"pwm(\d+)", base)
            if not match:
                continue
            idx = int(match.group(1))
            enable = os.path.join(device_path, f"pwm{idx}_enable")
            fan_input = os.path.join(device_path, f"fan{idx}_input")
            label = _read_text(os.path.join(device_path, f"fan{idx}_label")) or _("Fan {n}", n=idx)
            self.fans.append(
                Fan(
                    index=idx,
                    pwm_path=pwm_path,
                    enable_path=enable if os.path.exists(enable) else None,
                    input_path=fan_input if os.path.exists(fan_input) else None,
                    label=label,
                    max_rpm=_read_int(os.path.join(device_path, f"fan{idx}_max")),
                    min_rpm=_read_int(os.path.join(device_path, f"fan{idx}_min")),
                )
            )

    def _discover_temps(self, dev_name: str, path: str) -> None:
        node = _device_node(path)
        if node:
            dev_name_display = node  # e.g. nvme0, nvme1
        else:
            dev_name_display = dev_name
        for temp_path in sorted(glob.glob(os.path.join(path, "temp*_input"))):
            base = os.path.basename(temp_path)
            num = re.fullmatch(r"temp(\d+)_input", base)
            if not num:
                continue
            n = num.group(1)
            label = _read_text(os.path.join(path, f"temp{n}_label"))
            crit_raw = _read_int(os.path.join(path, f"temp{n}_crit"))
            if crit_raw is None:
                crit_raw = _read_int(os.path.join(path, f"temp{n}_max"))
            group, pretty = _classify_temp(dev_name, label, n, dev_name_display)
            if group is None:
                continue
            crit = None if crit_raw is None else crit_raw / 1000.0
            # Some drivers report a meaningless limit (e.g. 65261°C) — drop those
            if crit is not None and not (30.0 <= crit <= 130.0):
                crit = None
            self.temps.append(
                TempSensor(
                    key=f"{os.path.basename(path)}:{n}",
                    label=pretty,
                    group=group,
                    reader=_sysfs_temp_reader(temp_path),
                    crit=crit,
                )
            )

    # -------------------------------------------------------------------- reading
    @property
    def controllable(self) -> bool:
        return bool(self.fans)

    def read_fans(self) -> list[dict]:
        result = []
        for fan in self.fans:
            pwm = fan.pwm()
            result.append(
                {
                    "index": fan.index,
                    "label": fan.label,
                    "rpm": fan.rpm(),
                    "pwm": pwm,
                    "percent": None if pwm is None else round(pwm * 100 / 255),
                    "max_rpm": fan.max_rpm,
                    "min_rpm": fan.min_rpm,
                    "enable_mode": fan.enable_mode(),
                    "steps": fan.steps,
                }
            )
        return result

    def read_temps(self) -> list[dict]:
        result = []
        for sensor in self.temps:
            result.append(
                {
                    "key": sensor.key,
                    "label": sensor.label,
                    "group": sensor.group,
                    "value": sensor.read(),
                    "crit": sensor.crit,
                    "governed": sensor.governed,
                }
            )
        return result

    def max_governed_temp(self, temps: list[dict]) -> tuple[Optional[float], Optional[str]]:
        """The highest temperature relevant to the safety governor, and its label."""
        best_value: Optional[float] = None
        best_label: Optional[str] = None
        for item in temps:
            if not item["governed"] or item["value"] is None:
                continue
            if best_value is None or item["value"] > best_value:
                best_value, best_label = item["value"], item["label"]
        return best_value, best_label

    # -------------------------------------------------------------------- writing
    def fan_by_index(self, index: int) -> Optional[Fan]:
        for fan in self.fans:
            if fan.index == index:
                return fan
        return None

    def set_manual(self, fan: Fan) -> None:
        """Switch to manual mode if pwmN_enable exists (1 = manual)."""
        if fan.enable_path and fan.enable_mode() != 1:
            _write_int(fan.enable_path, 1)

    def set_auto(self, fan: Fan) -> None:
        """Hand control back to the BIOS/driver (2 = automatic)."""
        if not fan.enable_path:
            return
        for mode in (2, 0):  # some drivers use 0 for automatic mode
            try:
                _write_int(fan.enable_path, mode)
                return
            except OSError:
                continue

    def set_pwm(self, fan: Fan, value: int) -> int:
        """Write a pwm value and return the value the driver actually accepted."""
        value = max(0, min(255, int(value)))
        self.set_manual(fan)
        _write_int(fan.pwm_path, value)
        actual = fan.pwm()
        return value if actual is None else actual

    def probe_pwm_support(self, settle: float = 0.35) -> tuple[bool, str]:
        """Check whether pwm writes actually take effect.

        On some laptops (such as the Dell G15) sysfs accepts the write without
        an error, but the firmware ignores it and the value does not change.
        To detect this up front, a different value is written briefly and the
        original value is restored right away.
        """
        if not self.fans:
            return False, _("no pwm output found")

        fan = self.fans[0]
        original = fan.pwm()
        if original is None:
            return False, _("could not read the pwm value")

        # A test value that differs from the current one
        probe_value = 255 if original < 128 else 0
        try:
            self.set_manual(fan)
            _write_int(fan.pwm_path, probe_value)
        except OSError as exc:
            return False, _("could not write pwm: {error}", error=exc)

        time.sleep(settle)
        observed = fan.pwm()

        try:
            _write_int(fan.pwm_path, original)
        except OSError:
            pass

        if observed is None:
            return False, _("could not read the pwm value")
        if observed == original:
            return False, _(
                "the firmware ignores pwm writes (the value did not change) — manual speed control is not possible"
            )
        return True, ""

    def calibrate(self, fan: Fan, step: int = 4, settle: float = 0.02) -> list[int]:
        """Find the real pwm steps the driver accepts.

        On many laptops (dell_smm included) pwm is not continuous — e.g. only
        0/128/255. The fan speed changes briefly during calibration.
        """
        previous = fan.pwm()
        found: list[int] = []
        try:
            for value in list(range(0, 256, step)) + [255]:
                self.set_manual(fan)
                _write_int(fan.pwm_path, value)
                time.sleep(settle)
                actual = fan.pwm()
                if actual is not None and actual not in found:
                    found.append(actual)
        finally:
            if previous is not None:
                try:
                    _write_int(fan.pwm_path, previous)
                except OSError:
                    pass
        found.sort()
        fan.steps = found
        return found

    def snap_to_step(self, fan: Fan, value: int) -> int:
        """Snap the value to the nearest calibrated step, if there are any."""
        if not fan.steps:
            return value
        return min(fan.steps, key=lambda step: abs(step - value))


def _driver_rank(name: str) -> int:
    for rank, prefix in enumerate(PREFERRED_DRIVERS):
        if name.startswith(prefix):
            return rank
    return len(PREFERRED_DRIVERS)


def _short_gpu_name(name: Optional[str]) -> str:
    """«NVIDIA GeForce RTX 3050 Ti Laptop GPU» → «GPU RTX 3050 Ti»."""
    if not name:
        return "GPU"
    short = re.sub(r"\b(NVIDIA|GeForce|Laptop|GPU)\b", "", name).strip()
    short = re.sub(r"\s+", " ", short)
    return f"GPU {short}" if short else "GPU"


def _device_node(hwmon_path: str) -> Optional[str]:
    """Name of the block device behind an hwmon device (nvme0, nvme1, ...)."""
    real = os.path.realpath(hwmon_path)
    match = re.search(r"/(nvme\d+)(?:/|$)", real)
    return match.group(1) if match else None


def _classify_temp(
    dev_name: str, label: Optional[str], number: str, display: Optional[str] = None
) -> tuple[Optional[str], str]:
    """Map an hwmon sensor to a group and a human-friendly label."""
    display = display or dev_name
    if dev_name == "coretemp":
        if label and label.lower().startswith("package"):
            return "cpu", _("CPU package")
        return "other", f"CPU {label}" if label else _("CPU core {n}", n=number)
    if dev_name.startswith("k10temp") or dev_name.startswith("zenpower"):
        return "cpu", f"CPU {label or number}"
    if dev_name == "dell_smm":
        return "smm", f"SMM sensor {number}"
    if dev_name == "nvme":
        name = display.upper() if display.startswith("nvme") else "NVMe"
        return "nvme", f"{name} {label or number}"
    if dev_name.startswith("iwlwifi"):
        return "other", _("Wi-Fi module")
    if dev_name in ("AC", "BAT0", "BAT1") or dev_name.startswith("ucsi"):
        return None, ""
    if dev_name.startswith("amdgpu") or dev_name.startswith("nouveau"):
        return "gpu", f"GPU {label or number}"
    return "other", f"{dev_name} {label or number}"
