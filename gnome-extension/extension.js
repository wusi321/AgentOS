// SPDX-License-Identifier: GPL-2.0-or-later
//
// AgentOS GNOME Shell extension - top-bar indicator for the AgentOS agent-native
// desktop environment (GNOME Shell 42/43/44, classic GJS `imports` style).
//
// Scope and security contract (hard requirements, see README.md):
//   * This extension is a read-only UI client. It never runs a shell command
//     (no imports.misc.util / GLib.spawn / bash -c) and never opens a file.
//   * It stores no model API keys, no audit database and no other secret; its
//     only persistent state is the GSettings UI state declared in schemas/.
//   * It never approves anything. Approval decisions happen exclusively in the
//     AgentOS desktop application, which the extension merely asks the session
//     broker to open.
//   * Every D-Bus call is asynchronous and wrapped so that a missing, dead or
//     misbehaving service produces a visible non-fatal state instead of an
//     exception escaping into GNOME Shell.

const Gio = imports.gi.Gio;
const GLib = imports.gi.GLib;
const St = imports.gi.St;

const Main = imports.ui.main;
const MessageTray = imports.ui.messageTray;
const PanelMenu = imports.ui.panelMenu;
const PopupMenu = imports.ui.popupMenu;

const ExtensionUtils = imports.misc.extensionUtils;
const logError = imports.misc.logger.logError;

const AgentOSUtils = imports.utils;

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Session broker that owns the AgentOS per-user session. */
const SESSION_BUS_NAME = 'org.agentos.Session1';
/** Object path exported by the session broker. */
const SESSION_OBJECT_PATH = '/org/agentos/Session1';
/** Interface exported at SESSION_OBJECT_PATH. */
const SESSION_INTERFACE = 'org.agentos.Session1';

/** Fixed extension UUID; must match metadata.json and the schema path. */
const EXTENSION_UUID = 'agentos@agentos.dev';

/** Primary top-bar icon, with a fallback for icon themes that lack it. */
const ICON_NAME = 'utilities-system-monitor-symbolic';
/** Fallback top-bar icon. */
const ICON_FALLBACK_NAME = 'dialog-information-symbolic';

/** Timeout for the cheap OpenDesktop()/Subscribe() calls, in milliseconds. */
const SHORT_CALL_TIMEOUT_MS = 4000;
/** Timeout for GetStatus(), in milliseconds. */
const STATUS_CALL_TIMEOUT_MS = 8000;
/** Timeout for the best-effort Unsubscribe() during disable(), in ms. */
const UNSUBSCRIBE_TIMEOUT_MS = 2000;

/** Default poll interval, used only until the schema value is read. */
const DEFAULT_POLL_SECONDS = 5;
/** Minimum accepted poll interval, mirrors the schema range. */
const MIN_POLL_SECONDS = 2;
/** Maximum accepted poll interval, mirrors the schema range. */
const MAX_POLL_SECONDS = 60;

/** Minimum spacing between two desktop notifications, in seconds. */
const NOTIFICATION_MIN_GAP_SECONDS = 30;
/** How long one (event_type, task_id) signature stays suppressed, in seconds. */
const NOTIFICATION_SIGNATURE_SUPPRESS_SECONDS = 300;

/** Task states that count as "active" in the top-bar counter. */
const ACTIVE_TASK_STATES = ['QUEUED', 'RUNNING', 'WAITING_APPROVAL'];
/** Every task state the broker may report; unknown states are ignored. */
const KNOWN_TASK_STATES = ['QUEUED', 'RUNNING', 'WAITING_APPROVAL', 'SUCCEEDED',
    'FAILED', 'CANCELLED'];
/** Recent events are rendered newest-first; the broker caps them at 10. */
const MAX_BROKER_EVENTS = 10;

// ---------------------------------------------------------------------------
// Model helpers
// ---------------------------------------------------------------------------

/**
 * Read one task-state counter out of a broker `tasks` object.
 * @param {Object} tasks Task-state map, already normalized to an object.
 * @param {string} state One of KNOWN_TASK_STATES.
 * @returns {number} Non-negative integer count, 0 when absent or invalid.
 */
function taskCountOf(tasks, state) {
    const value = AgentOSUtils.asNumber(tasks[state], 0);
    if (value <= 0)
        return 0;
    return Math.floor(value);
}

/**
 * Counts of the task states shown as "active" in the panel.
 * @param {Object} model Normalized status model.
 * @returns {number} Sum of QUEUED + RUNNING + WAITING_APPROVAL.
 */
function activeTaskCount(model) {
    const tasks = AgentOSUtils.asObject(model.tasks, {});
    let total = 0;
    for (let i = 0; i < ACTIVE_TASK_STATES.length; i++)
        total += taskCountOf(tasks, ACTIVE_TASK_STATES[i]);
    return total;
}

/**
 * Coerce one task entry from the broker into a safe shape.
 * @param {*} item Untrusted entry from the `active_tasks` array.
 * @returns {{task_id: string, title: string, state: string}} Plain data.
 */
function normalizeActiveTask(item) {
    const raw = AgentOSUtils.asObject(item, {});
    let state = AgentOSUtils.asString(raw.state, 'UNKNOWN', 32).toUpperCase();
    if (KNOWN_TASK_STATES.indexOf(state) < 0)
        state = 'UNKNOWN';
    return {
        task_id: AgentOSUtils.asString(raw.task_id, '', 128),
        title: AgentOSUtils.asString(raw.title, '', 200),
        state: state,
    };
}

/**
 * Coerce one pending-approval entry from the broker into a safe shape.
 * @param {*} item Untrusted entry from the `pending_approvals` array.
 * @returns {{approval_id: string, operation: string, target: string, reason: string}}
 */
function normalizePendingApproval(item) {
    const raw = AgentOSUtils.asObject(item, {});
    return {
        approval_id: AgentOSUtils.asString(raw.approval_id, '', 128),
        operation: AgentOSUtils.asString(raw.operation, '', 200),
        target: AgentOSUtils.asString(raw.target, '', 200),
        reason: AgentOSUtils.asString(raw.reason, '', 400),
    };
}

/**
 * Coerce one recent-event entry from the broker into a safe shape.
 * @param {*} item Untrusted entry from the `recent_events` array.
 * @returns {{event_type: string, task_id: string, severity: string, summary: string,
 *            title: string, state: string, timestamp: number}}
 */
function normalizeRecentEvent(item) {
    const raw = AgentOSUtils.asObject(item, {});
    return {
        event_type: AgentOSUtils.asString(raw.event_type, '', 120),
        task_id: AgentOSUtils.asString(raw.task_id, '', 128),
        severity: AgentOSUtils.asString(raw.severity, 'info', 32).toLowerCase(),
        summary: AgentOSUtils.asString(raw.summary, '', 400),
        title: AgentOSUtils.asString(raw.title, '', 200),
        state: AgentOSUtils.asString(raw.state, '', 32).toUpperCase(),
        timestamp: AgentOSUtils.asNumber(raw.timestamp, 0),
    };
}

/**
 * Build the "service unavailable" model used before the first successful
 * status reply and after any broker failure.
 * @param {string} detail Human-readable cause, shown in the menu header.
 * @returns {Object} A fully normalized status model with empty collections.
 */
function unavailableModel(detail) {
    return {
        available: false,
        state: 'unavailable',
        detail: AgentOSUtils.asString(detail, 'no response from the session broker', 200),
        core_available: false,
        runtime_id: '',
        runtime_available: false,
        runtime_detail: '',
        tasks: {},
        active_tasks: [],
        pending_approvals: [],
        recent_events: [],
    };
}

/**
 * Normalize an arbitrary JSON payload from GetStatus() into a status model.
 * Missing, wrongly typed or hostile fields are replaced with safe defaults.
 * @param {Object} payload Parsed JSON document from the broker.
 * @returns {Object} A fully normalized status model with `available: true`.
 */
function normalizeStatus(payload) {
    const raw = AgentOSUtils.asObject(payload, {});

    const tasks = {};
    const rawTasks = AgentOSUtils.asObject(raw.tasks, {});
    for (let i = 0; i < KNOWN_TASK_STATES.length; i++) {
        const state = KNOWN_TASK_STATES[i];
        tasks[state] = taskCountOf(rawTasks, state);
    }

    const activeTasks = [];
    const rawActive = AgentOSUtils.asArray(raw.active_tasks);
    for (let i = 0; i < rawActive.length; i++)
        activeTasks.push(normalizeActiveTask(rawActive[i]));

    const pendingApprovals = [];
    const rawPending = AgentOSUtils.asArray(raw.pending_approvals);
    for (let i = 0; i < rawPending.length; i++)
        pendingApprovals.push(normalizePendingApproval(rawPending[i]));

    const recentEvents = [];
    const rawEvents = AgentOSUtils.asArray(raw.recent_events);
    const eventLimit = Math.min(rawEvents.length, MAX_BROKER_EVENTS);
    for (let i = 0; i < eventLimit; i++)
        recentEvents.push(normalizeRecentEvent(rawEvents[i]));

    return {
        available: true,
        state: 'ok',
        detail: '',
        core_available: AgentOSUtils.asBoolean(raw.core_available, false),
        runtime_id: AgentOSUtils.asString(raw.runtime_id, '', 64),
        runtime_available: AgentOSUtils.asBoolean(raw.runtime_available, false),
        runtime_detail: AgentOSUtils.asString(raw.runtime_detail, '', 200),
        tasks: tasks,
        active_tasks: activeTasks,
        pending_approvals: pendingApprovals,
        recent_events: recentEvents,
    };
}

/**
 * Build the one-line service description shown in the menu header.
 * @param {Object} model Normalized status model.
 * @returns {string} Text such as 'Core ok \u00b7 runtime mock ready'.
 */
function describeService(model) {
    if (!model.available)
        return 'AgentOS service unavailable';

    if (!model.core_available)
        return 'AgentOS Core offline';

    const parts = ['AgentOS Core ok'];
    if (model.runtime_available) {
        const runtime = model.runtime_id.length > 0 ? model.runtime_id : 'runtime';
        parts.push('runtime ' + runtime + ' ready');
    } else {
        parts.push('runtime unavailable');
    }
    return parts.join(' \u00b7 ');
}

/**
 * Build the secondary header line: counts plus the first useful detail string.
 * @param {Object} model Normalized status model.
 * @returns {string} Text such as '1 running \u00b7 1 awaiting approval'.
 */
function describeCounts(model) {
    if (!model.available)
        return AgentOSUtils.truncate(model.detail, 90);

    const tasks = AgentOSUtils.asObject(model.tasks, {});
    const running = taskCountOf(tasks, 'RUNNING');
    const queued = taskCountOf(tasks, 'QUEUED');
    const approvals = taskCountOf(tasks, 'WAITING_APPROVAL');
    const failed = taskCountOf(tasks, 'FAILED');

    const parts = [];
    if (running > 0)
        parts.push(String(running) + ' running');
    if (queued > 0)
        parts.push(String(queued) + ' queued');
    if (approvals > 0)
        parts.push(String(approvals) + ' awaiting approval');
    if (failed > 0)
        parts.push(String(failed) + ' failed');
    if (parts.length === 0)
        return 'No active tasks';
    return parts.join(' \u00b7 ');
}

/**
 * Build the human line for one pending approval.
 * @param {Object} approval Normalized approval entry.
 * @returns {string} Text such as 'system.service.restart on cups.service'.
 */
function describeApproval(approval) {
    const operation = approval.operation.length > 0 ? approval.operation : 'operation';
    const target = approval.target.length > 0 ? approval.target : 'unknown target';
    return AgentOSUtils.truncate(operation + ' on ' + target, 80);
}

/**
 * Build the human line for one recent event.
 * @param {Object} event Normalized event entry.
 * @returns {string} Marker-prefixed summary, never empty.
 */
function describeEvent(event) {
    const marker = AgentOSUtils.severityMarker(event.severity);
    let text = event.summary;
    if (text.length === 0)
        text = event.title;
    if (text.length === 0)
        text = event.event_type;
    if (text.length === 0)
        text = 'AgentOS event';
    return AgentOSUtils.truncate(marker + ' ' + text, 90);
}

// ---------------------------------------------------------------------------
// Session broker client
// ---------------------------------------------------------------------------

/**
 * Asynchronous, failure-tolerant client for the org.agentos.Session1 broker.
 *
 * Contract: every method returns immediately and never throws. Results are
 * delivered through callbacks; a missing or dead service is reported as a
 * failed status instead of an exception. At most one GetStatus() request is in
 * flight at a time and overlapping refresh requests are coalesced into one.
 */
const BrokerClient = class BrokerClient {

    /**
     * @param {Object} handlers Callbacks:
     *   `onStatus(model)` receives a normalized status model,
     *   `onEvent(event, token)` receives a normalized event object and token.
     */
    constructor(handlers) {
        this._handlers = handlers === undefined ? {} : handlers;
        this._subscriptionId = 0;
        this._token = '';
        this._inFlight = false;
        this._refreshPending = false;
        this._started = false;
    }

    /**
     * Register the D-Bus signal subscription and ask the broker for a token.
     * Called from enable(); safe to call when the broker is absent.
     * @returns {void}
     */
    start() {
        if (this._started)
            return;
        this._started = true;
        // The subscription is registered on the bus connection, not on the
        // service, so it must be established before (and independently of) the
        // Subscribe() call. GNOME 42+ API.
        this._subscribeToSignals();
        this._callSubscribe();
    }

    /**
     * Drop the signal subscription and tell the broker to forget the token.
     * Called from disable(); never throws and never blocks.
     * @returns {void}
     */
    stop() {
        this._started = false;
        this._inFlight = false;
        this._refreshPending = false;

        if (this._subscriptionId !== 0) {
            try {
                Gio.DBus.session.signal_unsubscribe(this._subscriptionId);
            } catch (error) {
                log('[AgentOS] signal_unsubscribe failed: ' + error);
            }
            this._subscriptionId = 0;
        }

        const token = this._token;
        this._token = '';
        if (token.length === 0)
            return;

        // Best-effort notification: the broker also expires idle tokens, so a
        // failure here is not worth surfacing to the user.
        try {
            Gio.DBus.session.call(SESSION_BUS_NAME, SESSION_OBJECT_PATH, SESSION_INTERFACE,
                'Unsubscribe', new GLib.Variant('(s)', [token]), null,
                Gio.DBusCallFlags.NONE, UNSUBSCRIBE_TIMEOUT_MS, null, null);
        } catch (error) {
            log('[AgentOS] Unsubscribe call failed: ' + error);
        }
    }

    /**
     * The subscription token currently believed to be valid.
     * @returns {string} Token, or '' when not subscribed.
     */
    get token() {
        return this._token;
    }

    /**
     * Ask the broker to launch or focus the AgentOS desktop application.
     * The extension performs no action of its own here; it only relays intent.
     * @returns {void}
     */
    openDesktop() {
        this._callSimple('OpenDesktop', SHORT_CALL_TIMEOUT_MS);
    }

    /**
     * Request a status snapshot. At most one GetStatus() call is in flight.
     * @param {boolean} force True for event-driven refreshes (a new snapshot is
     *   queued as soon as the current one settles). False for periodic polls,
     *   which are dropped while a call is in flight so a slow broker can never
     *   build a backlog of polls.
     * @returns {void}
     */
    requestStatus(force) {
        if (this._inFlight) {
            if (force)
                this._refreshPending = true;
            return;
        }
        this._inFlight = true;
        let completed = false;
        const finish = () => {
            if (completed)
                return;
            completed = true;
            this._inFlight = false;
            if (this._refreshPending) {
                this._refreshPending = false;
                this.requestStatus(true);
            }
        };
        this._callGetStatus(finish);
    }

    /**
     * Register the `Event` signal handler on the session bus.
     * @returns {void} Never throws; a failed subscription is logged and the
     *   extension keeps working through polling alone.
     */
    _subscribeToSignals() {
        try {
            this._subscriptionId = Gio.DBus.session.signal_subscribe(
                SESSION_BUS_NAME,
                SESSION_INTERFACE,
                'Event',
                SESSION_OBJECT_PATH,
                null,
                Gio.DBusSignalFlags.NONE,
                (connection, senderName, objectPath, interfaceName, signalName, parameters) => {
                    this._handleSignal(parameters);
                });
        } catch (error) {
            this._subscriptionId = 0;
            logError(error, '[AgentOS] could not subscribe to session broker events');
        }
    }

    /**
     * Handle one `Event(s token, s event_json)` signal.
     * @param {GLib.Variant} parameters Signal parameters, expected to be `(ss)`.
     * @returns {void} Never throws.
     */
    _handleSignal(parameters) {
        try {
            // The token filter is a security-relevant check: events addressed to
            // another subscriber must be ignored outright.
            if (this._token.length === 0)
                return;
            if (parameters === null || parameters === undefined)
                return;

            const unpacked = parameters.deep_unpack();
            if (!Array.isArray(unpacked) || unpacked.length < 2)
                return;
            const token = AgentOSUtils.asString(unpacked[0], '', 256);
            if (token !== this._token)
                return;

            const event = AgentOSUtils.parseJsonObject(unpacked[1]);
            if (event === null)
                return;

            if (typeof this._handlers.onEvent === 'function')
                this._handlers.onEvent(normalizeRecentEvent(event), this._token);
        } catch (error) {
            // A callback must never let a failure escape into the Shell.
            logError(error, '[AgentOS] failed to handle a session broker event');
        }
    }

    /**
     * Call Subscribe() and remember the returned token.
     * @returns {void}
     */
    _callSubscribe() {
        this._callWithReply('Subscribe', null, SHORT_CALL_TIMEOUT_MS, {
            onReply: (value) => {
                const unpacked = value.deep_unpack();
                const token = Array.isArray(unpacked)
                    ? AgentOSUtils.asString(unpacked[0], '', 256)
                    : '';
                this._token = token;
                if (token.length === 0) {
                    log('[AgentOS] session broker returned an empty subscription token');
                    return;
                }
                // Subscribe() succeeded: the next status snapshot may already be
                // useful, so ask for one right away.
                this.requestStatus(true);
            },
            onError: (error) => {
                // Not fatal: polling keeps the panel current.
                log('[AgentOS] Subscribe() failed: ' + error.message);
            },
        });
    }

    /**
     * Fire-and-forget call for methods without interesting replies.
     * @param {string} methodName D-Bus method on SESSION_INTERFACE.
     * @param {number} timeoutMs Call timeout in milliseconds.
     * @returns {void}
     */
    _callSimple(methodName, timeoutMs) {
        this._callWithReply(methodName, null, timeoutMs, {
            onReply: () => {},
            onError: (error) => {
                // Expected whenever the broker is not running; the header already
                // says so, hence a console note only.
                log('[AgentOS] ' + methodName + '() failed: ' + error.message);
            },
        });
    }

    /**
     * Call GetStatus(), parse the payload and deliver a normalized model.
     * @param {Function} finish Called exactly once, after the model is handed to
     *   the `onStatus` handler. Used to release the in-flight guard.
     * @returns {void}
     */
    _callGetStatus(finish) {
        this._callWithReply('GetStatus', null, STATUS_CALL_TIMEOUT_MS, {
            onReply: (value) => {
                const unpacked = value.deep_unpack();
                const state = Array.isArray(unpacked)
                    ? AgentOSUtils.asString(unpacked[0], 'error', 32)
                    : 'error';
                const payloadText = Array.isArray(unpacked) && unpacked.length > 1
                    ? unpacked[1]
                    : '';

                if (state !== 'ok') {
                    // 'unavailable' and 'error' are normal, expected broker
                    // states: the Core service or the runtime is simply down.
                    this._deliverStatus(unavailableModel(
                        state === 'unavailable'
                            ? 'AgentOS Core is not running'
                            : 'AgentOS session broker reported an error'), finish);
                    return;
                }

                const payload = AgentOSUtils.parseJsonObject(payloadText);
                if (payload === null) {
                    this._deliverStatus(unavailableModel(
                        'AgentOS session broker sent an unreadable payload'), finish);
                    return;
                }
                this._deliverStatus(normalizeStatus(payload), finish);
            },
            onError: (error) => {
                const detail = error.name === 'Gio.IOErrorEnum'
                    ? 'AgentOS session broker is not reachable'
                    : 'AgentOS session broker is not reachable (' + error.message + ')';
                this._deliverStatus(unavailableModel(detail), finish);
            },
        });
    }

    /**
     * Hand a model to the status handler, then release the in-flight guard.
     * @param {Object} model Normalized status model.
     * @param {Function} finish Completion callback from requestStatus().
     * @returns {void}
     */
    _deliverStatus(model, finish) {
        try {
            if (typeof this._handlers.onStatus === 'function')
                this._handlers.onStatus(model);
        } catch (error) {
            logError(error, '[AgentOS] failed to render a session broker status');
        }
        try {
            if (typeof finish === 'function')
                finish();
        } catch (error) {
            logError(error, '[AgentOS] failed to finish a status request');
        }
    }

    /**
     * Perform one asynchronous D-Bus method call with full error containment.
     * @param {string} methodName Method name on SESSION_INTERFACE.
     * @param {GLib.Variant|null} parameters Input parameters, or null for none.
     * @param {number} timeoutMs Call timeout in milliseconds.
     * @param {{onReply: Function, onError: Function}} callbacks `onReply` gets the
     *   GLib.Variant reply tuple; `onError` gets an Error-like object.
     * @returns {void} Never throws, even if the bus connection is unusable.
     */
    _callWithReply(methodName, parameters, timeoutMs, callbacks) {
        const safeReply = (connection, result) => {
            let value = null;
            try {
                value = connection.call_finish(result);
            } catch (error) {
                this._reportCallError(methodName, error, callbacks);
                return;
            }
            try {
                callbacks.onReply(value);
            } catch (error) {
                logError(error, '[AgentOS] ' + methodName + '() reply handling failed');
            }
        };

        try {
            Gio.DBus.session.call(
                SESSION_BUS_NAME,
                SESSION_OBJECT_PATH,
                SESSION_INTERFACE,
                methodName,
                parameters,
                null,
                Gio.DBusCallFlags.NONE,
                timeoutMs,
                null,
                safeReply);
        } catch (error) {
            // Gio.DBus.session is null when no session bus is running; the
            // synchronous path can also throw on invalid arguments.
            this._reportCallError(methodName, error, callbacks);
        }
    }

    /**
     * Route a failed call to the caller's error handler inside a guard.
     * @param {string} methodName Method that failed, used for logging.
     * @param {Error} error Failure raised by GIO or thrown synchronously.
     * @param {{onError: Function}} callbacks Caller callbacks.
     * @returns {void}
     */
    _reportCallError(methodName, error, callbacks) {
        const failure = error === null || error === undefined
            ? new Error(methodName + '() failed')
            : error;
        try {
            if (typeof callbacks.onError === 'function') {
                callbacks.onError(failure);
                return;
            }
        } catch (handlerError) {
            logError(handlerError, '[AgentOS] ' + methodName + '() error handling failed');
            return;
        }
        log('[AgentOS] ' + methodName + '() failed: ' + failure.message);
    }
};

// ---------------------------------------------------------------------------
// Notification throttling
// ---------------------------------------------------------------------------

/**
 * Rate limiter for desktop notifications.
 *
 * Contract: `shouldNotify(signature, nowSeconds)` returns true at most once per
 * signature, never twice within NOTIFICATION_SIGNATURE_SUPPRESS_SECONDS, and
 * never more often than once every NOTIFICATION_MIN_GAP_SECONDS across all
 * signatures. State is kept in memory only; nothing is written to disk.
 */
const NotificationGate = class NotificationGate {

    /** @param {number} nowSeconds Current monotonic time in seconds. */
    constructor(nowSeconds) {
        this._lastNotification = nowSeconds;
        this._signatures = {};
    }

    /**
     * Decide whether one notification may be shown and record it.
     *
     * Three rules are applied in order: the global 30 second gap, the 5 minute
     * per-signature suppression, then the record update.
     * @param {string} signature Stable signature such as 'failure:task.failed:abc'.
     * @param {number} nowSeconds Current monotonic time in seconds.
     * @returns {boolean} True when the caller may show the notification.
     */
    shouldNotify(signature, nowSeconds) {
        if (typeof signature !== 'string' || signature.length === 0)
            return false;

        if ((nowSeconds - this._lastNotification) < NOTIFICATION_MIN_GAP_SECONDS)
            return false;

        const seenAt = this._signatures[signature];
        if (typeof seenAt === 'number' &&
            (nowSeconds - seenAt) < NOTIFICATION_SIGNATURE_SUPPRESS_SECONDS)
            return false;

        this._signatures[signature] = nowSeconds;
        this._lastNotification = nowSeconds;
        return true;
    }
};

// ---------------------------------------------------------------------------
// CSS class helpers
// ---------------------------------------------------------------------------

/**
 * Add a CSS class to an actor, tolerating themes and Shell revisions that lack
 * the exact St.Widget method.
 * @param {St.Widget} actor Actor to style.
 * @param {string} className CSS class name.
 * @returns {void} Never throws.
 */
function addStyleClass(actor, className) {
    if (actor === null || actor === undefined || className.length === 0)
        return;
    try {
        if (typeof actor.add_style_class_name === 'function')
            actor.add_style_class_name(className);
    } catch (error) {
        log('[AgentOS] could not add CSS class ' + className + ': ' + error.message);
    }
}

/**
 * Remove a CSS class from an actor. Unknown classes are ignored by Clutter, but
 * the call is still guarded so a theme quirk can never break a refresh.
 * @param {St.Widget} actor Actor to restyle.
 * @param {string} className CSS class name.
 * @returns {void} Never throws.
 */
function removeStyleClass(actor, className) {
    if (actor === null || actor === undefined || className.length === 0)
        return;
    try {
        if (typeof actor.remove_style_class_name === 'function')
            actor.remove_style_class_name(className);
    } catch (error) {
        log('[AgentOS] could not remove CSS class ' + className + ': ' + error.message);
    }
}

// ---------------------------------------------------------------------------
// Panel indicator
// ---------------------------------------------------------------------------

/**
 * Top-bar indicator for AgentOS.
 *
 * Contract: construction never throws and never blocks on D-Bus; the indicator
 * is visible immediately with an "unavailable" state and upgrades itself as soon
 * as the session broker answers. `destroy()` releases every actor, GSettings
 * signal and timeout source the indicator created.
 */
const AgentOSIndicator = class AgentOSIndicator extends PanelMenu.Button {

    /**
     * @param {Gio.Settings} settings Extension GSettings instance (UI state only).
     */
    constructor(settings) {
        // 0.0 = fully reactive (GNOME 40+ semantics); no `dontCreateMenu` so the
        // popup menu is always available.
        super(0.0, 'AgentOS', false);

        this._settings = settings;
        this._destroyed = false;

        // Bound handlers are stored so disable() can disconnect exactly what
        // enable() connected.
        this._settingsChangedId = settings.connect('changed::poll-interval-seconds',
            () => this._onPollIntervalChanged());
        this._client = new BrokerClient({
            onStatus: (model) => this._onStatus(model),
            onEvent: (event) => this._onEvent(event),
        });

        this._timeoutId = 0;
        this._pollSeconds = DEFAULT_POLL_SECONDS;
        // The gate is seeded only when it is first used (see _showNotification),
        // so its timestamp comes from the same clock reading as the first check.
        this._gate = null;
        this._loggedFirstSuccess = false;
        this._lastModel = unavailableModel('waiting for the AgentOS session broker');

        // Reusable menu rows, created once and updated in place so that enable/
        // disable cycles do not leak actors.
        this._headerStatusItem = new PopupMenu.PopupMenuItem('AgentOS service unavailable');
        this._headerStatusItem.setSensitive(false);
        this._headerDetailItem = new PopupMenu.PopupMenuItem('');
        this._headerDetailItem.setSensitive(false);
        this._openDesktopItem = new PopupMenu.PopupMenuItem('Open AgentOS');
        this._openDesktopItem.connect('activate', () => this._client.openDesktop());

        this._activeSection = new PopupMenu.PopupMenuSection();
        this._activeMenu = new PopupMenu.PopupSubMenuMenuItem('Active tasks', true);
        this._activeMenu.menu.addMenuItem(this._activeSection);

        this._approvalSection = new PopupMenu.PopupMenuSection();
        this._approvalMenu = new PopupMenu.PopupSubMenuMenuItem('Waiting for approval', true);
        this._approvalMenu.menu.addMenuItem(this._approvalSection);

        this._eventSection = new PopupMenu.PopupMenuSection();
        this._eventMenu = new PopupMenu.PopupSubMenuMenuItem('Recent activity', true);
        this._eventMenu.menu.addMenuItem(this._eventSection);

        // Fixed, non-empty submenu children: dynamic rows are added to the
        // sections above, so the popup never has an empty submenu.
        this.menu.addMenuItem(this._headerStatusItem);
        this.menu.addMenuItem(this._headerDetailItem);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addMenuItem(this._openDesktopItem);
        this.menu.addMenuItem(this._activeMenu);
        this.menu.addMenuItem(this._approvalMenu);
        this.menu.addMenuItem(this._eventMenu);

        this._buildButtonContent();
        this._renderMenu(this._lastModel);
        this._updateButton(this._lastModel);
    }

    /**
     * Assemble the top-bar box: symbolic icon plus optional count label.
     * @returns {void}
     */
    _buildButtonContent() {
        // St.Bin + St.BoxLayout works on every panel implementation in GNOME
        // 42-44 without relying on the internal structure of PanelMenu.Button.
        this._box = new St.Bin({
            style_class: 'panel-status-menu-box',
            x_expand: true,
            y_expand: true,
            x_align: St.Align.MIDDLE,
            y_align: St.Align.MIDDLE,
        });

        this._layout = new St.BoxLayout({
            vertical: false,
            style_class: 'agentos-panel-box',
        });

        this._icon = new St.Icon({
            // GNOME 42+ St.Icon: icon_name plus fallback_icon_name, which the
            // Shell uses when the primary symbolic icon is missing from the
            // active icon theme.
            icon_name: ICON_NAME,
            fallback_icon_name: ICON_FALLBACK_NAME,
            style_class: 'system-status-icon',
        });

        this._label = new St.Label({
            text: '',
            y_align: St.Align.MIDDLE,
            style_class: 'agentos-panel-label',
            visible: false,
        });

        this._layout.add_child(this._icon);
        this._layout.add_child(this._label);
        // Clutter.BoxLayout.spacing (GNOME 42+); widths are driven by the theme.
        this._layout.spacing = 0;
        this._box.set_child(this._layout);
        this.add_child(this._box);
    }

    /**
     * Apply a fresh status model to the indicator.
     * @param {Object} model Normalized status model from the broker client.
     * @returns {void} Never throws: rendering problems are contained here.
     */
    _onStatus(model) {
        if (this._destroyed)
            return;
        try {
            this._lastModel = model;
            this._renderMenu(model);
            this._updateButton(model);
            this._noteNotifications(model);
            if (model.available && !this._loggedFirstSuccess) {
                this._loggedFirstSuccess = true;
                log('[AgentOS] session broker connected');
            }
        } catch (error) {
            logError(error, '[AgentOS] failed to apply a status update');
        }
    }

    /**
     * Handle one broker Event signal: refresh at once and raise a notification
     * when the event is a failure or an approval request.
     * @param {Object} event Normalized event entry.
     * @returns {void} Never throws.
     */
    _onEvent(event) {
        if (this._destroyed)
            return;
        try {
            const eventType = event.event_type.toLowerCase();
            const isFailure = eventType === 'task.failed' ||
                eventType.indexOf('failed') >= 0;
            const isApproval = eventType.indexOf('approval') >= 0 ||
                eventType.indexOf('waiting') >= 0;

            if (isFailure && this._notificationsEnabled('notify-on-task-failure')) {
                const signature = 'failure:' + eventType + ':' + event.task_id;
                this._showNotification('AgentOS task failed',
                    describeEvent(event), signature);
            } else if (isApproval && this._notificationsEnabled('notify-on-approval')) {
                const signature = 'approval:' + eventType + ':' + event.task_id;
                this._showNotification('AgentOS needs your approval',
                    describeEvent(event), signature);
            }

            // The event changed the world: refresh immediately instead of
            // waiting for the next poll tick.
            this._client.requestStatus(true);
        } catch (error) {
            logError(error, '[AgentOS] failed to handle an event notification');
        }
    }

    /**
     * Raise notifications derived from a status snapshot. This is what makes
     * approvals visible even when no signal is delivered.
     * @param {Object} model Normalized status model.
     * @returns {void}
     */
    _noteNotifications(model) {
        if (!model.available)
            return;

        if (this._notificationsEnabled('notify-on-approval')) {
            for (let i = 0; i < model.pending_approvals.length; i++) {
                const approval = model.pending_approvals[i];
                // Prefer the stable approval id; fall back to the operation and
                // target so an id-less payload is still deduplicated.
                const identity = approval.approval_id.length > 0
                    ? approval.approval_id
                    : (approval.operation + '/' + approval.target);
                this._showNotification('AgentOS needs your approval',
                    describeApproval(approval), 'approval-pending:' + identity);
            }
        }

        // Recent failures are announced when they arrive as signals; the status
        // payload repeats up to ten of them, so only the newest failure is
        // considered here and the signature map suppresses duplicates.
        if (this._notificationsEnabled('notify-on-task-failure')) {
            for (let j = 0; j < model.recent_events.length; j++) {
                const event = model.recent_events[j];
                if (AgentOSUtils.severityOf(event.severity) !== 'error')
                    continue;
                const signature = 'recent-failure:' + event.event_type + ':' + event.task_id;
                this._showNotification('AgentOS task failed', describeEvent(event), signature);
                break;
            }
        }
    }

    /**
     * Read one notification toggle defensively.
     * @param {string} key GSettings key name.
     * @returns {boolean} True when notifications of that kind are enabled.
     */
    _notificationsEnabled(key) {
        try {
            return this._settings.get_boolean(key);
        } catch (error) {
            // A missing key (for example a stale schema) must not disable the UI.
            log('[AgentOS] could not read setting ' + key + ': ' + error.message);
            return true;
        }
    }

    /**
     * Show a desktop notification through a MessageTray source, rate limited.
     * @param {string} title Notification title.
     * @param {string} body Notification body.
     * @param {string} signature Deduplication signature for this notification.
     * @returns {void} Never throws; notification failures are not user-visible.
     */
    _showNotification(title, body, signature) {
        if (this._destroyed)
            return;

        const now = this._monotonicSeconds();
        // Create the rate limiter on first use so its reference timestamp is
        // taken from the very clock reading it is compared against.
        if (this._gate === null)
            this._gate = new NotificationGate(now);
        if (!this._gate.shouldNotify(signature, now))
            return;

        try {
            const source = this._obtainNotificationSource();
            if (source === null)
                return;

            // GNOME 42/43 signature: (title, body). GNOME 44 added optional
            // parameters, which a GNOME 42/43 Shell would reject, so only the
            // two-argument form is used here.
            source.showNotification(new MessageTray.Notification(source, title, body));
        } catch (error) {
            logError(error, '[AgentOS] could not show a desktop notification');
        }
    }

    /**
     * Lazily create (or reuse) the MessageTray source backing our notifications.
     *
     * The source is cached at module level for the lifetime of the Shell session:
     * it belongs to the message tray and survives extension reloads, so creating
     * a fresh one per enable() would leak sources or, on GNOME 42, try to add the
     * same source twice.
     * @returns {Object|null} A MessageTray source, or null when unavailable.
     */
    _obtainNotificationSource() {
        if (_sharedNotificationSource !== null)
            return _sharedNotificationSource;

        const tray = Main.messageTray;
        if (tray === undefined || tray === null)
            return null;

        let source = null;

        // GNOME 43+ registers a shared system source so notifications survive a
        // session lock and are not tied to an application window.
        if (typeof MessageTray.getSystemSource === 'function') {
            source = MessageTray.getSystemSource();
        } else {
            // GNOME 42: build a plain source and keep it non-transient so it
            // does not vanish with an application window.
            source = new MessageTray.Source('AgentOS', ICON_NAME);
            if (typeof source.set_transient === 'function')
                source.set_transient(false);
            tray.addSource(source);
        }

        if (source === undefined || source === null)
            return null;

        _sharedNotificationSource = source;
        return source;
    }

    /**
     * Update the top-bar icon/label and the menu header rows.
     * @param {Object} model Normalized status model.
     * @returns {void}
     */
    _updateButton(model) {
        this._headerStatusItem.label.text = describeService(model);
        removeStyleClass(this._headerStatusItem, 'agentos-status-ok');
        removeStyleClass(this._headerStatusItem, 'agentos-status-bad');
        if (model.available && model.runtime_available)
            addStyleClass(this._headerStatusItem, 'agentos-status-ok');
        else
            addStyleClass(this._headerStatusItem, 'agentos-status-bad');

        this._headerDetailItem.label.text = describeCounts(model);

        let showCount = true;
        try {
            showCount = this._settings.get_boolean('show-task-count-in-panel');
        } catch (error) {
            log('[AgentOS] could not read show-task-count-in-panel: ' + error.message);
        }

        const count = activeTaskCount(model);
        const text = showCount ? AgentOSUtils.formatCount(count) : '';
        this._label.text = text;
        // An empty label is deactivated so the indicator collapses to the icon;
        // St.BoxLayout.spacing then no longer adds a gap (GNOME 42+).
        this._label.visible = text.length > 0;
        this._layout.spacing = text.length > 0 ? 4 : 0;
    }

    /**
     * Rebuild the three dynamic submenus from a status model.
     * @param {Object} model Normalized status model.
     * @returns {void}
     */
    _renderMenu(model) {
        this._renderMenuSection(this._activeSection, () => this._buildActiveTaskRows(model));
        this._renderMenuSection(this._approvalSection,
            () => this._buildApprovalRows(model));
        this._renderMenuSection(this._eventSection, () => this._buildEventRows(model));
    }

    /**
     * Replace the contents of one menu section.
     * @param {PopupMenu.PopupMenuSection} section Section to refill.
     * @param {Function} builder Returns the array of fresh menu items to add.
     * @returns {void}
     */
    _renderMenuSection(section, builder) {
        // `removeAll()` exists on PopupMenuBase in GNOME 42+ and destroys the
        // child items, so dynamic rows cannot accumulate across refreshes.
        section.removeAll();
        const rows = builder();
        for (let i = 0; i < rows.length; i++)
            section.addMenuItem(rows[i]);
    }

    /**
     * Build the rows for the "Active tasks" submenu.
     * @param {Object} model Normalized status model.
     * @returns {Array} Menu items, always at least one.
     */
    _buildActiveTaskRows(model) {
        if (!model.available)
            return [this._placeholderRow('AgentOS service unavailable')];
        if (model.active_tasks.length === 0)
            return [this._placeholderRow('No active tasks')];

        const rows = [];
        for (let i = 0; i < model.active_tasks.length; i++) {
            const task = model.active_tasks[i];
            const title = task.title.length > 0
                ? task.title
                : (task.task_id.length > 0 ? task.task_id : 'Untitled task');
            const row = new PopupMenu.PopupMenuItem(
                AgentOSUtils.truncate(title, 48) + ' \u2014 ' + task.state);
            // Clicking opens the desktop application, where task details live.
            // The extension itself can neither inspect nor control a task.
            row.connect('activate', () => this._client.openDesktop());
            rows.push(row);
        }
        return rows;
    }

    /**
     * Build the rows for the "Waiting for approval" submenu.
     * @param {Object} model Normalized status model.
     * @returns {Array} Menu items, always at least one.
     */
    _buildApprovalRows(model) {
        if (!model.available)
            return [this._placeholderRow('Approvals need the AgentOS service')];
        if (model.pending_approvals.length === 0)
            return [this._placeholderRow('Nothing waiting for approval')];

        const rows = [];
        for (let i = 0; i < model.pending_approvals.length; i++) {
            const approval = model.pending_approvals[i];
            const row = new PopupMenu.PopupMenuItem(describeApproval(approval));
            addStyleClass(row, 'agentos-approval-item');
            if (approval.reason.length > 0)
                row.label.tooltip_text = AgentOSUtils.truncate(approval.reason, 200);
            // Hard rule: the extension never approves. It only opens the app.
            row.connect('activate', () => this._client.openDesktop());
            rows.push(row);
        }
        return rows;
    }

    /**
     * Build the rows for the "Recent activity" submenu.
     * @param {Object} model Normalized status model.
     * @returns {Array} Menu items, always at least one.
     */
    _buildEventRows(model) {
        if (!model.available)
            return [this._placeholderRow('No recent activity available')];

        let limit = 5;
        try {
            limit = this._settings.get_uint('max-recent-events');
        } catch (error) {
            log('[AgentOS] could not read max-recent-events: ' + error.message);
            limit = 5;
        }
        if (limit < 1)
            limit = 1;
        if (limit > MAX_BROKER_EVENTS)
            limit = MAX_BROKER_EVENTS;

        if (model.recent_events.length === 0)
            return [this._placeholderRow('No recent activity')];

        // Most important first: errors, then warnings, then the rest in broker
        // order (newest first).
        const ordered = [];
        for (let i = 0; i < model.recent_events.length; i++)
            ordered.push(model.recent_events[i]);
        ordered.sort((left, right) => {
            return _severityRank(right.severity) - _severityRank(left.severity);
        });

        const rows = [];
        const shown = Math.min(limit, ordered.length);
        for (let j = 0; j < shown; j++) {
            const event = ordered[j];
            const row = new PopupMenu.PopupMenuItem(describeEvent(event));
            addStyleClass(row, 'agentos-event');
            const styleClass = AgentOSUtils.severityClass(event.severity);
            if (styleClass.length > 0)
                addStyleClass(row, styleClass);
            if (event.state === 'FAILED' && styleClass !== 'agentos-event-error')
                addStyleClass(row, 'agentos-status-bad');

            const clock = AgentOSUtils.formatClock(event.timestamp);
            if (clock.length > 0) {
                // A right-aligned dimmed time keeps the summary readable.
                const stamp = new St.Label({
                    text: clock,
                    style_class: 'agentos-subtext',
                    y_align: St.Align.MIDDLE,
                });
                row.add_child(stamp);
            }
            row.connect('activate', () => this._client.openDesktop());
            rows.push(row);
        }
        return rows;
    }

    /**
     * Create the disabled row used when a submenu has nothing to show.
     * @param {string} text Placeholder text.
     * @returns {PopupMenu.PopupMenuItem} A non-reactive menu row.
     */
    _placeholderRow(text) {
        const row = new PopupMenu.PopupMenuItem(text);
        row.setSensitive(false);
        return row;
    }

    /**
     * Seconds since an arbitrary epoch, used for throttle comparisons only.
     * @returns {number} Monotonic seconds.
     */
    _monotonicSeconds() {
        // GLib.get_monotonic_time() returns microseconds and is unaffected by
        // wall-clock changes; it exists in GLib 2.28+ (GNOME 42 uses 2.72).
        return GLib.get_monotonic_time() / 1000000;
    }

    /**
     * React to a poll-interval change by restarting the timer immediately.
     * @returns {void}
     */
    _onPollIntervalChanged() {
        this._stopTimer();
        this._startTimer();
        this._client.requestStatus(true);
    }

    /**
     * Read the configured poll interval, clamped to the schema range.
     * @returns {number} Interval in seconds.
     */
    _readPollInterval() {
        let value = DEFAULT_POLL_SECONDS;
        try {
            value = this._settings.get_uint('poll-interval-seconds');
        } catch (error) {
            log('[AgentOS] could not read poll-interval-seconds: ' + error.message);
            value = DEFAULT_POLL_SECONDS;
        }
        if (value < MIN_POLL_SECONDS)
            return MIN_POLL_SECONDS;
        if (value > MAX_POLL_SECONDS)
            return MAX_POLL_SECONDS;
        return value;
    }

    /**
     * Start the D-Bus subscription and the polling loop.
     * @returns {void}
     */
    start() {
        this._client.start();
        this._startTimer();
        this._client.requestStatus(true);
    }

    /**
     * Create the repeating poll timeout source (idempotent).
     * @returns {void}
     */
    _startTimer() {
        this._stopTimer();
        this._pollSeconds = this._readPollInterval();
        this._timeoutId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, this._pollSeconds,
            () => {
                if (this._destroyed)
                    return GLib.SOURCE_REMOVE;

                // The interval is re-read on every tick so a settings change
                // takes effect even if the `changed` signal was missed.
                const wanted = this._readPollInterval();
                if (wanted !== this._pollSeconds) {
                    // This source is removed by GLib when the callback returns
                    // SOURCE_REMOVE, so forget its id now: a later
                    // source_remove() on a dead id would warn.
                    this._timeoutId = 0;
                    this._client.requestStatus(true);
                    // Install the replacement source with the new interval.
                    this._startTimer();
                    return GLib.SOURCE_REMOVE;
                }
                this._client.requestStatus(false);
                return GLib.SOURCE_CONTINUE;
            });
    }

    /**
     * Remove the poll timeout source if one is registered.
     * @returns {void}
     */
    _stopTimer() {
        if (this._timeoutId !== 0) {
            GLib.source_remove(this._timeoutId);
            this._timeoutId = 0;
        }
    }

    /**
     * Release every resource this indicator owns. Safe to call twice.
     * @returns {void}
     */
    destroy() {
        if (this._destroyed)
            return;
        this._destroyed = true;

        this._stopTimer();

        if (this._settingsChangedId !== 0) {
            this._settings.disconnect(this._settingsChangedId);
            this._settingsChangedId = 0;
        }

        this._client.stop();

        // PanelMenu.Button.destroy() exists on GNOME 44+. On GNOME 42/43 the
        // panel button inherits St.Widget's destroy, which is present as well;
        // the explicit fallback below covers any revision that lacks both.
        if (typeof super.destroy === 'function') {
            super.destroy();
            return;
        }

        try {
            if (this.menu !== undefined && this.menu !== null)
                this.menu.destroy();
            if (this._box !== undefined && this._box !== null)
                this._box.destroy();
        } catch (error) {
            log('[AgentOS] could not destroy the panel indicator actors: ' + error.message);
        }
    }
};

/**
 * Rank a severity for sorting, highest first.
 * @param {*} severity Raw severity string.
 * @returns {number} 2 for errors, 1 for warnings, 0 otherwise.
 */
function _severityRank(severity) {
    const bucket = AgentOSUtils.severityOf(severity);
    if (bucket === 'error')
        return 2;
    if (bucket === 'warning')
        return 1;
    return 0;
}

// ---------------------------------------------------------------------------
// Extension entry points
// ---------------------------------------------------------------------------

/**
 * Locate the GSettings instance for this extension.
 *
 * `ExtensionUtils.getSettings()` is the supported path on GNOME 42-44, but it
 * can be missing on differently packaged builds; the Gio.Settings fallback keeps
 * the extension enableable. Only UI state is ever stored here.
 * @returns {Gio.Settings|null} A settings object, or null when the schema is
 *   unavailable (the caller then runs with compiled-in defaults).
 */
function getAgentSettings() {
    if (ExtensionUtils !== undefined && ExtensionUtils !== null &&
        typeof ExtensionUtils.getSettings === 'function') {
        try {
            return ExtensionUtils.getSettings('org.gnome.shell.extensions.agentos');
        } catch (error) {
            log('[AgentOS] ExtensionUtils.getSettings failed, falling back to Gio.Settings: ' +
                error.message);
        }
    }
    try {
        return new Gio.Settings({ schema_id: 'org.gnome.shell.extensions.agentos' });
    } catch (error) {
        logError(error, '[AgentOS] settings schema org.gnome.shell.extensions.agentos is missing');
        return null;
    }
}

/** Live indicator, or null while disabled. */
let _indicator = null;

/**
 * MessageTray source shared by every AgentOS indicator instance for the
 * lifetime of the Shell session. Kept outside the class so an enable/disable
 * cycle neither leaks a tray source nor re-adds one that already exists.
 */
let _sharedNotificationSource = null;

/**
 * Extension entry point. Registers the indicator, all signals and timers.
 * @returns {void}
 */
function enable() {
    const settings = getAgentSettings();
    if (settings === null) {
        // Without a schema the extension cannot honour its own preferences, but
        // it must still not break the Shell: no indicator is installed.
        log('[AgentOS] disabled: settings schema unavailable');
        return;
    }

    try {
        _indicator = new AgentOSIndicator(settings);
        Main.panel.addToStatusArea(EXTENSION_UUID, _indicator);
        _indicator.start();
    } catch (error) {
        // A failure while building the UI must leave the Shell untouched.
        logError(error, '[AgentOS] could not add the indicator to the panel');
        if (_indicator !== null) {
            try {
                _indicator.destroy();
            } catch (destroyError) {
                logError(destroyError, '[AgentOS] could not clean up a failed indicator');
            }
            _indicator = null;
        }
        return;
    }

    log('[AgentOS] extension enabled');
}

/**
 * Extension entry point. Removes the indicator and every resource it added.
 * @returns {void}
 */
function disable() {
    if (_indicator !== null) {
        try {
            _indicator.destroy();
        } catch (error) {
            logError(error, '[AgentOS] failed to destroy the indicator cleanly');
        }
        _indicator = null;
    }
    log('[AgentOS] extension disabled');
}
