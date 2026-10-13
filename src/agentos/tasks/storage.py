"""Durable task, event, and approval storage on SQLite.

One database holds tasks, their event history, runtime sessions, approvals, and
the audit trail. Every write is transactional, schema changes go through a
monotonic migration chain, and the service opens the database in WAL mode so
that readers (the CLI, the desktop app) never block the task worker.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from collections.abc import Iterable, Mapping
from typing import Any

from ..constants import STORAGE_SCHEMA_VERSION
from ..errors import StorageError, TaskStateError, ValidationError
from ..events import format_timestamp, parse_timestamp, utc_now
from ..jsonutil import bounded_payload, digest, truncate_text
from ..policy.approvals import Approval, ApprovalRequest
from ..policy.capabilities import PrivilegeLevel
from .states import TaskState, require_transition

#: Default page-level durability settings applied to every connection.
_CONNECTION_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=10000",
)

#: Maximum bytes retained for one task result.
MAX_RESULT_BYTES = 64 * 1024


def _safe_rollback(connection: sqlite3.Connection) -> None:
    """Roll back an explicit transaction, tolerating an already-closed one.

    Every write path rolls back on failure; without this guard a rollback after
    an inner handler already rolled back would replace the original error with
    "cannot rollback - no transaction is active".
    """
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _safe_commit(connection: sqlite3.Connection) -> None:
    """Commit an explicit transaction, tolerating an already-closed one."""
    try:
        connection.execute("COMMIT")
    except sqlite3.Error:
        pass


@dataclass(slots=True)
class TaskRecord:
    """A durable task.

    :param task_id: Stable identity.
    :param prompt: The user instruction.
    :param owner_uid: Local user who created the task.
    :param owner_name: Display name of that user, when known.
    :param state: Current lifecycle state.
    :param created_at: Creation timestamp (RFC 3339, UTC).
    :param updated_at: Last state change timestamp.
    :param workspace: Absolute working directory for the runtime session.
    :param runtime_id: Runtime selected for this task.
    :param session_id: Runtime-side session id, once created.
    :param level_cap: Highest privilege level this task may reach.
    :param title: Short display title.
    :param priority: Higher runs first among queued tasks.
    :param started_at: When the task began running.
    :param finished_at: When the task reached a terminal state.
    :param cancel_requested: Whether a cancellation was requested.
    :param side_effects_possible: Whether the task could have already executed a
        side-effecting operation; used to decide whether recovery may requeue it.
    :param result_summary: Bounded assistant output, once available.
    :param error_message: Last failure explanation.
    :param attempt_count: Number of times the task has started running.
    """

    task_id: str
    prompt: str
    owner_uid: int
    owner_name: str = ""
    state: TaskState = TaskState.CREATED
    created_at: str = ""
    updated_at: str = ""
    workspace: str = ""
    runtime_id: str = "mock"
    session_id: str | None = None
    level_cap: PrivilegeLevel = PrivilegeLevel.READ_ONLY
    title: str = ""
    priority: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    cancel_requested: bool = False
    side_effects_possible: bool = False
    result_summary: str = ""
    error_message: str = ""
    attempt_count: int = 0

    def __post_init__(self) -> None:
        if not self.task_id:
            self.task_id = str(uuid.uuid4())
        if not self.prompt.strip():
            raise ValidationError("task prompt must not be empty")
        if isinstance(self.owner_uid, bool) or self.owner_uid < 0:
            raise ValidationError("task owner_uid must be a non-negative integer")
        if not isinstance(self.state, TaskState):
            self.state = TaskState(self.state)
        if not isinstance(self.level_cap, PrivilegeLevel):
            self.level_cap = PrivilegeLevel(self.level_cap)
        moment = format_timestamp(utc_now())
        if not self.created_at:
            self.created_at = moment
        if not self.updated_at:
            self.updated_at = self.created_at
        if not self.title:
            self.title = truncate_text(" ".join(self.prompt.split()), limit=72)

    def to_dict(self) -> dict[str, Any]:
        """Return the wire representation of this task."""
        return {
            "task_id": self.task_id,
            "prompt": truncate_text(self.prompt, limit=8192),
            "owner_uid": self.owner_uid,
            "owner_name": self.owner_name,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "workspace": self.workspace,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "level_cap": int(self.level_cap),
            "level_cap_label": self.level_cap.label,
            "title": self.title,
            "priority": self.priority,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cancel_requested": self.cancel_requested,
            "side_effects_possible": self.side_effects_possible,
            "result_summary": truncate_text(self.result_summary, limit=8192),
            "error_message": truncate_text(self.error_message, limit=2048),
            "attempt_count": self.attempt_count,
        }


@dataclass(slots=True)
class TaskEventRecord:
    """One entry in a task's event history.

    :param task_id: Owning task.
    :param sequence: Monotonic sequence within the task.
    :param event_type: Versioned AgentOS event type.
    :param timestamp: Creation timestamp (RFC 3339, UTC).
    :param payload: Event body, redacted before storage.
    """

    task_id: str
    sequence: int
    event_type: str
    timestamp: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the wire representation of this history entry."""
        return {
            "task_id": self.task_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "payload": bounded_payload(dict(self.payload)),
        }


def store_digest(payload: Mapping[str, Any]) -> str:
    """Return the digest recorded alongside a stored payload."""
    return digest(dict(payload))


class Storage:
    """SQLite-backed storage for tasks, approvals, task history, and audit.

    :param path: Database file path; ``":memory:"`` is accepted for tests.
    :param create: Whether to create the schema when it is absent.
    """

    def __init__(self, path: str, *, create: bool = True) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        if path != ":memory:" and create:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._connection = self._connect(path)
        self._migrate()

    # -- connection management --------------------------------------------

    @staticmethod
    def _connect(path: str) -> sqlite3.Connection:
        """Open a connection with the deployment's pragmas applied.

        ``check_same_thread`` is disabled because the Core service serves IPC
        connections on the event loop while the task manager runs in worker
        threads. Every statement goes through :meth:`Storage._lock`, so a single
        connection is still only used by one thread at a time.
        """
        try:
            connection = sqlite3.connect(path, timeout=10.0, isolation_level=None, check_same_thread=False)
        except sqlite3.Error as error:
            raise StorageError(f"cannot open database {path}: {error}", details={"path": path}) from error
        connection.row_factory = sqlite3.Row
        for pragma in _CONNECTION_PRAGMAS:
            try:
                connection.execute(pragma)
            except sqlite3.Error as error:
                raise StorageError(f"cannot apply {pragma}: {error}", details={"path": path}) from error
        return connection

    @property
    def path(self) -> str:
        """Return the database path."""
        return self._path

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the live connection."""
        if self._connection is None:
            raise StorageError("storage has been closed")
        return self._connection

    def close(self) -> None:
        """Close the connection; safe to call repeatedly."""
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _migrate(self) -> None:
        """Apply the migration chain up to :data:`STORAGE_SCHEMA_VERSION`."""
        connection = self.connection
        try:
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.Error as error:
            raise StorageError(f"cannot read the schema version: {error}") from error
        if current > STORAGE_SCHEMA_VERSION:
            raise StorageError(
                f"database schema version {current} is newer than this build supports "
                f"({STORAGE_SCHEMA_VERSION}); upgrade agentos-core or restore a backup",
                details={"database_version": current, "supported": STORAGE_SCHEMA_VERSION},
            )
        for version in range(current + 1, STORAGE_SCHEMA_VERSION + 1):
            migration = _MIGRATIONS.get(version)
            if migration is None:
                raise StorageError(f"no migration is registered for schema version {version}")
            try:
                connection.execute("BEGIN IMMEDIATE")
                migration(connection)
                connection.execute(f"PRAGMA user_version={version}")
                _safe_commit(connection)
            except sqlite3.Error as error:
                _safe_rollback(connection)
                raise StorageError(
                    f"migration to schema version {version} failed: {error}",
                    details={"database_version": version},
                ) from error

    # -- tasks -------------------------------------------------------------

    def insert_task(self, task: TaskRecord) -> None:
        """Insert a new task.

        :raises StorageError: When the task id already exists or the insert fails.
        """
        with self._lock:
            try:
                self.connection.execute(
                    """
                    INSERT INTO tasks (
                        task_id, prompt, owner_uid, owner_name, state, created_at, updated_at,
                        workspace, runtime_id, session_id, level_cap, title, priority, started_at,
                        finished_at, cancel_requested, side_effects_possible, result_summary,
                        error_message, attempt_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _task_row(task),
                )
            except sqlite3.IntegrityError as error:
                raise StorageError(f"task {task.task_id} already exists", details={"task_id": task.task_id}) from error
            except sqlite3.Error as error:
                raise StorageError(f"cannot insert task: {error}", details={"task_id": task.task_id}) from error

    def get_task(self, task_id: str) -> TaskRecord | None:
        """Return one task, or ``None`` when it does not exist."""
        with self._lock:
            try:
                row = self.connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            except sqlite3.Error as error:
                raise StorageError(f"cannot read task {task_id}: {error}") from error
        return None if row is None else _task_from_row(row)

    def list_tasks(
        self,
        *,
        states: Iterable[TaskState] = (),
        owner_uid: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TaskRecord]:
        """Return tasks newest first.

        :param states: Restrict to these states.
        :param owner_uid: Restrict to one owner.
        :param limit: Maximum rows, capped at 500.
        :param offset: Rows to skip.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValidationError("task list limit must be between 1 and 500")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValidationError("task list offset must be a non-negative integer")
        clauses: list[str] = []
        values: list[Any] = []
        state_values = [state.value if isinstance(state, TaskState) else str(state) for state in states]
        if state_values:
            for state in state_values:
                if state not in {member.value for member in TaskState}:
                    raise ValidationError(f"unknown task state {state!r}")
            clauses.append("state IN (" + ",".join("?" for _ in state_values) + ")")
            values.extend(state_values)
        if owner_uid is not None:
            clauses.append("owner_uid = ?")
            values.append(owner_uid)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM tasks{where} ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?"
        values.extend([limit, offset])
        with self._lock:
            try:
                rows = self.connection.execute(sql, values).fetchall()
            except sqlite3.Error as error:
                raise StorageError(f"cannot list tasks: {error}") from error
        return [_task_from_row(row) for row in rows]

    def claim_next_task(self) -> TaskRecord | None:
        """Atomically move the highest-priority queued task to ``RUNNING``.

        The select and update happen inside one immediate transaction so that two
        workers cannot claim the same task.

        :returns: The claimed task, or ``None`` when the queue is empty.
        """
        with self._lock:
            connection = self.connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM tasks WHERE state = ? ORDER BY priority DESC, created_at ASC LIMIT 1",
                    (TaskState.QUEUED.value,),
                ).fetchone()
                if row is None:
                    _safe_commit(connection)
                    return None
                task = _task_from_row(row)
                require_transition(task.state, TaskState.RUNNING)
                moment = format_timestamp(utc_now())
                connection.execute(
                    "UPDATE tasks SET state = ?, updated_at = ?, started_at = COALESCE(started_at, ?),"
                    " attempt_count = attempt_count + 1 WHERE task_id = ?",
                    (TaskState.RUNNING.value, moment, moment, task.task_id),
                )
                _safe_commit(connection)
            except sqlite3.Error as error:
                _safe_rollback(connection)
                raise StorageError(f"cannot claim a queued task: {error}") from error
            refreshed = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task.task_id,)).fetchone()
        return _task_from_row(refreshed) if refreshed is not None else task

    def update_task_state(
        self,
        task_id: str,
        target: TaskState,
        *,
        error_message: str = "",
        result_summary: str | None = None,
        session_id: str | None = None,
        side_effects_possible: bool | None = None,
        cancel_requested: bool | None = None,
    ) -> TaskRecord:
        """Transition a task and return the refreshed record.

        :param task_id: Task to transition.
        :param target: Requested next state.
        :param error_message: Failure explanation, applied when ``target`` is ``FAILED``.
        :param result_summary: Bounded assistant output, when one is available.
        :param session_id: Runtime session id to record.
        :param side_effects_possible: Whether a side effect may have started.
        :param cancel_requested: Whether cancellation is requested.
        :raises TaskStateError: When the transition is illegal.
        :raises StorageError: When the task does not exist.
        """
        with self._lock:
            connection = self.connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                if row is None:
                    _safe_rollback(connection)
                    raise StorageError(f"task {task_id} does not exist", details={"task_id": task_id})
                task = _task_from_row(row)
                require_transition(task.state, target)
                moment = format_timestamp(utc_now())
                finished = moment if target.is_terminal else None
                connection.execute(
                    """
                    UPDATE tasks SET
                        state = ?, updated_at = ?, finished_at = COALESCE(?, finished_at),
                        error_message = CASE WHEN ? = '' THEN error_message ELSE ? END,
                        result_summary = COALESCE(?, result_summary),
                        session_id = COALESCE(?, session_id),
                        side_effects_possible = COALESCE(?, side_effects_possible),
                        cancel_requested = COALESCE(?, cancel_requested)
                    WHERE task_id = ?
                    """,
                    (
                        target.value,
                        moment,
                        finished,
                        error_message,
                        truncate_text(error_message, limit=2048),
                        None if result_summary is None else truncate_text(result_summary, limit=MAX_RESULT_BYTES),
                        session_id,
                        None if side_effects_possible is None else int(bool(side_effects_possible)),
                        None if cancel_requested is None else int(bool(cancel_requested)),
                        task_id,
                    ),
                )
                _safe_commit(connection)
                refreshed = connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            except sqlite3.Error as error:
                _safe_rollback(connection)
                raise StorageError(f"cannot update task {task_id}: {error}", details={"task_id": task_id}) from error
        if refreshed is None:
            raise StorageError(f"task {task_id} disappeared during update", details={"task_id": task_id})
        return _task_from_row(refreshed)

    def request_cancellation(self, task_id: str) -> TaskRecord:
        """Flag a task for cancellation and return the refreshed record."""
        with self._lock:
            try:
                self.connection.execute(
                    "UPDATE tasks SET cancel_requested = 1, updated_at = ? WHERE task_id = ?",
                    (format_timestamp(utc_now()), task_id),
                )
                row = self.connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            except sqlite3.Error as error:
                raise StorageError(f"cannot request cancellation: {error}", details={"task_id": task_id}) from error
        if row is None:
            raise StorageError(f"task {task_id} does not exist", details={"task_id": task_id})
        return _task_from_row(row)

    def count_tasks_by_state(self) -> dict[str, int]:
        """Return the number of tasks in each state."""
        with self._lock:
            try:
                rows = self.connection.execute("SELECT state, COUNT(*) FROM tasks GROUP BY state").fetchall()
            except sqlite3.Error as error:
                raise StorageError(f"cannot count tasks: {error}") from error
        counts = {member.value: 0 for member in TaskState}
        for row in rows:
            counts[str(row[0])] = int(row[1])
        return counts

    # -- task history ------------------------------------------------------

    def append_task_event(
        self,
        task_id: str,
        event_type: str,
        payload: Mapping[str, Any] = {},
    ) -> TaskEventRecord:
        """Append one entry to a task's history and return it."""
        redacted = bounded_payload(dict(payload))
        with self._lock:
            connection = self.connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM task_events WHERE task_id = ?", (task_id,)
                ).fetchone()
                sequence = int(row[0]) + 1
                moment = format_timestamp(utc_now())
                connection.execute(
                    "INSERT INTO task_events (task_id, sequence, event_type, timestamp, payload_json, payload_digest)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        sequence,
                        event_type,
                        parse_timestamp(moment).isoformat(),
                        json.dumps(redacted, ensure_ascii=False, sort_keys=True),
                        store_digest(redacted),
                    ),
                )
                _safe_commit(connection)
            except sqlite3.Error as error:
                _safe_rollback(connection)
                raise StorageError(f"cannot append task event: {error}", details={"task_id": task_id}) from error
        return TaskEventRecord(
            task_id=task_id,
            sequence=sequence,
            event_type=event_type,
            timestamp=moment,
            payload=redacted,
        )

    def list_task_events(self, task_id: str, *, limit: int = 200, after_sequence: int = 0) -> list[TaskEventRecord]:
        """Return a task's history in order.

        :param task_id: Owning task.
        :param limit: Maximum entries, capped at 1000.
        :param after_sequence: Return only entries after this sequence number.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValidationError("task event limit must be between 1 and 1000")
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
            raise ValidationError("after_sequence must be a non-negative integer")
        with self._lock:
            try:
                rows = self.connection.execute(
                    "SELECT task_id, sequence, event_type, timestamp, payload_json FROM task_events"
                    " WHERE task_id = ? AND sequence > ? ORDER BY sequence ASC LIMIT ?",
                    (task_id, after_sequence, limit),
                ).fetchall()
            except sqlite3.Error as error:
                raise StorageError(f"cannot read task events: {error}", details={"task_id": task_id}) from error
        events: list[TaskEventRecord] = []
        for row in rows:
            try:
                payload = json.loads(row[4]) if row[4] else {}
            except json.JSONDecodeError:
                payload = {"unreadable": True}
            events.append(
                TaskEventRecord(
                    task_id=str(row[0]),
                    sequence=int(row[1]),
                    event_type=str(row[2]),
                    timestamp=str(row[3]),
                    payload=payload,
                )
            )
        return events

    # -- approvals ---------------------------------------------------------

    def insert_approval(self, approval: Approval) -> None:
        """Persist a new approval request."""
        with self._lock:
            try:
                self.connection.execute(
                    """
                    INSERT INTO approvals (
                        approval_id, task_id, operation, target, parameter_digest, parameters_json,
                        level, state, nonce, created_at, expires_at, decided_at, decided_by_uid,
                        decided_by_name, decision_note, consumed_at, consumed_by_task, requested_by_uid,
                        requested_by_name, reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _approval_row(approval),
                )
            except sqlite3.IntegrityError as error:
                raise StorageError(
                    f"approval {approval.approval_id} already exists",
                    details={"approval_id": approval.approval_id},
                ) from error
            except sqlite3.Error as error:
                raise StorageError(f"cannot persist approval: {error}") from error

    def get_approval(self, approval_id: str) -> Approval | None:
        """Return one approval, or ``None`` when it does not exist."""
        with self._lock:
            try:
                row = self.connection.execute(
                    "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
                ).fetchone()
            except sqlite3.Error as error:
                raise StorageError(f"cannot read approval {approval_id}: {error}") from error
        return None if row is None else _approval_from_row(row)

    def list_approvals(
        self,
        *,
        states: Iterable[str] = (),
        task_id: str | None = None,
        limit: int = 50,
    ) -> list[Approval]:
        """Return approvals newest first, expiring stale ones first."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValidationError("approval list limit must be between 1 and 500")
        self.expire_stale_approvals()
        clauses: list[str] = []
        values: list[Any] = []
        state_values = [str(state) for state in states]
        if state_values:
            clauses.append("state IN (" + ",".join("?" for _ in state_values) + ")")
            values.extend(state_values)
        if task_id is not None:
            clauses.append("task_id = ?")
            values.append(task_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        values.append(limit)
        with self._lock:
            try:
                rows = self.connection.execute(
                    f"SELECT * FROM approvals{where} ORDER BY created_at DESC LIMIT ?", values
                ).fetchall()
            except sqlite3.Error as error:
                raise StorageError(f"cannot list approvals: {error}") from error
        return [_approval_from_row(row) for row in rows]

    def expire_stale_approvals(self) -> int:
        """Mark every past-expiry pending or granted approval as expired."""
        moment = parse_timestamp(format_timestamp(utc_now())).isoformat()
        with self._lock:
            try:
                cursor = self.connection.execute(
                    "UPDATE approvals SET state = 'expired' WHERE state IN ('pending', 'granted') AND expires_at <= ?",
                    (moment,),
                )
                expired = cursor.rowcount if cursor.rowcount > 0 else 0
            except sqlite3.Error as error:
                raise StorageError(f"cannot expire approvals: {error}") from error
        return expired

    def decide_approval(
        self,
        approval_id: str,
        *,
        granted: bool,
        decided_by_uid: int,
        decided_by_name: str = "",
        note: str = "",
    ) -> Approval:
        """Record a human decision on a pending approval.

        :param approval_id: Approval to decide.
        :param granted: Whether the human authorized the action.
        :param decided_by_uid: Deciding local user.
        :param decided_by_name: Display name of that user.
        :param note: Optional human note, bounded before storage.
        :raises StorageError: When the approval is missing.
        :raises ValidationError: When the approval is no longer pending.
        """
        with self._lock:
            connection = self.connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
                if row is None:
                    _safe_rollback(connection)
                    raise StorageError(f"approval {approval_id} does not exist", details={"approval_id": approval_id})
                approval = _approval_from_row(row)
                if approval.state != "pending":
                    _safe_rollback(connection)
                    raise ValidationError(
                        f"approval {approval_id} is {approval.state} and can no longer be decided",
                        details={"approval_id": approval_id, "state": approval.state},
                    )
                if approval.is_expired:
                    connection.execute(
                        "UPDATE approvals SET state = 'expired' WHERE approval_id = ?", (approval_id,)
                    )
                    _safe_commit(connection)
                    raise ValidationError(
                        f"approval {approval_id} has expired",
                        details={"approval_id": approval_id, "expires_at": approval.expires_at},
                    )
                moment = format_timestamp(utc_now())
                connection.execute(
                    "UPDATE approvals SET state = ?, decided_at = ?, decided_by_uid = ?, decided_by_name = ?,"
                    " decision_note = ? WHERE approval_id = ?",
                    (
                        "granted" if granted else "denied",
                        parse_timestamp(moment).isoformat(),
                        decided_by_uid,
                        truncate_text(decided_by_name, limit=128),
                        truncate_text(note, limit=512),
                        approval_id,
                    ),
                )
                _safe_commit(connection)
            except sqlite3.Error as error:
                _safe_rollback(connection)
                raise StorageError(f"cannot decide approval {approval_id}: {error}") from error
        refreshed = self.get_approval(approval_id)
        if refreshed is None:
            raise StorageError(f"approval {approval_id} disappeared after decision")
        return refreshed

    def consume_approval(self, approval_id: str, *, task_id: str) -> Approval:
        """Mark an approval consumed by ``task_id``.

        The update only matches a still-granted, unexpired, unconsumed row, so a
        concurrent second attempt sees no affected row and fails.

        :raises StorageError: When the approval does not exist.
        :raises ValidationError: When the approval is not currently usable.
        """
        moment = format_timestamp(utc_now())
        with self._lock:
            connection = self.connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "UPDATE approvals SET state = 'consumed', consumed_at = ?, consumed_by_task = ?"
                    " WHERE approval_id = ? AND state = 'granted' AND expires_at > ?",
                    (parse_timestamp(moment).isoformat(), task_id, approval_id, parse_timestamp(moment).isoformat()),
                )
                changed = cursor.rowcount if cursor.rowcount > 0 else 0
                if changed != 1:
                    _safe_rollback(connection)
                    current = connection.execute(
                        "SELECT state FROM approvals WHERE approval_id = ?", (approval_id,)
                    ).fetchone()
                    if current is None:
                        raise StorageError(
                            f"approval {approval_id} does not exist", details={"approval_id": approval_id}
                        )
                    raise ValidationError(
                        f"approval {approval_id} is {current[0]} and cannot be consumed",
                        details={"approval_id": approval_id, "state": current[0]},
                    )
                _safe_commit(connection)
            except sqlite3.Error as error:
                _safe_rollback(connection)
                raise StorageError(f"cannot consume approval {approval_id}: {error}") from error
        refreshed = self.get_approval(approval_id)
        if refreshed is None:
            raise StorageError(f"approval {approval_id} disappeared after consumption")
        return refreshed

    def revoke_approval(self, approval_id: str, *, revoked_by_uid: int) -> Approval:
        """Revoke a granted approval before it is consumed."""
        with self._lock:
            try:
                cursor = self.connection.execute(
                    "UPDATE approvals SET state = 'revoked' WHERE approval_id = ? AND state IN ('pending', 'granted')",
                    (approval_id,),
                )
                changed = cursor.rowcount if cursor.rowcount > 0 else 0
            except sqlite3.Error as error:
                raise StorageError(f"cannot revoke approval {approval_id}: {error}") from error
        if changed != 1:
            current = self.get_approval(approval_id)
            if current is None:
                raise StorageError(f"approval {approval_id} does not exist", details={"approval_id": approval_id})
            raise ValidationError(
                f"approval {approval_id} is {current.state} and cannot be revoked",
                details={"approval_id": approval_id, "state": current.state},
            )
        refreshed = self.get_approval(approval_id)
        if refreshed is None:
            raise StorageError(f"approval {approval_id} disappeared after revocation")
        return refreshed

    def count_pending_approvals(self) -> int:
        """Return the number of approvals still awaiting a human decision."""
        self.expire_stale_approvals()
        with self._lock:
            try:
                row = self.connection.execute(
                    "SELECT COUNT(*) FROM approvals WHERE state IN ('pending', 'granted')"
                ).fetchone()
            except sqlite3.Error as error:
                raise StorageError(f"cannot count approvals: {error}") from error
        return int(row[0]) if row else 0


# -- row mapping helpers ---------------------------------------------------


def _task_row(task: TaskRecord) -> tuple[Any, ...]:
    """Flatten a task into an insert tuple."""
    return (
        task.task_id,
        task.prompt,
        task.owner_uid,
        task.owner_name,
        task.state.value,
        parse_timestamp(task.created_at).isoformat(),
        parse_timestamp(task.updated_at).isoformat(),
        task.workspace,
        task.runtime_id,
        task.session_id,
        int(task.level_cap),
        task.title,
        task.priority,
        None if task.started_at is None else parse_timestamp(task.started_at).isoformat(),
        None if task.finished_at is None else parse_timestamp(task.finished_at).isoformat(),
        int(bool(task.cancel_requested)),
        int(bool(task.side_effects_possible)),
        task.result_summary,
        task.error_message,
        task.attempt_count,
    )


def _task_from_row(row: sqlite3.Row) -> TaskRecord:
    """Rebuild a task from a database row."""
    return TaskRecord(
        task_id=str(row["task_id"]),
        prompt=str(row["prompt"]),
        owner_uid=int(row["owner_uid"]),
        owner_name=str(row["owner_name"] or ""),
        state=TaskState(str(row["state"])),
        created_at=_iso_to_rfc3339(str(row["created_at"])),
        updated_at=_iso_to_rfc3339(str(row["updated_at"])),
        workspace=str(row["workspace"] or ""),
        runtime_id=str(row["runtime_id"] or "mock"),
        session_id=row["session_id"],
        level_cap=PrivilegeLevel(int(row["level_cap"])),
        title=str(row["title"] or ""),
        priority=int(row["priority"]),
        started_at=None if row["started_at"] is None else _iso_to_rfc3339(str(row["started_at"])),
        finished_at=None if row["finished_at"] is None else _iso_to_rfc3339(str(row["finished_at"])),
        cancel_requested=bool(row["cancel_requested"]),
        side_effects_possible=bool(row["side_effects_possible"]),
        result_summary=str(row["result_summary"] or ""),
        error_message=str(row["error_message"] or ""),
        attempt_count=int(row["attempt_count"]),
    )


def _approval_row(approval: Approval) -> tuple[Any, ...]:
    """Flatten an approval into an insert tuple."""
    request = approval.request
    return (
        approval.approval_id,
        request.task_id,
        request.operation,
        request.target,
        request.parameter_digest,
        json.dumps(dict(request.parameters), ensure_ascii=False, sort_keys=True),
        int(request.level),
        approval.state,
        approval.nonce,
        parse_timestamp(approval.created_at).isoformat(),
        parse_timestamp(approval.expires_at).isoformat(),
        None if approval.decided_at is None else parse_timestamp(approval.decided_at).isoformat(),
        approval.decided_by_uid,
        approval.decided_by_name,
        approval.decision_note,
        None if approval.consumed_at is None else parse_timestamp(approval.consumed_at).isoformat(),
        approval.consumed_by_task,
        request.requested_by_uid,
        request.requested_by_name,
        request.reason,
    )


def _approval_from_row(row: sqlite3.Row) -> Approval:
    """Rebuild an approval from a database row."""
    try:
        parameters = json.loads(row["parameters_json"]) if row["parameters_json"] else {}
    except json.JSONDecodeError:
        parameters = {}
    request = ApprovalRequest(
        task_id=str(row["task_id"]),
        operation=str(row["operation"]),
        target=row["target"],
        parameters=parameters,
        level=int(row["level"]),
        requested_by_uid=int(row["requested_by_uid"]),
        requested_by_name=str(row["requested_by_name"] or ""),
        reason=str(row["reason"] or ""),
    )
    return Approval(
        approval_id=str(row["approval_id"]),
        request=request,
        state=str(row["state"]),
        nonce=str(row["nonce"]),
        created_at=_iso_to_rfc3339(str(row["created_at"])),
        expires_at=_iso_to_rfc3339(str(row["expires_at"])),
        decided_at=None if row["decided_at"] is None else _iso_to_rfc3339(str(row["decided_at"])),
        decided_by_uid=row["decided_by_uid"],
        decided_by_name=str(row["decided_by_name"] or ""),
        decision_note=str(row["decision_note"] or ""),
        consumed_at=None if row["consumed_at"] is None else _iso_to_rfc3339(str(row["consumed_at"])),
        consumed_by_task=row["consumed_by_task"],
    )


def _iso_to_rfc3339(value: str) -> str:
    """Convert a stored SQLite timestamp back to RFC 3339 in UTC."""
    try:
        return format_timestamp(parse_timestamp(value))
    except ValidationError:
        return value


# -- migrations ------------------------------------------------------------


def _migration_001(connection: sqlite3.Connection) -> None:
    """Create the initial schema."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            prompt TEXT NOT NULL,
            owner_uid INTEGER NOT NULL,
            owner_name TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            workspace TEXT NOT NULL DEFAULT '',
            runtime_id TEXT NOT NULL DEFAULT 'mock',
            session_id TEXT,
            level_cap INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            finished_at TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            side_effects_possible INTEGER NOT NULL DEFAULT 0,
            result_summary TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS tasks_state_idx ON tasks (state, priority DESC, created_at)")
    connection.execute("CREATE INDEX IF NOT EXISTS tasks_owner_idx ON tasks (owner_uid, created_at DESC)")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS task_events (
            task_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            payload_digest TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (task_id, sequence),
            FOREIGN KEY (task_id) REFERENCES tasks (task_id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS approvals (
            approval_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            target TEXT,
            parameter_digest TEXT NOT NULL,
            parameters_json TEXT NOT NULL DEFAULT '{}',
            level INTEGER NOT NULL,
            state TEXT NOT NULL,
            nonce TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            decided_at TEXT,
            decided_by_uid INTEGER,
            decided_by_name TEXT NOT NULL DEFAULT '',
            decision_note TEXT NOT NULL DEFAULT '',
            consumed_at TEXT,
            consumed_by_task TEXT,
            requested_by_uid INTEGER NOT NULL,
            requested_by_name TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS approvals_state_idx ON approvals (state, created_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS approvals_task_idx ON approvals (task_id)")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS audit (
            record_id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            kind TEXT NOT NULL,
            action TEXT NOT NULL DEFAULT '',
            task_id TEXT,
            actor_uid INTEGER,
            actor_name TEXT NOT NULL DEFAULT '',
            operation TEXT,
            target TEXT,
            decision TEXT,
            outcome TEXT NOT NULL DEFAULT 'ok',
            reason TEXT NOT NULL DEFAULT '',
            parameter_digest TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS audit_task_idx ON audit (task_id, timestamp DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS audit_kind_idx ON audit (kind, timestamp DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS audit_timestamp_idx ON audit (timestamp DESC)")


#: Registered migrations keyed by target schema version.
_MIGRATIONS: Mapping[int, Any] = {1: _migration_001}


__all__ = [
    "MAX_RESULT_BYTES",
    "Storage",
    "TaskEventRecord",
    "TaskRecord",
    "store_digest",
]
