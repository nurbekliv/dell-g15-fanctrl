"""Daemon/client protocol: line-delimited JSON over a unix socket."""

from __future__ import annotations

import json

from .i18n import _

ENCODING = "utf-8"
MAX_LINE = 1 << 20  # 1 MiB — protection against a misbehaving client


def encode(message: dict) -> bytes:
    return (json.dumps(message, separators=(",", ":")) + "\n").encode(ENCODING)


def decode(line: bytes) -> dict:
    data = json.loads(line.decode(ENCODING))
    if not isinstance(data, dict):
        raise ValueError(_("the request must be a JSON object"))
    return data


def ok(data: dict | None = None) -> dict:
    return {"ok": True, "data": data or {}}


def error(message: str) -> dict:
    return {"ok": False, "error": message}
