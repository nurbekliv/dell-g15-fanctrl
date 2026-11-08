"""Daemon settings (/etc/fanctrld.conf)."""

from __future__ import annotations

import configparser
import os
from dataclasses import asdict, dataclass, field

CONFIG_PATH = os.environ.get("FANCTL_CONFIG", "/etc/fanctrld.conf")
DEFAULT_SOCKET = "/run/fanctrld.sock"
STATE_PATH = "/var/lib/fanctrld/state.json"


@dataclass
class Config:
    # [daemon]
    socket_path: str = DEFAULT_SOCKET
    socket_group: str = ""       # empty — only root can connect
    poll_interval: float = 2.0   # sensor polling interval (seconds)
    # Order in which fans are shown in the UI, e.g. "2,1" (empty = natural order).
    # This only affects the display — fan numbers do not change.
    fan_order: list[int] = field(default_factory=list)

    # [safety] — enforced inside the daemon, so they stay in effect when the GUI is closed
    warn_temp: float = 85.0        # above this temperature the fans are forced to 100%
    warn_hysteresis: float = 6.0   # manual mode resumes once it drops this much
    critical_temp: float = 93.0    # at this temperature manual mode is cancelled entirely
    lockout_seconds: float = 120.0 # manual mode is blocked this long after a critical event
    min_manual_pwm: int = 0        # lowest pwm allowed in manual mode

    # [state]
    restore_on_start: bool = False  # restore the last mode when the daemon restarts

    def as_dict(self) -> dict:
        return asdict(self)


def load(path: str = CONFIG_PATH) -> Config:
    cfg = Config()
    parser = configparser.ConfigParser()
    if not parser.read(path):
        return cfg

    def get(section: str, option: str, kind, current):
        if not parser.has_option(section, option):
            return current
        try:
            if kind is bool:
                return parser.getboolean(section, option)
            if kind is int:
                return parser.getint(section, option)
            if kind is float:
                return parser.getfloat(section, option)
            return parser.get(section, option)
        except ValueError:
            return current

    cfg.socket_path = get("daemon", "socket_path", str, cfg.socket_path)
    cfg.socket_group = get("daemon", "socket_group", str, cfg.socket_group)
    cfg.poll_interval = max(0.5, get("daemon", "poll_interval", float, cfg.poll_interval))

    raw_order = get("daemon", "fan_order", str, "")
    if raw_order:
        order: list[int] = []
        for part in raw_order.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                continue
            if value not in order:
                order.append(value)
        cfg.fan_order = order

    cfg.warn_temp = get("safety", "warn_temp", float, cfg.warn_temp)
    cfg.warn_hysteresis = get("safety", "warn_hysteresis", float, cfg.warn_hysteresis)
    cfg.critical_temp = get("safety", "critical_temp", float, cfg.critical_temp)
    cfg.lockout_seconds = get("safety", "lockout_seconds", float, cfg.lockout_seconds)
    cfg.min_manual_pwm = max(0, min(255, get("safety", "min_manual_pwm", int, cfg.min_manual_pwm)))

    cfg.restore_on_start = get("state", "restore_on_start", bool, cfg.restore_on_start)

    # Sanity check: the critical limit must not be below the warning limit
    if cfg.critical_temp <= cfg.warn_temp:
        cfg.critical_temp = cfg.warn_temp + 5.0
    return cfg
