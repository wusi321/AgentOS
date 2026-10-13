"""Audit store: append-only records of decisions, executions, and out-of-band notes.

The audit trail answers "who asked for what, what did policy decide, what ran,
and what happened". It is append-only by contract: there is no update or delete
API, and retention is a separate explicit operation.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, Final

from ..errors import StorageError, ValidationError
from ..events import format_timestamp, parse_timestamp, utc_now
from ..jsonutil import bounded_payload, digest, truncate_text

#: Audit record kinds.
AUDIT_KINDS: Final[frozenset[str]] = frozenset(
    {
        "task.created",
        "task.state_changed",
        "task.cancelled",
        "policy.decision",
        "approval.requested",
        "approval.decided",
        "approval.consumed",
        "tool.executed",
        "tool.failed",
        "runtime.event",
        "ipc.request",
        "ipc.denied",
        "system.note",
        "config.reloaded",
    }
)

#: Maximum payload bytes retained for one audit record.
MAX_AUDIT_PAYLOAD_BYTES: Final = 16 * 1024


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One immutable audit entry.

    :param kind: Member of :data:`AUDIT_KINDS`.
    :param action: Short verb phrase, for example ``restart``.
    :param record_id: Stable identity, generated when omitted.
    :param timestamp: Creation timestamp (RFC 3339, UTC).
    :param task_id: Owning task, when the record belongs to one.
    :param actor_uid: Local user the action is attributed to, when known.
    :param actor_name: Display name of that user, when known.
    :param operation: Tool or IPC method name.
    :param target: Action target.
    :param decision: Policy decision, when the record reports one.
    :param outcome: ``ok``, ``denied``, ``error``, or ``pending``.
    :param reason: Operator-facing explanation.
    :param parameter_digest: Digest of the parameters involved.
    :param payload: Additional structured context, redacted before storage.
    """

    kind: str
    action: str
    record_id: str = ""
    timestamp: str = ""
    task_id: str | None = None
    actor_uid: int | None = None
    actor_name: str = ""
    operation: str | None = None
    target: str | None = None
    decision: str | None = None
    outcome: str = "ok"
    reason: str = ""
    parameter_digest: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in AUDIT_KINDS:
            raise ValidationError(
                f"unknown audit kind {self.kind!r}",
                details={"kind": self.kind, "known": sorted(AUDIT_KINDS)},
            )
        if not self.record_id:
            object.__setattr__(self, "record_id", str(uuid.uuid4()))
        if not self.timestamp:
            object.__setattr__(self, "timestamp", format_timestamp(utc_now()))
        if self.actor_uid is not None and (isinstance(self.actor_uid, bool) or self.actor_uid < 0):
            raise ValidationError("audit actor_uid must be a non-negative integer")

    def to_dict(self) -> dict[str, Any]:
        """Return the wire representation with the payload redacted and bounded."""
        return {
            "record_id": self.record_id,
            "timestamp": self.timestamp,
            "kind": self.kind,
            "action": self.action,
            "task_id": self.task_id,
            "actor_uid": self.actor_uid,
            "actor_name": self.actor_name,
            "operation": self.operation,
            "target": self.target,
            "decision": self.decision,
            "outcome": self.outcome,
            "reason": truncate_text(self.reason, limit=1024),
            "parameter_digest": self.parameter_digest,
            "payload": bounded_payload(dict(self.payload), limit=MAX_AUDIT_PAYLOAD_BYTES),
        }


class AuditStore:
    """Append-only audit storage backed by SQLite.

    :param connection: Open connection whose schema already contains ``audit``.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def record(self, entry: AuditRecord) -> str:
        """Append ``entry`` and return its record id.

        :raises StorageError: When the insert fails.
        """
        document = entry.to_dict()
        import json

        try:
            self._connection.execute(
                """
                INSERT INTO audit (
                    record_id, timestamp, kind, action, task_id, actor_uid, actor_name,
                    operation, target, decision, outcome, reason, parameter_digest, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document["record_id"],
                    parse_timestamp(document["timestamp"]).isoformat(),
                    document["kind"],
                    document["action"],
                    document["task_id"],
                    document["actor_uid"],
                    document["actor_name"],
                    document["operation"],
                    document["target"],
                    document["decision"],
                    document["outcome"],
                    document["reason"],
                    document["parameter_digest"],
                    json.dumps(document["payload"], ensure_ascii=False, sort_keys=True),
                ),
            )
            self._connection.commit()
        except sqlite3.Error as error:
            raise StorageError(f"cannot append audit record: {error}", details={"kind": entry.kind}) from error
        return document["record_id"]

    def query(
        self,
        *,
        task_id: str | None = None,
        kind: str | None = None,
        since: str | None = None,
        until: str | None = None,
        operation: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return audit records newest first.

        :param task_id: Restrict to one task.
        :param kind: Restrict to one record kind.
        :param since: Inclusive lower bound, RFC 3339.
        :param until: Exclusive upper bound, RFC 3339.
        :param operation: Restrict to one operation name.
        :param limit: Maximum records returned, capped at 1000.
        :param offset: Records to skip.
        :raises ValidationError: On an out-of-range limit or malformed timestamp.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValidationError("audit query limit must be between 1 and 1000")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValidationError("audit query offset must be a non-negative integer")
        clauses: list[str] = []
        values: list[Any] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            values.append(task_id)
        if kind is not None:
            if kind not in AUDIT_KINDS:
                raise ValidationError(f"unknown audit kind {kind!r}", details={"known": sorted(AUDIT_KINDS)})
            clauses.append("kind = ?")
            values.append(kind)
        if operation is not None:
            clauses.append("operation = ?")
            values.append(operation)
        if since is not None:
            clauses.append("timestamp >= ?")
            values.append(parse_timestamp(since).isoformat())
        if until is not None:
            clauses.append("timestamp < ?")
            values.append(parse_timestamp(until).isoformat())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT record_id, timestamp, kind, action, task_id, actor_uid, actor_name, operation,"
            " target, decision, outcome, reason, parameter_digest, payload_json"
            f" FROM audit{where} ORDER BY timestamp DESC, rowid DESC LIMIT ? OFFSET ?"
        )
        values.extend([limit, offset])
        import json

        try:
            rows = self._connection.execute(sql, values).fetchall()
        except sqlite3.Error as error:
            raise StorageError(f"audit query failed: {error}") from error
        records: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row[13]) if row[13] else {}
            except json.JSONDecodeError:
                payload = {"unreadable": True}
            records.append(
                {
                    "record_id": row[0],
                    "timestamp": row[1],
                    "kind": row[2],
                    "action": row[3],
                    "task_id": row[4],
                    "actor_uid": row[5],
                    "actor_name": row[6],
                    "operation": row[7],
                    "target": row[8],
                    "decision": row[9],
                    "outcome": row[10],
                    "reason": row[11],
                    "parameter_digest": row[12],
                    "payload": payload,
                }
            )
        return records

    def count(self) -> int:
        """Return the number of retained audit records."""
        try:
            row = self._connection.execute("SELECT COUNT(*) FROM audit").fetchone()
        except sqlite3.Error as error:
            raise StorageError(f"audit count failed: {error}") from error
        return int(row[0]) if row else 0

    def summary(self, *, task_id: str | None = None) -> dict[str, Any]:
        """Return counts by kind and outcome, for the Activity view."""
        clauses = " WHERE task_id = ?" if task_id else ""
        values = (task_id,) if task_id else ()
        try:
            kinds = self._connection.execute(
                f"SELECT kind, COUNT(*) FROM audit{clauses} GROUP BY kind ORDER BY COUNT(*) DESC", values
            ).fetchall()
            outcomes = self._connection.execute(
                f"SELECT outcome, COUNT(*) FROM audit{clauses} GROUP BY outcome", values
            ).fetchall()
        except sqlite3.Error as error:
            raise StorageError(f"audit summary failed: {error}") from error
        return {
            "by_kind": {row[0]: row[1] for row in kinds},
            "by_outcome": {row[0]: row[1] for row in outcomes},
            "total": sum(row[1] for row in kinds),
        }

    def prune(self, *, retain_days: int, max_records: int) -> int:
        """Delete records past retention or beyond a capacity bound.

        :param retain_days: Records older than this many days are removed.
        :param max_records: Hard capacity bound; oldest records beyond it are removed.
        :returns: Number of removed records.
        :raises ValidationError: On a non-positive bound.
        """
        if isinstance(retain_days, bool) or not isinstance(retain_days, int) or retain_days < 1:
            raise ValidationError("retain_days must be a positive integer")
        if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records < 1:
            raise ValidationError("max_records must be a positive integer")
        from datetime import timedelta

        cutoff = format_timestamp(utc_now() - timedelta(days=retain_days))
        removed = 0
        try:
            cursor = self._connection.execute(
                "DELETE FROM audit WHERE timestamp < ?", (parse_timestamp(cutoff).isoformat(),)
            )
            removed += cursor.rowcount if cursor.rowcount > 0 else 0
            cursor = self._connection.execute(
                "DELETE FROM audit WHERE record_id IN ("
                " SELECT record_id FROM audit ORDER BY timestamp DESC, rowid DESC LIMIT -1 OFFSET ?"
                ")",
                (max_records,),
            )
            removed += cursor.rowcount if cursor.rowcount > 0 else 0
            self._connection.commit()
        except sqlite3.Error as error:
            raise StorageError(f"audit prune failed: {error}") from error
        return removed


def payload_digest(payload: Mapping[str, Any]) -> str:
    """Return the digest of a payload, for correlating audit rows with events."""
    return digest(dict(payload))


__all__ = ["AUDIT_KINDS", "MAX_AUDIT_PAYLOAD_BYTES", "AuditRecord", "AuditStore", "payload_digest"]
