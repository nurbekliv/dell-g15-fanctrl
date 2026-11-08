"""fanctrl graphical interface (PyQt5).

The window runs with normal user privileges and sends every change to the
fanctrld daemon over a unix socket.
"""

from __future__ import annotations

import collections
import sys
from typing import Optional

from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PyQt5.QtCore import QPointF
from PyQt5.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .client import Client, DaemonError
from .config import DEFAULT_SOCKET
from .i18n import _

HISTORY = 180  # number of points kept (~3 minutes at 1 s intervals)

COL_BG = "#181a1f"
COL_PANEL = "#22252b"
COL_TEXT = "#e6e6e6"
COL_MUTED = "#9aa0aa"
COL_ACCENT = "#4c9aff"
COL_WARN = "#ffb020"
COL_DANGER = "#ff5252"
COL_OK = "#3ddc84"

STYLESHEET = f"""
QMainWindow, QWidget {{ background: {COL_BG}; color: {COL_TEXT};
                        font-size: 13px; }}
QFrame#card {{ background: {COL_PANEL}; border-radius: 10px; }}
QLabel {{ background: transparent; }}
QLabel#h1 {{ font-size: 17px; font-weight: 600; }}
QLabel#muted {{ color: {COL_MUTED}; }}
QLabel#value {{ font-size: 22px; font-weight: 600; }}
QLabel#rpm {{ font-size: 26px; font-weight: 700; }}
QPushButton {{ background: #2e323a; border: none; border-radius: 6px;
               padding: 7px 14px; color: {COL_TEXT}; }}
QPushButton:hover {{ background: #3a3f49; }}
/* Note: the font weight is not changed in the :checked state — otherwise the
   text no longer fits the button width. */
QPushButton:checked {{ background: {COL_ACCENT}; color: #0b0d10; }}
QPushButton:disabled {{ color: #6b7280; }}
QSpinBox {{ background: #2e323a; border: none; border-radius: 6px;
            padding: 5px 6px; color: {COL_TEXT}; min-width: 62px; }}
QSlider::groove:horizontal {{ height: 6px; background: #34383f; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {COL_ACCENT}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {COL_TEXT}; width: 16px; height: 16px;
                              margin: -6px 0; border-radius: 8px; }}
QSlider::handle:horizontal:disabled {{ background: #5b6067; }}
"""


def temp_color(value: Optional[float], crit: Optional[float]) -> str:
    if value is None:
        return COL_MUTED
    limit = crit or 100.0
    if value >= limit - 8:
        return COL_DANGER
    if value >= limit - 25:
        return COL_WARN
    return COL_OK


class Card(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("card")


class TempCard(Card):
    """Small card for a single temperature sensor."""

    def __init__(self, label: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)
        self.name = QLabel(label)
        self.name.setObjectName("muted")
        self.value = QLabel("—")
        self.value.setObjectName("value")
        layout.addWidget(self.name)
        layout.addWidget(self.value)

    def update_value(self, value: Optional[float], crit: Optional[float]) -> None:
        text = f"{value:.0f}°C" if value is not None else "—"
        self.value.setText(text)
        self.value.setStyleSheet(f"color: {temp_color(value, crit)};")


class Graph(QWidget):
    """Temperature and fan speed history (drawn without external libraries)."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.temps: dict[str, collections.deque] = {}
        self.temp_colors = {"CPU": "#ff6b6b", "GPU": "#ffb020"}
        self.rpms: dict[str, collections.deque] = {}
        self.rpm_colors = ["#4c9aff", "#3ddc84"]
        self.max_rpm = 5000

    def push(self, temps: dict[str, Optional[float]], rpms: dict[str, Optional[int]]) -> None:
        for key, value in temps.items():
            series = self.temps.setdefault(key, collections.deque(maxlen=HISTORY))
            series.append(value)
        for key, value in rpms.items():
            series = self.rpms.setdefault(key, collections.deque(maxlen=HISTORY))
            series.append(value)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt naming style)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(38, 8, -8, -18)
        painter.fillRect(self.rect(), QColor(COL_PANEL))

        # Grid and temperature axis (20..100 °C)
        painter.setPen(QPen(QColor("#33373f"), 1))
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        for temp in (20, 40, 60, 80, 100):
            y = rect.bottom() - (temp - 20) / 80.0 * rect.height()
            painter.setPen(QPen(QColor("#2c3038"), 1, Qt.DotLine))
            painter.drawLine(rect.left(), int(y), rect.right(), int(y))
            painter.setPen(QColor(COL_MUTED))
            painter.drawText(4, int(y) + 4, f"{temp}°")

        def draw_series(series, color: str, scale, width: float = 1.8) -> None:
            points = [v for v in series]
            if len(points) < 2:
                return
            step = rect.width() / max(1, HISTORY - 1)
            poly = QPolygonF()
            start = HISTORY - len(points)
            for i, value in enumerate(points):
                if value is None:
                    continue
                x = rect.left() + (start + i) * step
                y = rect.bottom() - scale(value) * rect.height()
                poly.append(QPointF(x, max(rect.top(), min(rect.bottom(), y))))
            if poly.count() >= 2:
                painter.setPen(QPen(QColor(color), width))
                painter.drawPolyline(poly)

        temp_scale = lambda v: max(0.0, min(1.0, (v - 20) / 80.0))  # noqa: E731
        rpm_scale = lambda v: max(0.0, min(1.0, v / max(1, self.max_rpm)))  # noqa: E731

        for index, (key, series) in enumerate(self.rpms.items()):
            color = self.rpm_colors[index % len(self.rpm_colors)]
            draw_series(series, color, rpm_scale, 1.4)
        for key, series in self.temps.items():
            draw_series(series, self.temp_colors.get(key, "#c792ea"), temp_scale)

        # Legend
        x = rect.left() + 4
        painter.setFont(font)
        for key in self.temps:
            painter.setPen(QColor(self.temp_colors.get(key, "#c792ea")))
            painter.drawText(int(x), rect.top() + 12, f"— {key} °C")
            x += 70
        for index, key in enumerate(self.rpms):
            painter.setPen(QColor(self.rpm_colors[index % len(self.rpm_colors)]))
            painter.drawText(int(x), rect.top() + 12, f"— {key} RPM")
            x += 90
        painter.end()


class FanPanel(Card):
    """One fan: speed readout, slider and preset buttons."""

    def __init__(self, index: int, label: str, on_change) -> None:
        super().__init__()
        self.index = index
        self.on_change = on_change
        self.steps: list[int] = []
        self._syncing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        head = QHBoxLayout()
        self.name = QLabel(label)
        self.name.setObjectName("h1")
        self.rpm = QLabel("— RPM")
        self.rpm.setObjectName("rpm")
        self.rpm.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        head.addWidget(self.name)
        head.addStretch(1)
        head.addWidget(self.rpm)
        layout.addLayout(head)

        self.detail = QLabel(_("pwm — / max — RPM"))
        self.detail.setObjectName("muted")
        layout.addWidget(self.detail)

        row = QHBoxLayout()
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)      # value is a percentage
        self.slider.setSingleStep(5)
        self.slider.setPageStep(10)
        self.slider.valueChanged.connect(self._on_slider_moved)
        self.slider.sliderReleased.connect(self._commit)
        self.percent = QLabel("—")
        self.percent.setMinimumWidth(46)
        self.percent.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.slider, 1)
        row.addWidget(self.percent)
        layout.addLayout(row)

        presets = QHBoxLayout()
        presets.setSpacing(6)
        self.preset_buttons = []
        for percent in (0, 25, 50, 75, 100):
            button = QPushButton(f"{percent}%")
            button.clicked.connect(lambda _checked, p=percent: self._apply_percent(p))
            presets.addWidget(button)
            self.preset_buttons.append(button)
        presets.addStretch(1)
        layout.addLayout(presets)

    # --------------------------------------------------------------------- events
    def _on_slider_moved(self, value: int) -> None:
        self.percent.setText(f"{value}%")
        if self._syncing:
            return
        if not self.slider.isSliderDown():
            # Changed with the keyboard or mouse wheel — send right away
            self._commit()

    def _commit(self) -> None:
        if self._syncing:
            return
        self.on_change(self.index, self.slider.value())

    def _apply_percent(self, percent: int) -> None:
        self._syncing = True
        self.slider.setValue(percent)
        self._syncing = False
        self.percent.setText(f"{percent}%")
        self.on_change(self.index, percent)

    # -------------------------------------------------------------------- updates
    def set_enabled(self, enabled: bool) -> None:
        self.slider.setEnabled(enabled)
        for button in self.preset_buttons:
            button.setEnabled(enabled)

    def set_controls_visible(self, visible: bool) -> None:
        """Hide the slider entirely on systems where pwm control is not possible."""
        self.slider.setVisible(visible)
        self.percent.setVisible(visible)
        for button in self.preset_buttons:
            button.setVisible(visible)

    def update_from(self, fan: dict, manual: bool, backend: str) -> None:
        rpm = fan.get("rpm")
        max_rpm = fan.get("max_rpm")
        setpoint = fan.get("setpoint_percent")
        self.rpm.setText(f"{rpm} RPM" if rpm is not None else "— RPM")
        color = COL_ACCENT if (rpm or 0) > 0 else COL_MUTED
        self.rpm.setStyleSheet(f"color: {color};")

        if backend == "awcc":
            level = setpoint if (manual and setpoint is not None) else 0
            detail = f"boost {level}%" + ("" if manual else " — " + _("BIOS in control"))
        else:
            pwm = fan.get("pwm")
            level = setpoint if (manual and setpoint is not None) else fan.get("percent")
            detail = f"pwm {pwm if pwm is not None else '—'}"
        if max_rpm:
            detail += "  ·  " + _("measured max {rpm} RPM", rpm=max_rpm)
        self.detail.setText(detail)

        if level is not None and not self.slider.isSliderDown():
            self._syncing = True
            self.slider.setValue(int(level))
            self._syncing = False
            self.percent.setText(f"{int(level)}%")


# Keys are the kernel's power_supply status values; only the values are shown.
BATTERY_STATUS = {
    "Full": _("full"),
    "Charging": _("charging"),
    "Discharging": _("on battery power"),
    "Not charging": _("not charging"),
    "Unknown": _("unknown"),
}

# Keys are BIOS charging mode values (protocol); only the values are shown.
BATTERY_MODE_NAMES = {
    "Adaptive": _("Adaptive"),
    "Standard": _("Standard"),
    "Express": _("Express"),
    "PrimAcUse": _("Always plugged in"),
    "Custom": _("Custom limit"),
}


def battery_advice(battery: dict) -> str:
    """Short advice for the current state (empty string = no advice needed)."""
    conf = battery.get("config") or {}
    if not conf.get("available"):
        return ""
    current = conf.get("current")
    health = battery.get("health_percent")
    ac = battery.get("ac_online")
    percent = battery.get("percent") or 0

    # Show one tip at a time, the most useful one — long text does not fit the card
    if current == "Express":
        return _("Express mode wears the battery out the most — not recommended for everyday use.")
    if current in ("Standard", "Adaptive") and ac and percent >= 90:
        extra = (
            " " + _("Capacity is already down to {health}%.", health=f"{health:.0f}")
            if health is not None and health < 80
            else ""
        )
        return (
            _("The battery is kept full while plugged in — this wears out the remaining "
              "capacity fastest.")
            + extra
            + " " + _("Stop charging at 60%.")
        )
    if health is not None and health < 40:
        return _(
            "Battery capacity is only {health}% — don't rely on running unplugged, "
            "the laptop will shut down quickly if power is lost.",
            health=f"{health:.0f}",
        )
    return ""


class BatteryCard(Card):
    """Battery status and BIOS charging policy."""

    def __init__(self, on_mode, on_custom) -> None:
        super().__init__()
        self.on_mode = on_mode
        self.on_custom = on_custom
        self.mode_buttons: dict[str, QPushButton] = {}
        self._syncing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        head = QHBoxLayout()
        self.title = QLabel(_("Battery"))
        self.title.setObjectName("h1")
        self.charge = QLabel("—")
        self.charge.setObjectName("rpm")
        self.charge.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(self.charge)
        layout.addLayout(head)

        self.detail = QLabel("—")
        self.detail.setObjectName("muted")
        layout.addWidget(self.detail)

        self.health = QLabel("—")
        layout.addWidget(self.health)

        self.policy_row = QHBoxLayout()
        self.policy_row.setSpacing(6)
        self.policy_label = QLabel(_("Charging:"))
        self.policy_label.setObjectName("muted")
        self.policy_row.addWidget(self.policy_label)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.policy_row.addStretch(1)
        layout.addLayout(self.policy_row)

        self.custom_row = QHBoxLayout()
        self.custom_row.setSpacing(6)
        custom_label = QLabel(_("Custom limit:"))
        custom_label.setObjectName("muted")
        self.start_box = QSpinBox()
        self.start_box.setRange(50, 95)
        self.start_box.setSuffix("%")
        self.stop_box = QSpinBox()
        self.stop_box.setRange(55, 100)
        self.stop_box.setSuffix("%")
        self.custom_button = QPushButton(_("Apply"))
        self.custom_button.clicked.connect(
            lambda: self.on_custom(self.start_box.value(), self.stop_box.value())
        )
        self.custom_row.addWidget(custom_label)
        self.custom_row.addWidget(self.start_box)
        # Reads as one sentence: "Custom limit: [75%] <start text> [85%] <stop text> [Apply]"
        self.custom_row.addWidget(QLabel(_("to start charging,")))
        self.custom_row.addWidget(self.stop_box)
        self.custom_row.addWidget(QLabel(_("to stop")))
        self.custom_row.addWidget(self.custom_button)
        self.custom_row.addStretch(1)
        layout.addLayout(self.custom_row)

        self.advice = QLabel("")
        self.advice.setWordWrap(True)
        self.advice.setVisible(False)
        layout.addWidget(self.advice)

    def _set_custom_visible(self, visible: bool) -> None:
        for index in range(self.custom_row.count()):
            widget = self.custom_row.itemAt(index).widget()
            if widget is not None:
                widget.setVisible(visible)

    def update_from(self, battery: dict) -> None:
        percent = battery.get("percent")
        self.charge.setText(f"{percent}%" if percent is not None else "—")
        health = battery.get("health_percent")
        color = COL_OK
        if health is not None and health < 50:
            color = COL_DANGER
        elif health is not None and health < 80:
            color = COL_WARN
        self.charge.setStyleSheet(f"color: {COL_ACCENT};")

        parts = []
        state = battery.get("status")
        if state:
            parts.append(BATTERY_STATUS.get(state, state))
        ac = battery.get("ac_online")
        if ac is not None:
            parts.append(_("plugged in") if ac else _("unplugged"))
        power = battery.get("power_w")
        if power:
            parts.append(f"{power:.1f} W")
        self.detail.setText("  ·  ".join(parts) or "—")

        if health is not None:
            capacity = ""
            if battery.get("full_wh") and battery.get("design_wh"):
                capacity = f"   {battery['full_wh']:.1f} / {battery['design_wh']:.1f} Wh"
            self.health.setText(_("Health: {health}%", health=f"{health:.1f}") + capacity)
            self.health.setStyleSheet(f"color: {color};")
        else:
            self.health.setText("")

        conf = battery.get("config") or {}
        available = bool(conf.get("available"))
        self.policy_label.setVisible(available)
        if available:
            for value in conf.get("values", []):
                button = self.mode_buttons.get(value)
                if button is None:
                    button = QPushButton(BATTERY_MODE_NAMES.get(value, value))
                    button.setCheckable(True)
                    button.setToolTip((conf.get("descriptions") or {}).get(value, ""))
                    button.clicked.connect(lambda _c, v=value: self.on_mode(v))
                    self.mode_buttons[value] = button
                    self.mode_group.addButton(button)
                    self.policy_row.insertWidget(self.policy_row.count() - 1, button)
            current = conf.get("current")
            for value, button in self.mode_buttons.items():
                button.setChecked(value == current)

            limits = conf.get("limits") or {}
            custom = conf.get("custom") or {}
            self._set_custom_visible(conf.get("kind") == "sysman")
            self._syncing = True
            if limits:
                self.start_box.setRange(limits.get("start_min", 50), limits.get("start_max", 95))
                self.stop_box.setRange(limits.get("stop_min", 55), limits.get("stop_max", 100))
            # Don't overwrite a number the user is currently typing
            if not self.start_box.hasFocus() and custom.get("start") is not None:
                self.start_box.setValue(custom["start"])
            if not self.stop_box.hasFocus() and custom.get("stop") is not None:
                self.stop_box.setValue(custom["stop"])
            self._syncing = False
        else:
            self._set_custom_visible(False)

        advice = battery_advice(battery)
        self.advice.setText(advice)
        self.advice.setVisible(bool(advice))
        self.advice.setStyleSheet(
            f"background: #33301f; color: {COL_WARN}; border-radius: 6px; padding: 8px;"
        )


class MainWindow(QMainWindow):
    def __init__(self, socket_path: str) -> None:
        super().__init__()
        self.client = Client(socket_path, timeout=4.0)
        self.panels: dict[int, FanPanel] = {}
        self.temp_cards: dict[str, TempCard] = {}
        self.setWindowTitle(_("Fan Control"))
        self.resize(920, 880)
        self.setStyleSheet(STYLESHEET)
        self._build()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    # ------------------------------------------------------------------------- UI
    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        header = QHBoxLayout()
        self.title = QLabel(_("Fan Control"))
        self.title.setObjectName("h1")
        self.subtitle = QLabel(_("connecting to the daemon…"))
        self.subtitle.setObjectName("muted")
        left = QVBoxLayout()
        left.addWidget(self.title)
        left.addWidget(self.subtitle)
        header.addLayout(left)
        header.addStretch(1)

        self.auto_button = QPushButton(_("Auto (BIOS)"))
        self.manual_button = QPushButton(_("Manual"))
        for button in (self.auto_button, self.manual_button):
            button.setCheckable(True)
            button.setMinimumWidth(120)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.auto_button)
        self.mode_group.addButton(self.manual_button)
        self.mode_group.setExclusive(True)
        self.auto_button.setChecked(True)
        self.auto_button.clicked.connect(lambda: self.set_mode("auto"))
        self.manual_button.clicked.connect(lambda: self.set_mode("manual"))
        header.addWidget(self.auto_button)
        header.addWidget(self.manual_button)
        root.addLayout(header)

        self.profile_row = QHBoxLayout()
        self.profile_row.setSpacing(6)
        self.profile_label = QLabel(_("Thermal mode:"))
        self.profile_label.setObjectName("muted")
        self.profile_row.addWidget(self.profile_label)
        self.profile_group = QButtonGroup(self)
        self.profile_group.setExclusive(True)
        self.profile_buttons: dict[str, QPushButton] = {}
        self.profile_row.addStretch(1)
        root.addLayout(self.profile_row)

        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        self.banner.setStyleSheet(
            f"background: #3a1f1f; color: {COL_DANGER}; border-radius: 8px; padding: 9px;"
        )
        root.addWidget(self.banner)

        self.temp_row = QGridLayout()
        self.temp_row.setSpacing(10)
        root.addLayout(self.temp_row)

        self.graph = Graph()
        root.addWidget(self.graph, 1)

        self.fan_row = QHBoxLayout()
        self.fan_row.setSpacing(10)
        root.addLayout(self.fan_row)

        self.battery_card = BatteryCard(self.set_battery_mode, self.set_battery_custom)
        self.battery_card.setVisible(False)
        root.addWidget(self.battery_card)

        footer = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setObjectName("muted")
        self.calibrate_button = QPushButton(_("Calibrate"))
        self.calibrate_button.setToolTip(
            _("Detects the actual pwm steps the driver accepts.\n"
              "The fan speed will change for a few seconds.")
        )
        self.calibrate_button.clicked.connect(self.calibrate)
        footer.addWidget(self.status_label, 1)
        footer.addWidget(self.calibrate_button)
        root.addLayout(footer)

    def _ensure_profiles(self, profiles: dict) -> None:
        """BIOS thermal mode buttons (the main control on systems where pwm is locked)."""
        if not profiles.get("available"):
            self.profile_label.setVisible(False)
            return
        self.profile_label.setVisible(True)
        descriptions = profiles.get("descriptions", {})
        for value in profiles.get("values", []):
            button = self.profile_buttons.get(value)
            if button is None:
                button = QPushButton(value)
                button.setCheckable(True)
                button.setToolTip(descriptions.get(value, ""))
                button.clicked.connect(lambda _c, v=value: self.set_profile(v))
                self.profile_buttons[value] = button
                self.profile_group.addButton(button)
                # Insert before the stretch
                self.profile_row.insertWidget(self.profile_row.count() - 1, button)
        current = profiles.get("current")
        for value, button in self.profile_buttons.items():
            button.setChecked(value == current)

    def set_profile(self, value: str) -> None:
        try:
            data = self.client.request({"cmd": "profile", "value": value})
            self.status_label.setText(_("thermal mode: {mode}", mode=data.get("current", value)))
        except DaemonError as exc:
            self.show_error(str(exc))
        self.refresh()

    def _ensure_panels(self, fans: list[dict]) -> None:
        for fan in fans:
            index = fan["index"]
            if index in self.panels:
                continue
            panel = FanPanel(index, fan.get("label") or _("Fan {n}", n=index), self.on_fan_change)
            self.panels[index] = panel
            self.fan_row.addWidget(panel)

    def _ensure_temp_cards(self, temps: list[dict]) -> None:
        order = {"cpu": 0, "gpu": 1, "smm": 2, "nvme": 3}
        shown = sorted(
            (t for t in temps if t["group"] in order),
            key=lambda t: (order[t["group"]], t["label"]),
        )
        for position, temp in enumerate(shown):
            card = self.temp_cards.get(temp["key"])
            if card is None:
                card = TempCard(temp["label"])
                self.temp_cards[temp["key"]] = card
                self.temp_row.addWidget(card, position // 6, position % 6)
            card.update_value(temp.get("value"), temp.get("crit"))

    # -------------------------------------------------------------------- actions
    def on_fan_change(self, index: int, percent: int) -> None:
        try:
            self.client.request({"cmd": "set", "fan": index, "percent": int(percent)})
            self.status_label.setText(_("fan {n}: {percent}% sent", n=index, percent=percent))
        except DaemonError as exc:
            self.show_error(str(exc))

    def set_mode(self, mode: str) -> None:
        try:
            self.client.set_mode(mode)
            self.status_label.setText(
                _("manual mode enabled") if mode == "manual" else _("BIOS control restored")
            )
        except DaemonError as exc:
            self.show_error(str(exc))
        self.refresh()

    def set_battery_mode(self, value: str) -> None:
        try:
            data = self.client.request({"cmd": "battery", "mode": value})
            current = (data.get("config") or {}).get("current", value)
            self.status_label.setText(
                _("charging mode: {mode} (saved in BIOS)",
                  mode=BATTERY_MODE_NAMES.get(current, current))
            )
        except DaemonError as exc:
            self.show_error(str(exc))
        self.refresh()

    def set_battery_custom(self, start: int, stop: int) -> None:
        try:
            data = self.client.request(
                {"cmd": "battery", "start": int(start), "stop": int(stop)}
            )
            custom = (data.get("config") or {}).get("custom") or {}
            self.status_label.setText(
                _("charge limit: {start}% → {stop}%",
                  start=custom.get("start"), stop=custom.get("stop"))
            )
        except DaemonError as exc:
            self.show_error(str(exc))
        self.refresh()

    def calibrate(self) -> None:
        answer = QMessageBox.question(
            self,
            _("Calibrate"),
            _("During calibration the fans will run at different speeds for a few seconds.\n"
              "Continue?"),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.calibrate_button.setEnabled(False)
        self.status_label.setText(_("calibrating…"))
        QApplication.processEvents()
        old_timeout = self.client.timeout
        try:
            self.client.timeout = 60.0
            data = self.client.calibrate("all")
            steps = data.get("steps", {})
            text = "; ".join(
                _("fan {n}: {count} steps", n=index, count=len(values))
                for index, values in steps.items()
            )
            self.status_label.setText(_("calibrated — {result}", result=text))
        except DaemonError as exc:
            self.show_error(str(exc))
        finally:
            self.client.timeout = old_timeout
            self.calibrate_button.setEnabled(True)

    def show_error(self, message: str) -> None:
        self._banner(message, "danger")

    def _banner(self, message: str, kind: str = "danger") -> None:
        colors = {
            "danger": ("#3a1f1f", COL_DANGER),
            "warn": ("#33301f", COL_WARN),
        }
        background, foreground = colors.get(kind, colors["danger"])
        self.banner.setText(message)
        self.banner.setStyleSheet(
            f"background: {background}; color: {foreground}; border-radius: 8px; padding: 9px;"
        )
        self.banner.setVisible(True)

    # -------------------------------------------------------------------- updates
    def refresh(self) -> None:
        try:
            status = self.client.status()
        except DaemonError as exc:
            self.subtitle.setText(_("no connection to the daemon"))
            self.show_error(
                _("{error}\n\nStart the daemon:  sudo systemctl start fanctrld", error=exc)
            )
            for panel in self.panels.values():
                panel.set_enabled(False)
            return

        # The banner (safety / errors / connection) is recomputed below.
        mode = status.get("mode", "auto")
        manual = mode == "manual"
        controllable = status.get("controllable", False)

        self.title.setText(status.get("model") or _("Fan Control"))
        hottest = status.get("hottest") or "—"
        max_temp = status.get("max_temp")
        temp_text = f"{max_temp:.0f}°C" if max_temp is not None else "—"
        backend_names = {"awcc": "AWCC boost", "pwm": "hwmon pwm", "none": _("no control")}
        self.subtitle.setText(
            _(
                "backend: {backend}   ·   hottest: {sensor} {temp}   ·   mode: {mode}",
                backend=backend_names.get(status.get("backend"), "—"),
                sensor=hottest,
                temp=temp_text,
                mode=_("manual") if manual else _("automatic (BIOS)"),
            )
        )

        self.manual_button.setEnabled(controllable)
        (self.manual_button if manual else self.auto_button).setChecked(True)

        governor = status.get("governor") or {}
        errors = status.get("errors") or []
        if governor.get("active"):
            self._banner("⚠ " + _("Safety override: {reason}", reason=governor.get("reason", "")))
        elif governor.get("lockout_remaining", 0) > 0:
            self._banner(
                "⛔ " + _(
                    "Critical temperature detected — manual mode is locked for {seconds} seconds",
                    seconds=f"{governor['lockout_remaining']:.0f}",
                )
            )
        elif errors:
            self._banner("! " + errors[-1])
        elif not controllable:
            reason = status.get("backend_reason") or _("no control method found")
            extra = ""
            if (status.get("profiles") or {}).get("available"):
                extra = "  " + _("Use the thermal mode above to influence the fan speed.")
            self._banner(
                _("Manual speed control is not available: {reason}.", reason=reason) + extra,
                "warn",
            )
        else:
            self.banner.setVisible(False)

        # Calibration only makes sense for the pwm backend
        self.calibrate_button.setVisible(status.get("backend") == "pwm")

        self._ensure_profiles(status.get("profiles") or {})

        temps = status.get("temps", [])
        self._ensure_temp_cards(temps)

        fans = status.get("fans", [])
        self._ensure_panels(fans)
        backend = status.get("backend", "none")
        for fan in fans:
            panel = self.panels.get(fan["index"])
            if panel is None:
                continue
            panel.update_from(fan, manual, backend)
            panel.set_controls_visible(controllable)
            panel.set_enabled(manual and controllable)

        battery = status.get("battery") or {}
        self.battery_card.setVisible(bool(battery.get("available")))
        if battery.get("available"):
            self.battery_card.update_from(battery)

        # Data for the graph
        graph_temps: dict[str, Optional[float]] = {}
        for temp in temps:
            if temp["group"] == "cpu" and "CPU" not in graph_temps:
                graph_temps["CPU"] = temp.get("value")
            elif temp["group"] == "gpu" and "GPU" not in graph_temps:
                graph_temps["GPU"] = temp.get("value")
        graph_rpms = {
            (fan.get("label") or _("Fan {n}", n=fan["index"])): fan.get("rpm") for fan in fans
        }
        max_rpm = max([fan.get("max_rpm") or 0 for fan in fans] or [0])
        if max_rpm:
            self.graph.max_rpm = max_rpm
        self.graph.push(graph_temps, graph_rpms)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.timer.stop()
        self.client.close()
        super().closeEvent(event)


def main(socket_path: Optional[str] = None) -> int:
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Fan Control")  # stable app identity, intentionally not translated
    window = MainWindow(socket_path or DEFAULT_SOCKET)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
