"""fanctrl — command-line interface."""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional

from .client import Client, DaemonError
from .config import DEFAULT_SOCKET
from .i18n import _

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def temp_color(value: Optional[float], crit: Optional[float]) -> str:
    if value is None:
        return "0"
    limit = crit or 100.0
    if value >= limit - 8:
        return "1;31"  # red
    if value >= limit - 25:
        return "33"    # yellow
    return "32"        # green


def bar(fraction: Optional[float], width: int = 16) -> str:
    if fraction is None:
        return "?" * width
    filled = int(max(0.0, min(1.0, fraction)) * width)
    return "█" * filled + "░" * (width - filled)


BACKEND_NAMES = {
    "awcc": "AWCC boost",
    "pwm": "hwmon pwm",
    "none": "—",
}


# Keys are the kernel's power_supply status values; only the values are shown.
BATTERY_STATUS = {
    "Full": _("full"),
    "Charging": _("charging"),
    "Discharging": _("discharging"),
    "Not charging": _("not charging"),
    "Unknown": _("unknown"),
}


def health_color(percent: Optional[float]) -> str:
    if percent is None:
        return "0"
    if percent < 50:
        return "1;31"
    if percent < 80:
        return "33"
    return "32"


def battery_line(battery: dict) -> str:
    """`Battery    100% · full · on AC   health: 26.5% (14.5/54.9 Wh)`"""
    parts = []
    percent = battery.get("percent")
    if percent is not None:
        parts.append(f"{percent}%")
    state = battery.get("status")
    if state:
        parts.append(BATTERY_STATUS.get(state, state))
    ac = battery.get("ac_online")
    if ac is not None:
        parts.append(_("on AC") if ac else _("unplugged"))
    power = battery.get("power_w")
    if power:
        parts.append(f"{power:.1f} W")

    # Pad first, then colour, so the escape codes don't break the column width
    text = c(f"{_('Battery'):<10}", "1") + " " + " · ".join(parts)
    health = battery.get("health_percent")
    if health is not None:
        detail = ""
        if battery.get("full_wh") and battery.get("design_wh"):
            detail = f" ({battery['full_wh']:.1f}/{battery['design_wh']:.1f} Wh)"
        text += "   " + _(
            "health: {percent}", percent=c(f"{health:.1f}%", health_color(health))
        ) + detail
    cycles = battery.get("cycle_count")
    if cycles:
        text += "   " + _("cycles: {count}", count=cycles)
    return text


def charge_policy_line(conf: dict) -> str:
    current = conf.get("current") or "?"
    short = (conf.get("short_names") or {}).get(current, current)
    text = f"{_('Charging'):<10} {c(short, '1;33')}"
    custom = conf.get("custom") or {}
    if current == "Custom" or custom.get("stop") is not None:
        start, stop = custom.get("start"), custom.get("stop")
        if current == "Custom":
            span = (
                f"{start}% → {stop}%"
                if start is not None
                else _("stops at {stop}%", stop=stop)
            )
            text += f"   ({span})"
    if conf.get("advanced"):
        text += c("   ⚠ " + _("Advanced Battery Charge is enabled in BIOS"), "33")
    return text


def format_status(status: dict, compact: bool = False) -> str:
    lines: list[str] = []
    mode = status.get("mode", "?")
    backend = status.get("backend", "none")
    mode_text = c(_("MANUAL"), "1;35") if mode == "manual" else c(_("AUTO (BIOS)"), "1;36")
    lines.append(
        f"{c(status.get('model', _('Unknown model')), '1')}   "
        + _(
            "backend: {backend}   mode: {mode}",
            backend=BACKEND_NAMES.get(backend, backend),
            mode=mode_text,
        )
    )

    profiles = status.get("profiles") or {}
    if profiles.get("available"):
        current = profiles.get("current") or "?"
        others = [v for v in profiles.get("values", []) if v != current]
        lines.append(
            "  " + _("Thermal mode: {mode}", mode=c(current, "1;33"))
            + ("   " + _("(others: {modes})", modes=", ".join(others)) if others else "")
        )

    if backend == "none":
        lines.append(
            c(
                "  ⓘ " + _("Manual control is not available: {reason}",
                           reason=status.get("backend_reason", "")),
                "33",
            )
        )
    elif status.get("additive_only"):
        lines.append(
            c("  ⓘ " + _("Boost is added on top of the BIOS fan curve: "
                         "0% = BIOS alone, 100% = maximum"), "2")
        )

    gov = status.get("governor") or {}
    if gov.get("active"):
        lines.append(c("  ⚠ " + _("SAFETY: {reason}", reason=gov.get("reason", "")), "1;31"))
    if gov.get("lockout_remaining", 0) > 0:
        lines.append(
            c(
                "  ⛔ " + _("locked out: {seconds} s left",
                           seconds=f"{gov['lockout_remaining']:.0f}"),
                "1;31",
            )
        )

    lines.append("")
    for fan in status.get("fans", []):
        rpm = fan.get("rpm")
        max_rpm = fan.get("max_rpm") or 0
        frac = (rpm / max_rpm) if (rpm is not None and max_rpm) else None
        rpm_text = f"{rpm:>5} RPM" if rpm is not None else "  ??? RPM"
        setpoint = fan.get("setpoint_percent")

        if backend == "awcc":
            if mode == "manual" and setpoint is not None:
                control_text = c(f"boost {setpoint:>3}%", "1;35")
            else:
                control_text = c("boost   0% (BIOS)", "2")
        else:
            pwm = fan.get("pwm")
            control_text = (
                f"pwm {pwm:>3} ({fan.get('percent'):>3}%)" if pwm is not None else "pwm  ?"
            )
        lines.append(
            f"  {fan.get('label', _('Fan')):<10} {rpm_text}  {bar(frac)}  {control_text}"
        )

    battery = status.get("battery") or {}
    if battery.get("available"):
        lines.append("")
        lines.append("  " + battery_line(battery))
        conf = battery.get("config") or {}
        if conf.get("available"):
            lines.append("  " + charge_policy_line(conf))

    lines.append("")
    groups: dict[str, list[dict]] = {}
    for temp in status.get("temps", []):
        if compact and temp["group"] == "other":
            continue
        groups.setdefault(temp["group"], []).append(temp)

    order = ["cpu", "gpu", "smm", "nvme", "other"]
    titles = {
        "cpu": "CPU",
        "gpu": "GPU",
        "smm": _("Chassis (SMM)"),
        "nvme": "NVMe",
        "other": _("Other"),
    }
    for group in order:
        items = groups.get(group)
        if not items:
            continue
        cells = []
        for temp in items:
            value = temp.get("value")
            text = f"{value:.0f}°C" if value is not None else " ?°C"
            cells.append(f"{temp['label']}: {c(text, temp_color(value, temp.get('crit')))}")
        lines.append(f"  {titles[group]:<13} " + "   ".join(cells))

    for err in status.get("errors", [])[-3:]:
        lines.append(c(f"  ! {err}", "31"))
    return "\n".join(lines)


def cmd_status(args, client: Client) -> int:
    print(format_status(client.status(), compact=not args.all))
    return 0


def cmd_watch(args, client: Client) -> int:
    interval = max(0.5, args.interval)
    try:
        while True:
            status = client.status()
            sys.stdout.write("\033[H\033[J" if USE_COLOR else "\n")
            print(format_status(status, compact=not args.all))
            print(c("\n  " + _("refresh: {interval}s   quit: Ctrl+C",
                               interval=f"{interval:.1f}"), "2"))
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        return 0


def _parse_speed(text: str) -> dict:
    """Accept `80%`, `0.8`, `200` (pwm) or `max`/`off`."""
    text = text.strip().lower()
    if text in ("max", "full", "100"):
        return {"percent": 100.0}
    if text in ("off", "0"):
        return {"pwm": 0}
    if text.endswith("%"):
        return {"percent": float(text[:-1])}
    value = float(text)
    if 0.0 < value <= 1.0:
        return {"percent": value * 100.0}
    return {"pwm": int(value)}


def cmd_set(args, client: Client) -> int:
    try:
        speed = _parse_speed(args.speed)
    except ValueError:
        print(c(_("could not parse speed: {speed}", speed=args.speed), "31"), file=sys.stderr)
        return 2
    fan = args.fan if args.fan == "all" else int(args.fan)
    data = client.request({"cmd": "set", "fan": fan, **speed})
    applied = data.get("applied", {})
    for index, value in applied.items():
        print(_("fan {n}: {percent}%", n=index, percent=value))
    if data.get("governor_active"):
        print(c(_("note: safety override is active — fans are at 100% now"), "33"))
    print(c(_("mode: manual"), "2"))
    return 0


def cmd_mode(args, client: Client) -> int:
    data = client.set_mode(args.mode)
    print(_("mode: {mode}", mode=data.get("mode")))
    if args.mode == "auto":
        print(c(_("BIOS control restored (fan speed will change within a few seconds)"), "2"))
    return 0


def cmd_calibrate(args, client: Client) -> int:
    client.timeout = 60.0
    print(_("calibrating... fan speed will change briefly"), flush=True)
    data = client.calibrate(args.fan if args.fan == "all" else int(args.fan))
    for index, steps in data.get("steps", {}).items():
        percents = ", ".join(f"{s} ({round(s * 100 / 255)}%)" for s in steps)
        print(_("fan {n}: {count} steps → {steps}", n=index, count=len(steps), steps=percents))
    return 0


def cmd_profile(args, client: Client) -> int:
    if not args.value:
        data = client.request({"cmd": "profile"})
        current = data.get("current")
        descriptions = data.get("descriptions", {})
        print(f"{data.get('name', _('Thermal mode'))}:")
        for value in data.get("values", []):
            mark = c("●", "1;32") if value == current else "○"
            print(f"  {mark} {value:<18} {descriptions.get(value, '')}")
        return 0

    # Accept any letter case: 'cool' → 'Cool'
    data = client.request({"cmd": "profile"})
    values = data.get("values", [])
    match = next((v for v in values if v.lower() == args.value.lower()), args.value)
    result = client.request({"cmd": "profile", "value": match})
    print(_("thermal mode: {mode}", mode=c(result.get("current", match), "1;32")))
    return 0


def cmd_battery(args, client: Client) -> int:
    value = (args.value or "").lower()

    # Show only
    if not value:
        data = client.request({"cmd": "battery"})
        if not data.get("available"):
            print(c(_("no battery found"), "33"))
            return 1
        print(battery_line(data))
        conf = data.get("config") or {}
        if not conf.get("available"):
            print(c(_("the charge limit cannot be controlled by software on this system"), "2"))
            return 0
        print(charge_policy_line(conf))
        print()
        current = conf.get("current")
        descriptions = conf.get("descriptions", {})
        for name in conf.get("values", []):
            mark = c("●", "1;32") if name == current else "○"
            print(f"  {mark} {name:<12} {descriptions.get(name, '')}")
        limits = conf.get("limits") or {}
        if limits:
            print(
                c(
                    "\n  " + _(
                        "custom limit: start {start_min}–{start_max}%, "
                        "stop {stop_min}–{stop_max}%",
                        start_min=limits.get("start_min", "?"),
                        start_max=limits.get("start_max", "?"),
                        stop_min=limits.get("stop_min", "?"),
                        stop_max=limits.get("stop_max", "?"),
                    ),
                    "2",
                )
            )
        return 0

    # Custom limit:  fanctrl battery custom 50 60
    # ("chegara" is the Uzbek alias of "custom"; kept for compatibility.)
    if value in ("custom", "limit", "chegara"):
        numbers = args.range
        if not numbers:
            print(c(_("specify the percentages, e.g.:  fanctrl battery custom 50 60"), "31"),
                  file=sys.stderr)
            return 2
        if len(numbers) == 1:
            start, stop = None, numbers[0]
        else:
            start, stop = numbers[0], numbers[1]
        payload = {"cmd": "battery", "stop": stop}
        if start is not None:
            payload["start"] = start
        data = client.request(payload)
    else:
        # Accept any letter case: 'primacuse' → 'PrimAcUse'
        info = client.request({"cmd": "battery"})
        values = ((info.get("config") or {}).get("values")) or []
        match = next((v for v in values if v.lower() == value), args.value)
        data = client.request({"cmd": "battery", "mode": match})

    conf = data.get("config") or {}
    print(charge_policy_line(conf))
    print(c(_("saved in BIOS — stays in effect after a reboot"), "2"))
    return 0


def cmd_config(args, client: Client) -> int:
    for key, value in client.config().items():
        print(f"{key:<18} = {value}")
    return 0


def cmd_gui(args, client: Client) -> int:
    from .gui import main as gui_main

    return gui_main(socket_path=args.socket)


def build_parser() -> argparse.ArgumentParser:
    # argparse runs help texts through %-formatting, so a literal % is written %%.
    parser = argparse.ArgumentParser(
        prog="fanctrl",
        description=_("Fan monitoring and manual fan control for Linux"),
    )
    parser.add_argument("--socket", default=DEFAULT_SOCKET, help=_("path to the daemon socket"))
    parser.add_argument("-a", "--all", action="store_true", help=_("show all sensors"))
    sub = parser.add_subparsers(dest="command")

    # -a is accepted both before and after the subcommand ("fanctrl -a status" and
    # "fanctrl status -a"); SUPPRESS keeps the subparser from resetting the global value.
    p = sub.add_parser("status", help=_("show the current status"))
    p.add_argument("-a", "--all", action="store_true", default=argparse.SUPPRESS,
                   help=_("show all sensors"))
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("watch", help=_("live monitoring in the terminal"))
    p.add_argument("-a", "--all", action="store_true", default=argparse.SUPPRESS,
                   help=_("show all sensors"))
    p.add_argument("-n", "--interval", type=float, default=2.0, help=_("refresh interval"))
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("set", help=_("set the fan speed manually"))
    p.add_argument("fan", help=_("fan number or 'all'"))
    p.add_argument("speed", help=_("e.g. 70%%, 0.7, 200 (pwm), max, off"))
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("auto", help=_("return to automatic BIOS control"))
    p.set_defaults(func=cmd_mode, mode="auto")

    p = sub.add_parser("manual", help=_("switch to manual mode"))
    p.set_defaults(func=cmd_mode, mode="manual")

    p = sub.add_parser("calibrate", help=_("detect the pwm steps the driver accepts"))
    p.add_argument("fan", nargs="?", default="all", help=_("fan number or 'all'"))
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("profile", help=_("show or change the BIOS thermal mode"))
    p.add_argument("value", nargs="?", help=_("e.g. Cool, Quiet, Optimized, UltraPerformance"))
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("battery", help=_("battery status and charge limit"))
    p.add_argument(
        "value",
        nargs="?",
        help=_("mode (Adaptive, Standard, Express, PrimAcUse) or 'custom'"),
    )
    p.add_argument(
        "range",
        nargs="*",
        type=int,
        help=_("for 'custom': start and stop percentage, e.g.  custom 50 60"),
    )
    p.set_defaults(func=cmd_battery)

    p = sub.add_parser("config", help=_("show the daemon settings"))
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("gui", help=_("open the graphical window"))
    p.set_defaults(func=cmd_gui)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.command = "status"
        args.func = cmd_status
        args.all = getattr(args, "all", False)

    if args.func is cmd_gui:
        return cmd_gui(args, None)  # the GUI opens its own connection

    client = Client(args.socket)
    try:
        return args.func(args, client)
    except DaemonError as exc:
        print(c(_("error: {error}", error=exc), "1;31"), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
