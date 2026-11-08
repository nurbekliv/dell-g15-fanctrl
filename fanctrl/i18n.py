"""Minimal translation layer.

The English text in the source code is the message id. A translation is a
JSON file ``fanctrl/locales/<lang>.json`` that maps each English string to its
translation::

    {
      "Fan {n}": "Kuler {n}",
      "Manual control": "Qo'lda boshqaruv"
    }

Anything missing from the file falls back to English, so a partial
translation is always safe.

The language is taken from ``FANCTRL_LANG`` and then from the usual locale
variables (``LANGUAGE``, ``LC_ALL``, ``LC_MESSAGES``, ``LANG``). For
``uz_UZ.UTF-8`` the files ``uz_UZ.json`` and ``uz.json`` are tried in that
order.

See "Adding a language" in README.md and ``tools/i18n_check.py``.
"""

import json
import os
from pathlib import Path

LOCALE_DIR = Path(__file__).resolve().parent / "locales"

_ENV_VARS = ("FANCTRL_LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")


def language_candidates(environ=None):
    """Language codes to try, most specific first (e.g. ``uz_UZ``, ``uz``)."""
    environ = os.environ if environ is None else environ
    seen = []
    for var in _ENV_VARS:
        value = environ.get(var, "")
        # LANGUAGE may be a colon separated priority list, e.g. "uz:en".
        for item in value.split(":"):
            code = item.split(".")[0].split("@")[0].strip()
            if not code or code in ("C", "POSIX"):
                continue
            for candidate in (code, code.split("_")[0]):
                if candidate not in seen:
                    seen.append(candidate)
        if seen:
            break
    return seen


def load_catalog(environ=None, locale_dir=LOCALE_DIR):
    for code in language_candidates(environ):
        if code == "en":
            return {}
        path = Path(locale_dir) / f"{code}.json"
        if not path.is_file():
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return {
            key: value
            for key, value in data.items()
            if isinstance(value, str) and value and not key.startswith("_")
        }
    return {}


_CATALOG = load_catalog()


def _(message, /, **params):
    """Translate ``message``; ``{name}`` placeholders are filled from ``params``.

    Literal braces in a message that takes parameters must be doubled
    (``{{`` / ``}}``), exactly as with ``str.format``.
    """
    text = _CATALOG.get(message, message)
    if not params:
        return text
    try:
        return text.format(**params)
    except (KeyError, IndexError, ValueError):
        # A broken translation must never crash the program.
        return message.format(**params)
