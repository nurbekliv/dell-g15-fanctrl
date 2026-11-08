"""fanctrld — fan control daemon that runs as root.

Responsibilities:
  * read the sensors periodically and cache the latest state;
  * hold a manually set pwm value (rewrite it whenever the BIOS takes it
    back);
  * safety governor — force the fans to 100% when temperatures climb, and hand
    control back to the BIOS entirely at the critical limit;
  * serve GUI/CLI clients over a unix socket.
"""

from __future__ import annotations

import errno
import grp
import json
import logging
import os
import selectors
import signal
import socket
import sys
import time
from typing import Optional

from . import protocol
from .awcc import Awcc, AwccError
from .battery import Battery
from .config import STATE_PATH, Config, load as load_config
from .hwmon import FanController
from .i18n import _
from .thermal import ThermalProfiles

log = logging.getLogger("fanctrld")

MODE_AUTO = "auto"
MODE_MANUAL = "manual"

# Control backends
BACKEND_AWCC = "awcc"   # Dell AWCC boost (0–100%), can only add cooling
BACKEND_PWM = "pwm"     # classic hwmon pwm (0–255)
BACKEND_NONE = "none"


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.ctl = FanController()
        self.profiles = ThermalProfiles()
        self.battery = Battery()
        # BIOS charging attributes are read through WMI — keep them cached
        self._batt_cfg: dict = {}
        self._batt_cfg_at = 0.0
        self.awcc = Awcc()
        self.awcc_map: dict[int, int] = {}
        # Whether pwm writes actually take effect (None = not probed yet)
        self.pwm_effective: Optional[bool] = None
        self.pwm_reason = ""
        self.backend = BACKEND_NONE
        self.backend_reason = ""
        self.mode = MODE_AUTO
        # Manual setpoints — always in PERCENT (0–100)
        self.setpoints: dict[int, int] = {}
        self.governor_active = False
        self.governor_reason = ""
        self.lockout_until = 0.0
        self.write_failures = 0
        self.errors: list[str] = []
        self.status: dict = {}
        self.running = True
        self._sel = selectors.DefaultSelector()
        self._clients: dict[socket.socket, bytearray] = {}
        self._server: Optional[socket.socket] = None
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)

    # ------------------------------------------------------------------ startup
    def setup(self) -> None:
        if not self.ctl.controllable:
            log.warning(
                "no pwm control found — the daemon runs in monitoring-only mode"
            )
        else:
            log.info(
                "driver=%s fans=%s model=%s",
                self.ctl.driver,
                [f.index for f in self.ctl.fans],
                self.ctl.model,
            )
        if self.profiles.available:
            log.info(
                "thermal modes (%s): %s",
                self.profiles.name,
                ", ".join(self.profiles.values),
            )
        self._select_backend()
        self._load_state()
        self._open_socket()
        self._sel.register(self._wake_r, selectors.EVENT_READ, self._drain_wakeup)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._on_signal)

    def _select_backend(self) -> None:
        """Work out which method can be used to control the fans.

        AWCC boost is preferred: it is reliable and can only add cooling.
        Classic pwm is probed only when AWCC is not available.
        """
        if self.awcc.available:
            self.awcc_map = self.awcc.map_to_hwmon(self.ctl.read_fans())
            self.backend = BACKEND_AWCC
            self.backend_reason = ""
            log.info(
                "control method: AWCC boost (fans: %s)",
                ", ".join(f"{i}→{fid:#x}" for i, fid in sorted(self.awcc_map.items())),
            )
            return

        if self.awcc.reason:
            log.info("AWCC not available: %s", self.awcc.reason)

        if self._check_pwm_effective():
            self.backend = BACKEND_PWM
            self.backend_reason = ""
            log.info("control method: hwmon pwm")
            return

        self.backend = BACKEND_NONE
        self.backend_reason = self.pwm_reason or self.awcc.reason or _("no control method found")

    @property
    def can_reduce_cooling(self) -> bool:
        """Whether a manual setting can REDUCE cooling (pwm — yes, boost — no)."""
        return self.backend == BACKEND_PWM

    def _check_pwm_effective(self) -> bool:
        """Whether pwm control really works — probed once and remembered."""
        if self.pwm_effective is not None:
            return self.pwm_effective
        if not self.ctl.controllable:
            self.pwm_effective = False
            self.pwm_reason = _("no pwm output found")
            return False
        try:
            ok, reason = self.ctl.probe_pwm_support()
        except OSError as exc:
            ok, reason = False, _("pwm probe failed: {error}", error=exc)
        self.pwm_effective = ok
        self.pwm_reason = reason
        if ok:
            log.info("pwm control works — manual mode available")
        else:
            log.warning("manual pwm control not possible: %s", reason)
        return ok

    def _on_signal(self, signum, _frame) -> None:
        self.running = False
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass

    def _drain_wakeup(self, _key) -> None:
        try:
            os.read(self._wake_r, 4096)
        except OSError:
            pass

    def _open_socket(self) -> None:
        path = self.cfg.socket_path
        if os.path.exists(path):
            # If another instance is running — stop; otherwise remove the stale socket
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.5)
                probe.connect(path)
                probe.close()
                raise SystemExit(f"fanctrld is already running ({path})")
            except (ConnectionRefusedError, socket.timeout, OSError):
                pass
            finally:
                probe.close()
            os.unlink(path)

        os.makedirs(os.path.dirname(path) or "/", exist_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.setblocking(False)
        server.bind(path)
        server.listen(8)

        gid = -1
        if self.cfg.socket_group:
            try:
                gid = grp.getgrnam(self.cfg.socket_group).gr_gid
            except KeyError:
                log.error("socket_group=%s not found, the socket is accessible to root only",
                          self.cfg.socket_group)
        if gid >= 0:
            os.chown(path, 0, gid)
            os.chmod(path, 0o660)
        else:
            os.chmod(path, 0o600)

        self._server = server
        self._sel.register(server, selectors.EVENT_READ, self._accept)
        log.info("socket: %s (group=%s)", path, self.cfg.socket_group or "root")

    # ------------------------------------------------------------------- state file
    def _load_state(self) -> None:
        if not self.cfg.restore_on_start:
            return
        try:
            with open(STATE_PATH) as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            return
        if state.get("mode") != MODE_MANUAL:
            return
        points: dict[int, int] = {}
        for key, value in (state.get("setpoints") or {}).items():
            try:
                index, percent = int(key), int(value)
            except (TypeError, ValueError):
                continue
            # Values are stored in percent; values from an old/corrupt file are rejected
            if 0 <= percent <= 100:
                points[index] = percent
        if points:
            self.setpoints = points
            self.mode = MODE_MANUAL
            log.info("restored the last manual setpoints: %s", points)

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"mode": self.mode, "setpoints": self.setpoints}, fh)
            os.replace(tmp, STATE_PATH)
        except OSError as exc:
            log.debug("could not save state: %s", exc)

    # ------------------------------------------------------------------- main loop
    def run(self) -> int:
        self.setup()
        next_tick = 0.0
        try:
            while self.running:
                now = time.monotonic()
                if now >= next_tick:
                    self.tick()
                    next_tick = now + self.cfg.poll_interval
                timeout = max(0.05, next_tick - time.monotonic())
                for key, _events in self._sel.select(timeout):
                    key.data(key)
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        log.info("shutting down — returning the fans to automatic control")
        self._release_to_auto()
        for conn in list(self._clients):
            self._close_client(conn)
        if self._server is not None:
            try:
                self._sel.unregister(self._server)
            except KeyError:
                pass
            self._server.close()
            try:
                os.unlink(self.cfg.socket_path)
            except OSError:
                pass

    # -------------------------------------------------------------------- control
    def _release_to_auto(self) -> None:
        if self.backend == BACKEND_AWCC:
            for fan_id in self.awcc_map.values():
                try:
                    self.awcc.set_boost(fan_id, 0)
                except AwccError as exc:
                    log.error("could not reset boost to 0 (%#x): %s", fan_id, exc)
            return
        for fan in self.ctl.fans:
            try:
                self.ctl.set_auto(fan)
            except OSError as exc:
                log.error("fan %s did not return to automatic control: %s", fan.index, exc)

    def _apply_setpoints(self, forced_full: bool) -> None:
        """Hold the requested value in manual mode.

        The AWCC boost value cannot be read back, so it is rewritten every time
        (which also keeps the firmware from resetting it on its own).
        With pwm the value is written only when it does not match.
        """
        failed = False

        if self.backend == BACKEND_AWCC:
            for index, fan_id in self.awcc_map.items():
                target = 100 if forced_full else self.setpoints.get(index)
                if target is None:
                    continue
                try:
                    self.awcc.set_boost(fan_id, target)
                except AwccError as exc:
                    failed = True
                    self._push_error(
                        _("fan {index}: could not set boost ({error})", index=index, error=exc)
                    )
            self.write_failures = self.write_failures + 1 if failed else 0
            if self.write_failures >= 3:
                self.write_failures = 0
                self._trip(_("AWCC boost failed several times — switched to automatic mode"))
            return

        for fan in self.ctl.fans:
            percent = 100 if forced_full else self.setpoints.get(fan.index)
            if percent is None:
                continue
            target = self.ctl.snap_to_step(fan, round(percent * 255 / 100))
            current = fan.pwm()
            if current is not None and current == target:
                continue
            try:
                actual = self.ctl.set_pwm(fan, target)
                if actual != target:
                    # The driver snapped the value to a step — this is normal
                    log.debug("fan %s: requested %s, applied %s", fan.index, target, actual)
            except OSError as exc:
                failed = True
                self._push_error(
                    _("fan {index}: could not write pwm ({error})", index=fan.index, error=exc)
                )

        # Repeated write failures — no point in holding on to control
        self.write_failures = self.write_failures + 1 if failed else 0
        if self.write_failures >= 3:
            self.write_failures = 0
            self._trip(_("pwm writes failed several times — switched to automatic mode"))

    def _ordered_fans(self) -> list[dict]:
        """Sort the fans by the configured `fan_order` (display only)."""
        fans = self.ctl.read_fans()
        if not self.cfg.fan_order:
            return fans
        rank = {index: position for position, index in enumerate(self.cfg.fan_order)}
        # Fans not in the list go last, in their natural order
        return sorted(fans, key=lambda fan: rank.get(fan["index"], len(rank) + fan["index"]))

    def _battery_status(self, refresh_config: bool = False) -> dict:
        """Battery status. The BIOS settings are refreshed at most every 30 seconds."""
        now = time.monotonic()
        if refresh_config or not self._batt_cfg or now - self._batt_cfg_at > 30.0:
            try:
                self._batt_cfg = self.battery.config.as_dict()
            except OSError as exc:
                self._batt_cfg = {"available": False, "error": str(exc)}
            self._batt_cfg_at = now
        return self.battery.read(self._batt_cfg)

    def _push_error(self, message: str) -> None:
        log.error(message)
        self.errors.append(message)
        del self.errors[:-5]

    def tick(self) -> None:
        temps = self.ctl.read_temps()
        max_temp, hot_label = self.ctl.max_governed_temp(temps)
        now = time.monotonic()

        forced_full = False
        if self.mode == MODE_MANUAL and not self.can_reduce_cooling:
            # Boost can only add cooling — the user's setting cannot cause
            # overheating. So control is never taken away here; the boost is
            # only raised to 100% when needed.
            if max_temp is None or max_temp >= self.cfg.warn_temp:
                forced_full = True
                if not self.governor_active:
                    self.governor_reason = (
                        _(
                            "{sensor} {temp}°C ≥ {limit}°C — boost 100%",
                            sensor=hot_label,
                            temp=f"{max_temp:.0f}",
                            limit=f"{self.cfg.warn_temp:.0f}",
                        )
                        if max_temp is not None
                        else _("temperature sensors could not be read — boost 100%")
                    )
                    log.warning(self.governor_reason)
                self.governor_active = True
            elif self.governor_active and max_temp <= self.cfg.warn_temp - self.cfg.warn_hysteresis:
                self.governor_active = False
                self.governor_reason = ""
                log.info("temperature back to normal — manual setpoints restored")
            elif self.governor_active:
                forced_full = True
            self._apply_setpoints(forced_full)
        elif self.mode == MODE_MANUAL:
            if max_temp is None:
                self._trip(
                    _("temperature sensors could not be read — control returned to automatic")
                )
            elif max_temp >= self.cfg.critical_temp:
                self._trip(
                    _(
                        "CRITICAL: {sensor} {temp}°C — manual mode cancelled",
                        sensor=hot_label,
                        temp=f"{max_temp:.0f}",
                    )
                )
                self.lockout_until = now + self.cfg.lockout_seconds
            elif max_temp >= self.cfg.warn_temp:
                forced_full = True
                if not self.governor_active:
                    self.governor_reason = _(
                        "{sensor} {temp}°C ≥ {limit}°C — fans forced to 100%",
                        sensor=hot_label,
                        temp=f"{max_temp:.0f}",
                        limit=f"{self.cfg.warn_temp:.0f}",
                    )
                    log.warning(self.governor_reason)
                self.governor_active = True
            elif self.governor_active and max_temp <= self.cfg.warn_temp - self.cfg.warn_hysteresis:
                self.governor_active = False
                self.governor_reason = ""
                log.info("temperature back to normal — manual setpoints restored")
            elif self.governor_active:
                forced_full = True  # hysteresis band: do not lower yet

            if self.mode == MODE_MANUAL:
                self._apply_setpoints(forced_full)
        else:
            self.governor_active = False
            self.governor_reason = ""

        fans = self._ordered_fans()
        for fan in fans:
            fan["setpoint_percent"] = self.setpoints.get(fan["index"])
            fan["awcc_id"] = self.awcc_map.get(fan["index"])
        profiles = self.profiles.as_dict()
        profiles["current"] = self.profiles.current()
        self.status = {
            "model": self.ctl.model,
            "driver": self.ctl.driver,
            "backend": self.backend,
            "backend_reason": self.backend_reason,
            # Boost can only add cooling — clients need to know this
            "additive_only": self.backend == BACKEND_AWCC,
            "controllable": self.backend != BACKEND_NONE,
            "pwm_effective": self.pwm_effective,
            "pwm_reason": self.pwm_reason,
            "profiles": profiles,
            "battery": self._battery_status(),
            "mode": self.mode,
            "setpoints": dict(self.setpoints),
            "fans": fans,
            "temps": temps,
            "max_temp": max_temp,
            "hottest": hot_label,
            "governor": {
                "active": self.governor_active,
                "reason": self.governor_reason,
                "warn_temp": self.cfg.warn_temp,
                "critical_temp": self.cfg.critical_temp,
                "lockout_remaining": max(0.0, self.lockout_until - now),
            },
            "errors": list(self.errors),
            "timestamp": time.time(),
        }

    def _trip(self, reason: str) -> None:
        """Cancel manual mode for safety reasons."""
        self._push_error(reason)
        self.mode = MODE_AUTO
        self.governor_active = False
        self.governor_reason = reason
        self._release_to_auto()
        self._save_state()

    # --------------------------------------------------------------------- clients
    def _accept(self, key) -> None:
        try:
            conn, _addr = key.fileobj.accept()
        except OSError:
            return
        conn.setblocking(False)
        self._clients[conn] = bytearray()
        self._sel.register(conn, selectors.EVENT_READ, self._on_client_data)

    def _close_client(self, conn: socket.socket) -> None:
        try:
            self._sel.unregister(conn)
        except KeyError:
            pass
        self._clients.pop(conn, None)
        conn.close()

    def _on_client_data(self, key) -> None:
        conn = key.fileobj
        try:
            chunk = conn.recv(65536)
        except OSError as exc:
            if exc.errno == errno.EAGAIN:
                return
            self._close_client(conn)
            return
        if not chunk:
            self._close_client(conn)
            return

        buf = self._clients.get(conn)
        if buf is None:
            return
        buf.extend(chunk)
        if len(buf) > protocol.MAX_LINE:
            self._close_client(conn)
            return

        while b"\n" in buf:
            line, _sep, rest = bytes(buf).partition(b"\n")
            buf.clear()
            buf.extend(rest)
            if not line.strip():
                continue
            try:
                request = protocol.decode(line)
            except (ValueError, UnicodeDecodeError) as exc:
                self._send(conn, protocol.error(_("invalid request: {error}", error=exc)))
                continue
            self._send(conn, self.handle(request))

    def _send(self, conn: socket.socket, message: dict) -> None:
        try:
            conn.sendall(protocol.encode(message))
        except OSError:
            self._close_client(conn)

    # --------------------------------------------------------------------- commands
    def handle(self, request: dict) -> dict:
        cmd = str(request.get("cmd", "")).lower()
        try:
            if cmd == "status":
                if not self.status:
                    self.tick()
                return protocol.ok(self.status)
            if cmd == "ping":
                return protocol.ok({"pong": True, "version": _version()})
            if cmd == "config":
                return protocol.ok(self.cfg.as_dict())
            if cmd == "mode":
                return self._cmd_mode(request)
            if cmd == "set":
                return self._cmd_set(request)
            if cmd == "calibrate":
                return self._cmd_calibrate(request)
            if cmd == "profile":
                return self._cmd_profile(request)
            if cmd == "battery":
                return self._cmd_battery(request)
            return protocol.error(_("unknown command: {cmd}", cmd=cmd or _("(empty)")))
        except OSError as exc:
            return protocol.error(_("device error: {error}", error=exc))

    def _check_controllable(self) -> Optional[dict]:
        if self.backend == BACKEND_NONE:
            if self.profiles.available:
                return protocol.error(
                    _(
                        "{reason} — use a thermal mode instead: {modes}",
                        reason=self.backend_reason,
                        modes=", ".join(self.profiles.values),
                    )
                )
            return protocol.error(self.backend_reason)
        remaining = self.lockout_until - time.monotonic()
        if remaining > 0:
            return protocol.error(
                _(
                    "safety lockout: manual mode unlocks in {seconds} seconds",
                    seconds=f"{remaining:.0f}",
                )
            )
        return None

    def _cmd_mode(self, request: dict) -> dict:
        mode = str(request.get("mode", "")).lower()
        if mode not in (MODE_AUTO, MODE_MANUAL):
            return protocol.error(_("mode must be either 'auto' or 'manual'"))
        if mode == MODE_MANUAL:
            problem = self._check_controllable()
            if problem:
                return problem
            if not self.setpoints:
                # Starting point: 0 with AWCC (BIOS in charge), the current value with pwm
                for fan in self.ctl.fans:
                    if self.backend == BACKEND_AWCC:
                        self.setpoints[fan.index] = 0
                    else:
                        current = fan.pwm()
                        if current is not None:
                            self.setpoints[fan.index] = round(current * 100 / 255)
            self.mode = MODE_MANUAL
        else:
            self.mode = MODE_AUTO
            self.governor_active = False
            self.governor_reason = ""
            self._release_to_auto()
        self._save_state()
        self.tick()
        return protocol.ok({"mode": self.mode, "setpoints": dict(self.setpoints)})

    def _resolve_percent(self, request: dict) -> tuple[Optional[int], Optional[str]]:
        """Extract the percentage from a request (pwm 0–255 is accepted too)."""
        if "percent" in request:
            try:
                percent = float(request["percent"])
            except (TypeError, ValueError):
                return None, _("percent must be a number")
        elif "pwm" in request:
            try:
                percent = int(request["pwm"]) * 100 / 255
            except (TypeError, ValueError):
                return None, _("pwm must be an integer")
        else:
            return None, _("neither percent nor pwm was given")
        return int(round(max(0.0, min(100.0, percent)))), None

    def _cmd_set(self, request: dict) -> dict:
        problem = self._check_controllable()
        if problem:
            return problem
        value, err = self._resolve_percent(request)
        if err:
            return protocol.error(err)
        if self.backend == BACKEND_PWM:
            minimum = round(self.cfg.min_manual_pwm * 100 / 255)
            value = max(value, minimum)

        target = request.get("fan", "all")
        if target in ("all", "*", None):
            fans = list(self.ctl.fans)
        else:
            try:
                fan = self.ctl.fan_by_index(int(target))
            except (TypeError, ValueError):
                return protocol.error(_("invalid fan number: {fan}", fan=target))
            if fan is None:
                return protocol.error(_("fan {fan} not found", fan=target))
            fans = [fan]

        self.mode = MODE_MANUAL  # `set` switches to manual mode automatically
        applied = {}
        for fan in fans:
            self.setpoints[fan.index] = value
            applied[fan.index] = 100 if self.governor_active else value
        self._apply_setpoints(self.governor_active)
        self._save_state()
        self.tick()
        return protocol.ok(
            {
                "mode": self.mode,
                "requested": value,
                "applied": applied,
                "governor_active": self.governor_active,
            }
        )

    def _cmd_profile(self, request: dict) -> dict:
        """Read or change the BIOS thermal mode."""
        if not self.profiles.available:
            return protocol.error(_("no thermal mode setting found on this system"))
        value = request.get("value")
        if value is None:
            data = self.profiles.as_dict()
            data["current"] = self.profiles.current()
            return protocol.ok(data)
        try:
            applied = self.profiles.set(str(value))
        except ValueError as exc:
            return protocol.error(str(exc))
        except OSError as exc:
            return protocol.error(_("could not change the thermal mode: {error}", error=exc))
        log.info("thermal mode: %s", applied)
        self.tick()
        return protocol.ok({"current": applied, "values": self.profiles.values})

    def _cmd_battery(self, request: dict) -> dict:
        """Read the battery status or change the charging policy."""
        mode = request.get("mode")
        stop = request.get("stop")
        start = request.get("start")

        if mode is None and stop is None and start is None:
            return protocol.ok(self._battery_status(refresh_config=True))

        cfg = self.battery.config
        if not cfg.available:
            return protocol.error(
                _("the charge limit cannot be changed from software on this system (it can be set manually in the BIOS)")
            )

        try:
            if stop is not None or start is not None:
                if stop is None:
                    return protocol.error(_("a charge limit needs `stop` (the stop percentage)"))
                cfg.set_custom(
                    int(start) if start is not None else None,
                    int(stop),
                )
            elif mode is not None:
                cfg.set_mode(str(mode))
        except ValueError as exc:
            return protocol.error(str(exc))
        except OSError as exc:
            return protocol.error(_("the charging setting was not changed: {error}", error=exc))

        status = self._battery_status(refresh_config=True)
        log.info(
            "battery policy: %s %s",
            status.get("config", {}).get("current"),
            status.get("config", {}).get("custom"),
        )
        return protocol.ok(status)

    def _cmd_calibrate(self, request: dict) -> dict:
        problem = self._check_controllable()
        if problem:
            return problem
        if self.backend != BACKEND_PWM:
            return protocol.error(
                _(
                    "calibration only makes sense with the hwmon pwm method (current method: {backend})",
                    backend=self.backend,
                )
            )
        target = request.get("fan", "all")
        fans = (
            list(self.ctl.fans)
            if target in ("all", "*", None)
            else [self.ctl.fan_by_index(int(target))]
        )
        fans = [f for f in fans if f is not None]
        if not fans:
            return protocol.error(_("fan {fan} not found", fan=target))
        result = {}
        for fan in fans:
            result[fan.index] = self.ctl.calibrate(fan)
            log.info("fan %s steps: %s", fan.index, result[fan.index])
        self.tick()
        return protocol.ok({"steps": result})


def _version() -> str:
    from . import __version__

    return __version__


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="fanctrld", description=_("Fan control daemon"))
    parser.add_argument("--config", default=None, help=_("path to the config file"))
    parser.add_argument("--socket", default=None, help=_("path to the unix socket"))
    parser.add_argument("--debug", action="store_true", help=_("verbose logging"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    if os.geteuid() != 0:
        log.error("fanctrld must run as root")
        return 1

    cfg = load_config(args.config) if args.config else load_config()
    if args.socket:
        cfg.socket_path = args.socket
    return Daemon(cfg).run()


if __name__ == "__main__":
    raise SystemExit(main())
