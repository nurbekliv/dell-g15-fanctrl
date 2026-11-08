/* Fan Control — GNOME Shell top panel extension.
 *
 * Shows the temperature and fan speed in the top-right corner; its menu lets
 * you switch modes and set fan speeds with sliders.
 * Every action is sent to the fanctrld daemon over a Unix socket.
 *
 * Note: GNOME Shell runs on a single thread — all I/O here must be
 * asynchronous, otherwise the whole desktop freezes.
 */

const { Clutter, Gio, GLib, GObject, St } = imports.gi;

const ByteArray = imports.byteArray;
const ExtensionUtils = imports.misc.extensionUtils;
const Main = imports.ui.main;
const PanelMenu = imports.ui.panelMenu;
const PopupMenu = imports.ui.popupMenu;
const Slider = imports.ui.slider;
const Util = imports.misc.util;

const SOCKET_PATH = '/run/fanctrld.sock';
const POLL_CLOSED = 5;   // seconds, while the menu is closed
const POLL_OPEN = 1;     // seconds, while the menu is open
const SEND_THROTTLE_MS = 250;
const ECHO_GUARD_US = 1500000;  // ignore external updates for 1.5 s after a slider change is sent

// Limits set with a single click from the menu: [start, stop]
const BATTERY_LIMIT_PRESETS = [[50, 60], [65, 75], [75, 85]];

/* ------------------------------------------------------------ translations */
/*
 * Same scheme as fanctrl/i18n.py: the English text is the message id, and
 * locales/<lang>.json in the extension directory maps it to a translation.
 * Missing or empty entries fall back to English. The language comes from
 * FANCTRL_LANG, then from the session locale (for uz_UZ.UTF-8: uz_UZ.json,
 * then uz.json); "en" means no translation.
 *
 * The catalog is read once in enable(). Reading one small file from the
 * extension's own directory is the only synchronous I/O here — GNOME Shell
 * reads metadata.json the same way.
 */

let translations = new Map();

function languageCandidates() {
    const sources = [
        (GLib.getenv('FANCTRL_LANG') || '').split(':'),
        GLib.get_language_names(),
    ];
    for (const names of sources) {
        const found = [];
        for (const name of names) {
            const code = String(name).split('.')[0].split('@')[0].trim();
            if (!code || code === 'C' || code === 'POSIX')
                continue;
            for (const candidate of [code, code.split('_')[0]]) {
                if (!found.includes(candidate))
                    found.push(candidate);
            }
        }
        if (found.length)
            return found;
    }
    return [];
}

function loadTranslations(localeDir) {
    translations = new Map();
    try {
        for (const code of languageCandidates()) {
            if (code === 'en')
                return;
            const file = localeDir.get_child(`${code}.json`);
            if (file.query_file_type(Gio.FileQueryInfoFlags.NONE, null) !== Gio.FileType.REGULAR)
                continue;
            const [, bytes] = file.load_contents(null);
            const data = JSON.parse(ByteArray.toString(bytes));
            const loaded = new Map();
            if (data && typeof data === 'object' && !Array.isArray(data)) {
                for (const [key, value] of Object.entries(data)) {
                    if (typeof value === 'string' && value && !key.startsWith('_'))
                        loaded.set(key, value);
                }
            }
            translations = loaded;
            return;
        }
    } catch (e) {
        // An unreadable or invalid file means English, never a broken extension.
        translations = new Map();
        log(`fanctrl: translations not loaded: ${e.message}`);
    }
}

// Fills {name} placeholders; "{{" and "}}" are literal braces, as in Python's
// str.format. Returns null when a placeholder has no value or a brace is unpaired.
function formatMessage(text, params) {
    let ok = true;
    const result = text.replace(/\{\{|\}\}|\{(\w*)\}|[{}]/g, (match, key) => {
        if (match === '{{')
            return '{';
        if (match === '}}')
            return '}';
        if (key !== undefined && Object.prototype.hasOwnProperty.call(params, key))
            return String(params[key]);
        ok = false;
        return match;
    });
    return ok ? result : null;
}

function _(message, params) {
    const text = translations.get(message) || message;
    if (!params || Object.keys(params).length === 0)
        return text;
    const result = formatMessage(text, params);
    if (result !== null)
        return result;
    // A broken translation must never break the extension.
    const fallback = formatMessage(message, params);
    return fallback !== null ? fallback : message;
}

/* ------------------------------------------------------------------ labels */

// Built on each call so the names are translated after enable() loaded the catalog.
function batteryStatusName(status) {
    const names = {
        'Full': _('full'),
        'Charging': _('charging'),
        'Discharging': _('discharging'),
        'Not charging': _('not charging'),
        'Unknown': _('unknown'),
    };
    return names[status] || status || '';
}

function batteryModeName(mode) {
    const names = {
        'Adaptive': _('Adaptive'),
        'Standard': _('Standard (up to 100%)'),
        'Express': _('Express'),
        'PrimAcUse': _('Primarily AC use (~50%)'),
        'Custom': _('Custom limit'),
    };
    return names[mode] || mode || '';
}

function decodeLine(line) {
    if (line === null || line === undefined)
        return '';
    if (line instanceof Uint8Array)
        return ByteArray.toString(line);
    return String(line);
}

/* ------------------------------------------------------------------ client */

class DaemonClient {
    constructor() {
        this._conn = null;
        this._input = null;
        this._queue = [];
        this._busy = false;
        this._cancellable = new Gio.Cancellable();
    }

    destroy() {
        this._cancellable.cancel();
        this._queue = [];
        this._closeConnection();
    }

    _closeConnection() {
        if (this._conn) {
            try {
                this._conn.close(null);
            } catch (e) {
                // already closed — nothing to do
            }
        }
        this._conn = null;
        this._input = null;
    }

    request(message, callback) {
        this._queue.push({ message, callback });
        this._pump();
    }

    _pump() {
        if (this._busy || this._queue.length === 0)
            return;

        this._busy = true;
        const job = this._queue.shift();
        const finish = (error, data) => {
            this._busy = false;
            try {
                job.callback(error, data);
            } catch (e) {
                logError(e, 'fanctrl: error while handling the response');
            }
            this._pump();
        };

        this._ensureConnection(error => {
            if (error) {
                this._closeConnection();
                finish(error, null);
                return;
            }
            this._exchange(job.message, finish);
        });
    }

    _ensureConnection(callback) {
        if (this._conn && !this._conn.is_closed()) {
            callback(null);
            return;
        }
        this._closeConnection();

        const client = new Gio.SocketClient();
        const address = new Gio.UnixSocketAddress({ path: SOCKET_PATH });
        client.connect_async(address, this._cancellable, (source, result) => {
            try {
                this._conn = client.connect_finish(result);
                this._input = new Gio.DataInputStream({
                    base_stream: this._conn.get_input_stream(),
                });
                callback(null);
            } catch (e) {
                this._conn = null;
                this._input = null;
                callback(e);
            }
        });
    }

    _exchange(message, finish) {
        const output = this._conn.get_output_stream();
        const bytes = new GLib.Bytes(`${JSON.stringify(message)}\n`);

        output.write_bytes_async(bytes, GLib.PRIORITY_DEFAULT, this._cancellable, (src, res) => {
            try {
                output.write_bytes_finish(res);
            } catch (e) {
                this._closeConnection();
                finish(e, null);
                return;
            }

            this._input.read_line_async(GLib.PRIORITY_DEFAULT, this._cancellable, (src2, res2) => {
                let raw;
                try {
                    [raw] = this._input.read_line_finish(res2);
                } catch (e) {
                    this._closeConnection();
                    finish(e, null);
                    return;
                }
                if (raw === null) {
                    this._closeConnection();
                    finish(new Error(_('the daemon closed the connection')), null);
                    return;
                }

                let response;
                try {
                    response = JSON.parse(decodeLine(raw));
                } catch (e) {
                    finish(e, null);
                    return;
                }
                if (!response.ok) {
                    finish(new Error(response.error || _('unknown error')), null);
                    return;
                }
                finish(null, response.data || {});
            });
        });
    }
}

/* --------------------------------------------------------------- indicator */

const FanIndicator = GObject.registerClass(
class FanIndicator extends PanelMenu.Button {
    _init() {
        super._init(0.5, 'Fan Control');

        this._client = new DaemonClient();
        this._rows = new Map();     // fan index -> {item, name, slider, ...}
        this._timerId = 0;
        this._pollInterval = 0;
        this._manual = false;
        this._connected = false;
        this._batteryLimitsBuilt = false;

        const box = new St.BoxLayout({ style_class: 'panel-status-menu-box' });
        this._icon = new St.Icon({
            icon_name: 'weather-windy-symbolic',
            style_class: 'system-status-icon',
        });
        this._panelLabel = new St.Label({
            text: '—',
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'fanctrl-panel-label',
        });
        box.add_child(this._icon);
        box.add_child(this._panelLabel);
        this.add_child(box);

        this._buildMenu();

        this.menu.connect('open-state-changed', (menu, isOpen) => {
            this._setPolling(isOpen ? POLL_OPEN : POLL_CLOSED);
            if (isOpen)
                this._refresh();
        });

        this._setPolling(POLL_CLOSED);
        this._refresh();
    }

    /* -------------------------------------------------------------- menu */
    _buildMenu() {
        this._headerItem = new PopupMenu.PopupMenuItem(_('Connecting…'), {
            reactive: false,
            style_class: 'fanctrl-header',
        });
        this.menu.addMenuItem(this._headerItem);

        this._tempItem = new PopupMenu.PopupMenuItem('', { reactive: false });
        this._tempItem.label.clutter_text.line_wrap = true;
        this.menu.addMenuItem(this._tempItem);

        this._bannerItem = new PopupMenu.PopupMenuItem('', { reactive: false });
        this._bannerItem.label.clutter_text.line_wrap = true;
        this._bannerItem.label.style_class = 'fanctrl-banner';
        this._bannerItem.visible = false;
        this.menu.addMenuItem(this._bannerItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._fanSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._fanSection);

        this._presetItem = new PopupMenu.PopupBaseMenuItem({
            reactive: false,
            can_focus: false,
        });
        const presetBox = new St.BoxLayout({ style_class: 'fanctrl-presets', x_expand: true });
        this._presetButtons = [];
        [0, 25, 50, 75, 100].forEach(percent => {
            const button = new St.Button({
                label: `${percent}%`,
                style_class: 'button fanctrl-preset',
                x_expand: true,
                can_focus: true,
            });
            button.connect('clicked', () => this._applyPercentToAll(percent));
            presetBox.add_child(button);
            this._presetButtons.push(button);
        });
        this._presetItem.add_child(presetBox);
        this.menu.addMenuItem(this._presetItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        /* BIOS thermal mode — the main control on laptops where PWM is locked */
        this._profileMenu = new PopupMenu.PopupSubMenuMenuItem(_('Thermal mode'), false);
        this._profileItems = new Map();
        this._profileMenu.visible = false;
        this.menu.addMenuItem(this._profileMenu);

        /* Battery: status and the BIOS charging policy */
        this._batteryMenu = new PopupMenu.PopupSubMenuMenuItem(_('Battery'), false);
        this._batteryItems = new Map();
        this._batteryMenu.visible = false;
        this._batteryInfo = new PopupMenu.PopupMenuItem('', { reactive: false });
        this._batteryInfo.label.clutter_text.line_wrap = true;
        this._batteryMenu.menu.addMenuItem(this._batteryInfo);
        this._batteryMenu.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._batteryModeSection = new PopupMenu.PopupMenuSection();
        this._batteryMenu.menu.addMenuItem(this._batteryModeSection);
        this._batteryLimitSection = new PopupMenu.PopupMenuSection();
        this._batteryMenu.menu.addMenuItem(this._batteryLimitSection);
        this.menu.addMenuItem(this._batteryMenu);

        this._modeSwitch = new PopupMenu.PopupSwitchMenuItem(_('Manual control'), false);
        this._modeSwitch.connect('toggled', (item, state) => {
            this._setMode(state ? 'manual' : 'auto');
        });
        this.menu.addMenuItem(this._modeSwitch);

        const windowItem = new PopupMenu.PopupMenuItem(_('Open window…'));
        windowItem.connect('activate', () => {
            try {
                Util.spawn(['fanctrl-gui']);
            } catch (e) {
                logError(e, 'fanctrl: could not start fanctrl-gui');
            }
        });
        this.menu.addMenuItem(windowItem);
    }

    _ensureRow(fan) {
        if (this._rows.has(fan.index))
            return this._rows.get(fan.index);

        const item = new PopupMenu.PopupBaseMenuItem({ reactive: false, can_focus: false });
        const column = new St.BoxLayout({ vertical: true, x_expand: true });

        const titleRow = new St.BoxLayout({ x_expand: true });
        const name = new St.Label({
            text: fan.label || _('Fan {n}', { n: fan.index }),
            x_expand: true,
        });
        const value = new St.Label({ text: '—', style_class: 'fanctrl-fan-value' });
        titleRow.add_child(name);
        titleRow.add_child(value);

        const sliderRow = new St.BoxLayout({ x_expand: true, style_class: 'fanctrl-slider-row' });
        const slider = new Slider.Slider(0);
        slider.x_expand = true;
        sliderRow.add_child(slider);

        column.add_child(titleRow);
        column.add_child(sliderRow);
        item.add_child(column);
        this._fanSection.addMenuItem(item);

        const row = {
            item,
            name,
            value,
            slider,
            sliderRow,
            dragging: false,
            syncing: false,
            lastSent: 0,
            throttleId: 0,
        };

        slider.connect('drag-begin', () => {
            row.dragging = true;
        });
        slider.connect('drag-end', () => {
            row.dragging = false;
            this._sendLevel(fan.index, row, true);
        });
        slider.connect('notify::value', () => {
            const percent = Math.round(slider.value * 100);
            row.value.text = row.rpmText ? `${row.rpmText}   ${percent}%` : `${percent}%`;
            // Programmatic update from the status — do not echo it back to the daemon
            if (row.syncing)
                return;
            this._sendLevel(fan.index, row, false);
        });

        this._rows.set(fan.index, row);
        return row;
    }

    _updateProfiles(profiles) {
        if (!profiles || !profiles.available) {
            this._profileMenu.visible = false;
            return;
        }
        this._profileMenu.visible = true;
        const current = profiles.current;
        const descriptions = profiles.descriptions || {};

        (profiles.values || []).forEach(value => {
            if (!this._profileItems.has(value)) {
                const text = descriptions[value] ? `${value} — ${descriptions[value]}` : value;
                const item = new PopupMenu.PopupMenuItem(text);
                item.connect('activate', () => this._setProfile(value));
                this._profileMenu.menu.addMenuItem(item);
                this._profileItems.set(value, item);
            }
            const item = this._profileItems.get(value);
            item.setOrnament(value === current
                ? PopupMenu.Ornament.DOT : PopupMenu.Ornament.NONE);
        });

        this._profileMenu.label.text = current
            ? _('Thermal mode: {mode}', { mode: current }) : _('Thermal mode');
    }

    _updateBattery(battery) {
        if (!battery || !battery.available) {
            this._batteryMenu.visible = false;
            return;
        }
        this._batteryMenu.visible = true;

        const state = batteryStatusName(battery.status);
        const info = [];
        if (battery.percent !== null && battery.percent !== undefined)
            info.push(`${battery.percent}%`);
        if (state)
            info.push(state);
        if (battery.ac_online !== null && battery.ac_online !== undefined)
            info.push(battery.ac_online ? _('plugged in') : _('unplugged'));
        if (battery.health_percent !== null && battery.health_percent !== undefined) {
            let health = _('health {percent}%', { percent: battery.health_percent.toFixed(1) });
            if (battery.full_wh && battery.design_wh)
                health += ` (${battery.full_wh.toFixed(1)}/${battery.design_wh.toFixed(1)} Wh)`;
            info.push(health);
        }
        this._batteryInfo.label.text = info.join('  ·  ');

        const config = battery.config || {};
        if (!config.available) {
            this._batteryMenu.label.text = _('Battery: {percent}%', { percent: battery.percent });
            return;
        }

        const current = config.current;
        (config.values || []).forEach(value => {
            if (!this._batteryItems.has(value)) {
                const item = new PopupMenu.PopupMenuItem(batteryModeName(value));
                item.connect('activate', () => this._setBatteryMode(value));
                this._batteryModeSection.addMenuItem(item);
                this._batteryItems.set(value, item);
            }
            this._batteryItems.get(value).setOrnament(
                value === current ? PopupMenu.Ornament.DOT : PopupMenu.Ornament.NONE);
        });

        // Common charge limits — only when "Custom" is supported
        if ((config.values || []).indexOf('Custom') >= 0 && !this._batteryLimitsBuilt) {
            this._batteryLimitsBuilt = true;
            BATTERY_LIMIT_PRESETS.forEach(([start, stop]) => {
                const item = new PopupMenu.PopupMenuItem(
                    _('Charge limit: {start}% → {stop}%', { start, stop }));
                item.connect('activate', () => this._setBatteryLimit(start, stop));
                this._batteryLimitSection.addMenuItem(item);
            });
        }

        // Short form for the submenu title: the mode name without its "(…)" note
        const short = batteryModeName(current).replace(/\s*\(.*\)\s*$/, '');
        this._batteryMenu.label.text =
            _('Battery: {percent}% — {mode}', { percent: battery.percent, mode: short });
    }

    _setBatteryMode(value) {
        this._client.request({ cmd: 'battery', mode: value }, (error) => {
            if (error)
                this._showBanner(error.message);
            this._refresh();
        });
    }

    _setBatteryLimit(start, stop) {
        this._client.request({ cmd: 'battery', start, stop }, (error) => {
            if (error)
                this._showBanner(error.message);
            this._refresh();
        });
    }

    _setProfile(value) {
        this._client.request({ cmd: 'profile', value }, (error) => {
            if (error)
                this._showBanner(error.message);
            this._refresh();
        });
    }

    /* ----------------------------------------------------------- actions */
    _sendLevel(index, row, immediate) {
        row.lastSent = GLib.get_monotonic_time();

        const send = () => {
            row.throttleId = 0;
            // Read the percentage at send time — a delayed send must carry the
            // slider's latest position
            const percent = Math.round(row.slider.value * 100);
            this._client.request({ cmd: 'set', fan: index, percent }, (error, data) => {
                if (error) {
                    this._showBanner(error.message);
                    return;
                }
                if (data && data.mode === 'manual')
                    this._manual = true;
            });
            return GLib.SOURCE_REMOVE;
        };

        // The throttle is per fan — otherwise one slider would cancel the
        // other's pending send
        if (row.throttleId) {
            GLib.source_remove(row.throttleId);
            row.throttleId = 0;
        }
        if (immediate)
            send();
        else
            row.throttleId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, SEND_THROTTLE_MS, send);
    }

    _applyPercentToAll(percent) {
        this._client.request({ cmd: 'set', fan: 'all', percent }, (error) => {
            if (error) {
                this._showBanner(error.message);
                return;
            }
            this._manual = true;
            this._refresh();
        });
    }

    _setMode(mode) {
        this._client.request({ cmd: 'mode', mode }, (error) => {
            if (error)
                this._showBanner(error.message);
            this._refresh();
        });
    }

    _showBanner(text) {
        this._bannerItem.label.text = text;
        this._bannerItem.visible = true;
    }

    /* ----------------------------------------------------------- refresh */
    _setPolling(seconds) {
        if (this._pollInterval === seconds && this._timerId)
            return;
        if (this._timerId) {
            GLib.source_remove(this._timerId);
            this._timerId = 0;
        }
        this._pollInterval = seconds;
        this._timerId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, seconds, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _refresh() {
        this._client.request({ cmd: 'status' }, (error, status) => {
            if (error) {
                this._onDisconnected(error);
                return;
            }
            this._onStatus(status);
        });
    }

    _onDisconnected(error) {
        this._connected = false;
        this._panelLabel.text = '—';
        this._headerItem.label.text = _('No connection to the fanctrld daemon');
        this._tempItem.label.text =
            _('Start it with:  {command}', { command: 'sudo systemctl start fanctrld' });
        this._showBanner(String(error.message || error));
        this._modeSwitch.setSensitive(false);
        this._presetButtons.forEach(button => button.reactive = false);
        this._rows.forEach(row => {
            row.slider.reactive = false;
            row.value.text = '—';
        });
    }

    _onStatus(status) {
        this._connected = true;
        const manual = status.mode === 'manual';
        this._manual = manual;
        const controllable = !!status.controllable;

        /* Panel label: the hottest sensor and the fastest fan */
        const maxTemp = status.max_temp;
        const fans = status.fans || [];
        const topRpm = fans.reduce((acc, fan) => Math.max(acc, fan.rpm || 0), 0);
        const tempText = maxTemp === null || maxTemp === undefined
            ? '—' : `${Math.round(maxTemp)}°`;
        const rpmText = topRpm >= 1000
            ? `${(topRpm / 1000).toFixed(1)}k` : `${topRpm}`;
        this._panelLabel.text = `${tempText} · ${rpmText}`;

        this._headerItem.label.text =
            `${status.model || 'Fan Control'} — ${manual ? _('manual') : _('automatic (BIOS)')}`;

        const temps = status.temps || [];
        const pick = group => temps.find(t => t.group === group && t.value !== null);
        const parts = [];
        const cpu = pick('cpu');
        const gpu = pick('gpu');
        if (cpu)
            parts.push(`CPU ${Math.round(cpu.value)}°`);
        if (gpu)
            parts.push(`GPU ${Math.round(gpu.value)}°`);
        const smm = temps.filter(t => t.group === 'smm' && t.value !== null);
        if (smm.length) {
            const hottest = Math.max(...smm.map(t => t.value));
            parts.push(_('Chassis {temp}°', { temp: Math.round(hottest) }));
        }
        const nvme = temps.filter(t => t.group === 'nvme' && t.value !== null);
        if (nvme.length) {
            const hottest = Math.max(...nvme.map(t => t.value));
            parts.push(`NVMe ${Math.round(hottest)}°`);
        }
        this._tempItem.label.text = parts.join('   ');

        /* Warning banner */
        const governor = status.governor || {};
        const errors = status.errors || [];
        if (governor.active) {
            this._showBanner(`⚠ ${governor.reason || _('safety override active')}`);
        } else if (governor.lockout_remaining > 0) {
            const seconds = Math.round(governor.lockout_remaining);
            this._showBanner(
                `⛔ ${_('Critical temperature — manual mode locked for {seconds} s', { seconds })}`);
        } else if (!controllable) {
            const reason = status.pwm_reason || _('no PWM control found');
            const hint = (status.profiles || {}).available
                ? ` — ${_('use Thermal mode instead')}` : '';
            this._showBanner(`${reason}${hint}`);
        } else if (errors.length) {
            this._showBanner(`! ${errors[errors.length - 1]}`);
        } else {
            this._bannerItem.visible = false;
        }

        /* BIOS thermal mode and battery */
        this._updateProfiles(status.profiles);
        this._updateBattery(status.battery);

        /* Mode switch and presets — hidden entirely where PWM control does not work */
        this._modeSwitch.visible = controllable;
        this._presetItem.visible = controllable;
        this._modeSwitch.setSensitive(controllable);
        if (this._modeSwitch.state !== manual)
            this._modeSwitch.setToggleState(manual);
        this._presetButtons.forEach(button => {
            button.reactive = controllable;
            button.opacity = controllable ? 255 : 128;
        });

        /* Fans — values are percentages (a "boost" level on AWCC, the speed on PWM) */
        const now = GLib.get_monotonic_time();
        fans.forEach(fan => {
            const row = this._ensureRow(fan);
            const rpm = fan.rpm === null || fan.rpm === undefined ? '—' : `${fan.rpm} RPM`;
            const setpoint = fan.setpoint_percent;
            const fallback = status.backend === 'awcc' ? 0 : fan.percent;
            const shown = manual && setpoint !== null && setpoint !== undefined
                ? setpoint : fallback;

            row.slider.reactive = manual && controllable;
            row.sliderRow.visible = controllable;
            row.item.visible = true;

            if (!row.dragging && now - row.lastSent > ECHO_GUARD_US &&
                shown !== null && shown !== undefined) {
                const value = Math.max(0, Math.min(1, shown / 100));
                if (Math.abs(row.slider.value - value) > 0.004) {
                    row.syncing = true;
                    row.slider.value = value;
                    row.syncing = false;
                }
            }
            row.rpmText = rpm;
            const percent = Math.round(row.slider.value * 100);
            row.value.text = `${rpm}   ${percent}%`;
        });
    }

    destroy() {
        if (this._timerId) {
            GLib.source_remove(this._timerId);
            this._timerId = 0;
        }
        this._rows.forEach(row => {
            if (row.throttleId) {
                GLib.source_remove(row.throttleId);
                row.throttleId = 0;
            }
        });
        this._client.destroy();
        super.destroy();
    }
});

/* --------------------------------------------------------------- extension */

let indicator = null;

function init() {
    // nothing to do when the extension is loaded
}

function enable() {
    try {
        loadTranslations(ExtensionUtils.getCurrentExtension().dir.get_child('locales'));
    } catch (e) {
        translations = new Map();
        log(`fanctrl: translations not loaded: ${e.message}`);
    }
    indicator = new FanIndicator();
    Main.panel.addToStatusArea('fanctrl', indicator, 0, 'right');
}

function disable() {
    if (indicator) {
        indicator.destroy();
        indicator = null;
    }
}
