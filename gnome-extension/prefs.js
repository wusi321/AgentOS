// SPDX-License-Identifier: GPL-2.0-or-later
//
// AgentOS GNOME Shell extension - preferences window (GNOME 42/43/44, Gtk4/Adw).
//
// The window edits UI state only. It cannot read or write files, cannot run
// commands and never handles credentials: every control below maps 1:1 onto a
// key declared in schemas/org.gnome.shell.extensions.agentos.gschema.xml.

const Adw = imports.gi.Adw;
const Gio = imports.gi.Gio;
const Gtk = imports.gi.Gtk;

const ExtensionUtils = imports.misc.extensionUtils;
const logError = imports.misc.logger.logError;

/** Schema id of the extension settings. */
const SCHEMA_ID = 'org.gnome.shell.extensions.agentos';

/** Lower bound of `poll-interval-seconds`, mirroring the schema range. */
const POLL_MIN = 2;
/** Upper bound of `poll-interval-seconds`, mirroring the schema range. */
const POLL_MAX = 60;
/** Lower bound of `max-recent-events`, mirroring the schema range. */
const EVENTS_MIN = 1;
/** Upper bound of `max-recent-events`, mirroring the schema range. */
const EVENTS_MAX = 10;

/**
 * Add a row to an Adw preferences group across libadwaita versions.
 *
 * libadwaita 1.4 (GNOME 44) renamed `add()` to `append()` and kept `add()` as a
 * deprecated alias; libadwaita 1.1 (GNOME 42) only has `add()`. Trying the
 * GNOME 42 name first keeps one code path working on all three releases.
 * @param {Adw.PreferencesGroup} group Target group.
 * @param {Gtk.Widget} row Row to add.
 * @returns {void}
 */
function addRow(group, row) {
    if (typeof group.add === 'function')
        group.add(row);
    else
        group.append(row);
}

/**
 * Read the extension settings, preferring the supported ExtensionUtils path.
 * @returns {Gio.Settings|null} Settings instance, or null when the schema is
 *   not installed (the window then shows a read-only explanation).
 */
function openSettings() {
    try {
        return ExtensionUtils.getSettings(SCHEMA_ID);
    } catch (error) {
        logError(error, '[AgentOS] could not open settings ' + SCHEMA_ID);
        return null;
    }
}

/**
 * Build a spin row bound two-way to an unsigned integer setting.
 * @param {Gio.Settings} settings Extension settings.
 * @param {string} key Schema key name.
 * @param {string} title Row title.
 * @param {string} subtitle Row subtitle.
 * @param {number} lower Minimum value.
 * @param {number} upper Maximum value.
 * @returns {Adw.SpinRow} A row that writes straight through to GSettings.
 */
function buildSpinRow(settings, key, title, subtitle, lower, upper) {
    const row = new Adw.SpinRow({
        title: title,
        subtitle: subtitle,
        adjustment: new Gtk.Adjustment({
            lower: lower,
            upper: upper,
            step_increment: 1,
            page_increment: 5,
            value: settings.get_uint(key),
        }),
    });
    settings.bind(key, row, 'value', Gio.SettingsBindFlags.DEFAULT);
    return row;
}

/**
 * Build a switch row bound two-way to a boolean setting.
 * @param {Gio.Settings} settings Extension settings.
 * @param {string} key Schema key name.
 * @param {string} title Row title.
 * @param {string} subtitle Row subtitle.
 * @returns {Adw.SwitchRow} A row that writes straight through to GSettings.
 */
function buildSwitchRow(settings, key, title, subtitle) {
    const row = new Adw.SwitchRow({
        title: title,
        subtitle: subtitle,
    });
    settings.bind(key, row, 'active', Gio.SettingsBindFlags.DEFAULT);
    return row;
}

/**
 * Build the single preferences page holding every AgentOS setting.
 * @param {Gio.Settings|null} settings Extension settings.
 * @returns {Adw.PreferencesPage} Page with panel, notification and about groups.
 */
function buildPage(settings) {
    const page = new Adw.PreferencesPage({ title: 'AgentOS' });

    if (settings === null) {
        // The schema is missing: explain the situation instead of crashing the
        // preferences process.
        const brokenGroup = new Adw.PreferencesGroup({
            title: 'Settings unavailable',
            description: 'The GSettings schema ' + SCHEMA_ID + ' is not installed, so the extension has no configurable options. Reinstall the AgentOS package to restore it.',
        });
        const brokenRow = new Adw.ActionRow({
            title: 'Schema missing',
            subtitle: '/usr/share/glib-2.0/schemas/org.gnome.shell.extensions.agentos.gschema.xml',
        });
        addRow(brokenGroup, brokenRow);
        page.add(brokenGroup);
        return page;
    }

    const panelGroup = new Adw.PreferencesGroup({
        title: 'Top bar',
        description: 'How the AgentOS indicator appears in the GNOME Shell panel.',
    });
    addRow(panelGroup, buildSwitchRow(settings, 'show-task-count-in-panel',
        'Show active task count',
        'Display the number of queued, running and approval-waiting tasks next to the icon.'));
    addRow(panelGroup, buildSpinRow(settings, 'poll-interval-seconds',
        'Status poll interval',
        'Seconds between status requests to the AgentOS session broker.',
        POLL_MIN, POLL_MAX));

    const notificationGroup = new Adw.PreferencesGroup({
        title: 'Notifications',
        description: 'Notifications are rate limited to one every 30 seconds and never repeat the same task within 5 minutes. Approvals are only announced here; they are always decided in the AgentOS desktop application.',
    });
    addRow(notificationGroup, buildSwitchRow(settings, 'notify-on-task-failure',
        'Notify when a task fails',
        'Show one notification per failed task.'));
    addRow(notificationGroup, buildSwitchRow(settings, 'notify-on-approval',
        'Notify when a task needs approval',
        'Show one notification per task waiting for an approval decision.'));

    const menuGroup = new Adw.PreferencesGroup({
        title: 'Menu',
        description: 'Content of the AgentOS drop-down menu.',
    });
    addRow(menuGroup, buildSpinRow(settings, 'max-recent-events',
        'Recent activity entries',
        'How many recent events the menu lists; the broker sends at most 10.',
        EVENTS_MIN, EVENTS_MAX));

    const aboutGroup = new Adw.PreferencesGroup({
        title: 'About',
        description: 'This extension is a read-only UI client: it runs no shell commands, reads and writes no files, stores no credentials and never approves anything on your behalf.',
    });
    const brokerRow = new Adw.ActionRow({
        title: 'AgentOS session broker',
        subtitle: 'org.agentos.Session1 at /org/agentos/Session1 on the D-Bus session bus',
    });
    addRow(aboutGroup, brokerRow);
    const uuidRow = new Adw.ActionRow({
        title: 'Extension UUID',
        subtitle: 'agentos@agentos.dev',
    });
    addRow(aboutGroup, uuidRow);

    page.add(panelGroup);
    page.add(notificationGroup);
    page.add(menuGroup);
    page.add(aboutGroup);
    return page;
}

/**
 * GNOME 42+ entry point: fill the window the Shell already created.
 *
 * `fillPreferencesWindow()` was introduced with GNOME 42 and is the only
 * preferences hook defined here; defining `buildPrefsWidget()` as well would
 * make gnome-shell-extensions-app warn about both entry points being present.
 * @param {Adw.PreferencesWindow} window Window supplied by gnome-extensions-app.
 * @returns {void} Never throws; failures are logged instead of escaping.
 */
function fillPreferencesWindow(window) {
    if (window === undefined || window === null)
        return;
    try {
        window.add(buildPage(openSettings()));
    } catch (error) {
        logError(error, '[AgentOS] preferences window failed to build');
    }
}
