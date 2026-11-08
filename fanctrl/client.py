"""Client that talks to the daemon (used by both the CLI and the GUI)."""

from __future__ import annotations

import socket
from typing import Optional

from . import protocol
from .config import DEFAULT_SOCKET
from .i18n import _


class DaemonError(RuntimeError):
    """An error returned by the daemon, or a connection problem."""


class Client:
    def __init__(self, socket_path: str = DEFAULT_SOCKET, timeout: float = 5.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._buf = bytearray()

    # ------------------------------------------------------------------- connection
    def connect(self) -> None:
        if self._sock is not None:
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
        except FileNotFoundError:
            sock.close()
            raise DaemonError(
                _(
                    "socket not found: {path}\nIs the daemon running? Check: systemctl status fanctrld",
                    path=self.socket_path,
                )
            )
        except PermissionError:
            sock.close()
            raise DaemonError(
                _(
                    "permission denied for {path} — your user is not in the socket group",
                    path=self.socket_path,
                )
            )
        except OSError as exc:
            sock.close()
            raise DaemonError(_("could not connect to the daemon: {error}", error=exc))
        self._sock = sock
        self._buf.clear()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        self._buf.clear()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def __enter__(self) -> "Client":
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------------- requests
    def request(self, message: dict) -> dict:
        self.connect()
        assert self._sock is not None
        try:
            self._sock.sendall(protocol.encode(message))
            line = self._read_line()
        except (OSError, socket.timeout) as exc:
            self.close()
            raise DaemonError(_("connection lost: {error}", error=exc))
        try:
            response = protocol.decode(line)
        except ValueError as exc:
            self.close()
            raise DaemonError(_("could not read the response: {error}", error=exc))
        if not response.get("ok"):
            raise DaemonError(response.get("error", _("unknown error")))
        return response.get("data", {})

    def _read_line(self) -> bytes:
        assert self._sock is not None
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise OSError(_("the daemon closed the connection"))
            self._buf.extend(chunk)
            if len(self._buf) > protocol.MAX_LINE:
                raise OSError(_("response too long"))
        line, _sep, rest = bytes(self._buf).partition(b"\n")
        self._buf.clear()
        self._buf.extend(rest)
        return line

    # -------------------------------------------------------------- convenience methods
    def status(self) -> dict:
        return self.request({"cmd": "status"})

    def ping(self) -> dict:
        return self.request({"cmd": "ping"})

    def config(self) -> dict:
        return self.request({"cmd": "config"})

    def set_mode(self, mode: str) -> dict:
        return self.request({"cmd": "mode", "mode": mode})

    def set_pwm(self, fan, pwm: int) -> dict:
        return self.request({"cmd": "set", "fan": fan, "pwm": int(pwm)})

    def set_percent(self, fan, percent: float) -> dict:
        return self.request({"cmd": "set", "fan": fan, "percent": float(percent)})

    def calibrate(self, fan="all") -> dict:
        return self.request({"cmd": "calibrate", "fan": fan})
