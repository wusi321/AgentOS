// SPDX-License-Identifier: GPL-2.0-or-later
//
// AgentOS GNOME Shell extension - shared helpers.
//
// This module is deliberately dependency-free (it imports nothing from GNOME
// Shell) so it can be loaded from extension.js, prefs.js and from future
// components without pulling in Shell internals.
//
// Security note: every helper below treats its input as untrusted data. The
// JSON documents received from the AgentOS session broker are parsed and then
// defensively coerced; nothing here touches the filesystem, spawns a process or
// holds a secret.

// Classic `imports.*` GI style: ESModules are not reliable on GNOME 42.
const GLib = imports.gi.GLib;

/**
 * Normalize an unknown value to a plain object.
 * @param {*} value Any value, typically a value unpacked from D-Bus or JSON.
 * @param {Object} fallback Object returned when `value` is not a usable object.
 * @returns {Object} `value` when it is a non-null object, otherwise `fallback`.
 */
function asObject(value, fallback) {
    const empty = fallback === undefined ? {} : fallback;
    if (value === null || value === undefined)
        return empty;
    if (typeof value !== 'object')
        return empty;
    if (Array.isArray(value))
        return empty;
    return value;
}

/**
 * Normalize an unknown value to an array.
 * @param {*} value Any value, typically a value unpacked from D-Bus or JSON.
 * @returns {Array} `value` when it is an array, otherwise a new empty array.
 */
function asArray(value) {
    if (Array.isArray(value))
        return value;
    return [];
}

/**
 * Coerce an unknown value to a string with a defensive length cap.
 * @param {*} value Any value.
 * @param {string} fallback String returned when `value` is not a string.
 * @param {number} maxLength Hard cap applied to the returned string.
 * @returns {string} A plain string, never longer than `maxLength` characters.
 */
function asString(value, fallback, maxLength) {
    const limit = typeof maxLength === 'number' ? maxLength : 256;
    let text = '';
    if (typeof value === 'string')
        text = value;
    else if (typeof value === 'number' && isFinite(value))
        text = String(value);
    else
        text = typeof fallback === 'string' ? fallback : '';
    if (text.length > limit)
        text = text.substring(0, limit);
    return text;
}

/**
 * Coerce an unknown value to a finite number.
 * @param {*} value Any value.
 * @param {number} fallback Number returned when `value` is not usable.
 * @returns {number} A finite number.
 */
function asNumber(value, fallback) {
    if (typeof value === 'number' && isFinite(value))
        return value;
    if (typeof value === 'string') {
        const parsed = Number(value);
        if (isFinite(parsed))
            return parsed;
    }
    return fallback;
}

/**
 * Coerce an unknown value to a boolean.
 * @param {*} value Any value.
 * @param {boolean} fallback Boolean returned when `value` is not a boolean.
 * @returns {boolean} A boolean.
 */
function asBoolean(value, fallback) {
    if (typeof value === 'boolean')
        return value;
    return fallback === true;
}

/**
 * Parse a JSON document that arrived over D-Bus.
 * @param {*} text String (or already-parsed value) to interpret.
 * @returns {Object|null} The parsed object, or null when the input is not a
 *   JSON object. Malformed input never throws.
 */
function parseJsonObject(text) {
    if (text === null || text === undefined)
        return null;
    if (typeof text === 'object')
        return Array.isArray(text) ? null : text;
    if (typeof text !== 'string')
        return null;
    const raw = text.trim();
    if (raw.length === 0)
        return null;
    try {
        const parsed = JSON.parse(raw);
        if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed))
            return null;
        return parsed;
    } catch (error) {
        // A service that emits malformed JSON is a data problem, not a crash:
        // report it at the console and degrade to "no data".
        log('[AgentOS] ignoring malformed JSON payload: ' + error);
        return null;
    }
}

/**
 * Truncate a display string, appending an ellipsis when it was shortened.
 * @param {*} value Value to render.
 * @param {number} maxLength Maximum number of characters kept.
 * @returns {string} A single-line string safe to put in a menu label.
 */
function truncate(value, maxLength) {
    const limit = typeof maxLength === 'number' ? maxLength : 64;
    let text = asString(value, '', 512);
    text = text.replace(/[\r\n\t]+/g, ' ');
    if (text.length <= limit)
        return text;
    if (limit <= 1)
        return text.substring(0, limit);
    return text.substring(0, limit - 1) + '\u2026';
}

/**
 * Build the clock-time stamp used by event menu rows.
 * @param {number} seconds Unix epoch seconds, or 0 when the time is unknown.
 * @returns {string} `HH:MM` in local time (or `MM-DD HH:MM` for older events),
 *   or '' when the time is unknown. Uses String#padStart (ES2017, available in
 *   GJS 42) instead of the GJS-only String#format extension.
 */
function formatClock(seconds) {
    if (typeof seconds !== 'number' || !isFinite(seconds) || seconds <= 0)
        return '';
    const now = GLib.DateTime.new_now_local();
    const moment = GLib.DateTime.new_from_unix_local(Math.floor(seconds));
    if (moment === null || now === null)
        return '';

    const hour = String(moment.get_hour()).padStart(2, '0');
    const minute = String(moment.get_minute()).padStart(2, '0');
    if (moment.get_year() === now.get_year() &&
        moment.get_day_of_year() === now.get_day_of_year()) {
        return hour + ':' + minute;
    }
    const month = String(moment.get_month()).padStart(2, '0');
    const day = String(moment.get_day_of_month()).padStart(2, '0');
    return month + '-' + day + ' ' + hour + ':' + minute;
}

/**
 * Map a raw severity string to one of the known severity buckets.
 * @param {*} severity Raw severity from the broker payload.
 * @returns {string} One of 'error', 'warning', 'info'.
 */
function severityOf(severity) {
    const text = asString(severity, '', 32).toLowerCase();
    if (text === 'error' || text === 'critical' || text === 'fatal')
        return 'error';
    if (text === 'warning' || text === 'warn')
        return 'warning';
    return 'info';
}

/**
 * Choose the single-character marker shown in front of an event summary.
 * @param {*} severity Raw severity from the broker payload.
 * @returns {string} '!' for errors, '\u25B2' for warnings, '\u00B7' otherwise.
 */
function severityMarker(severity) {
    const bucket = severityOf(severity);
    if (bucket === 'error')
        return '!';
    if (bucket === 'warning')
        return '\u25B2';
    return '\u00B7';
}

/**
 * Choose the CSS class used to colour an event row.
 * @param {*} severity Raw severity from the broker payload.
 * @returns {string} 'agentos-event-error', 'agentos-event-warning' or ''.
 */
function severityClass(severity) {
    const bucket = severityOf(severity);
    if (bucket === 'error')
        return 'agentos-event-error';
    if (bucket === 'warning')
        return 'agentos-event-warning';
    return '';
}

/**
 * Render the top-bar label text for a task count, e.g. 'AgentOS 2'.
 * @param {number} count Number of active tasks.
 * @returns {string} '' when the count is not positive, else 'AgentOS <count>'.
 */
function formatCount(count) {
    const value = asNumber(count, 0);
    if (value <= 0)
        return '';
    return 'AgentOS ' + String(Math.floor(value));
}
