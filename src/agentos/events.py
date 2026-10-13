"""Versioned AgentOS event envelope and payload helpers.

All core modules publish and consume one event format. The envelope is stable,
validated on construction, and carries a sensitivity marker so that logging and
audit retention can treat classified events differently.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from collections.abc import Mapping
from typing import Any, Final

from .constants import EVENT_SCHEMA_VERSION, PROJECT_SLUG
from .errors import ValidationError
from .jsonutil import bounded_payload, canonical_json, is_json_value, redact

#: Event type vocabulary. Values are the wire strings; they never change meaning.
EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "task.created",
        "task.state_changed",
        "task.completed",
        "task.failed",
        "task.cancelled",
        "runtime.session_created",
        "runtime.message_delta",
        "runtime.message_completed",
        "runtime.tool_call_started",
        "runtime.tool_call_completed",
        "runtime.warning",
        "runtime.error",
        "runtime.exited",
        "policy.decision",
        "approval.requested",
        "approval.granted",
        "approval.denied",
        "approval.expired",
        "approval.consumed",
        "system.metric_sample",
        "system.threshold_exceeded",
        "system.service_state",
        "audit.note",
    }
)


class Severity(str, Enum):
    """Event severity, ordered from least to most urgent."""

    DEBUG = "debug"
    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        """Return a comparable rank for threshold filtering."""
        return _SEVERITY_RANK[self]


_SEVERITY_RANK: Final[dict[Severity, int]] = {
    Severity.DEBUG: 0,
    Severity.INFO: 1,
    Severity.NOTICE: 2,
    Severity.WARNING: 3,
    Severity.ERROR: 4,
    Severity.CRITICAL: 5,
}


class Sensitivity(str, Enum):
    """Classification controlling redaction and retention."""

    NORMAL = "normal"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def format_timestamp(moment: datetime) -> str:
    """Render ``moment`` as an RFC 3339 timestamp in UTC."""
    if moment.tzinfo is None:
        raise ValidationError("timestamps must be timezone-aware")
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(text: str) -> datetime:
    """Parse an RFC 3339 timestamp produced by :func:`format_timestamp`."""
    if not isinstance(text, str):
        raise ValidationError("timestamp must be a string")
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ValidationError(f"invalid RFC 3339 timestamp: {text!r}") from error
    if parsed.tzinfo is None:
        raise ValidationError(f"timestamp lacks a timezone offset: {text!r}")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Event:
    """One versioned AgentOS event.

    :param event_type: Member of :data:`EVENT_TYPES`.
    :param payload: Event-specific body; redacted and size-bounded on creation.
    :param event_id: Stable identity, generated when omitted.
    :param timestamp: Creation time; defaults to now.
    :param source: Producing component, for example ``agentos-core``.
    :param task_id: Owning task, when the event belongs to one.
    :param session_id: Owning agent session, when the event belongs to one.
    :param severity: How urgently a consumer should surface the event.
    :param sensitivity: Classification controlling redaction and retention.
    :param schema_version: Envelope version, fixed unless a migration says otherwise.
    """

    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = ""
    timestamp: str = ""
    source: str = PROJECT_SLUG + "-core"
    task_id: str | None = None
    session_id: str | None = None
    severity: Severity = Severity.INFO
    sensitivity: Sensitivity = Sensitivity.NORMAL
    schema_version: int = EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.event_type not in EVENT_TYPES:
            raise ValidationError(
                f"unknown event_type {self.event_type!r}",
                details={"event_type": self.event_type, "known": sorted(EVENT_TYPES)},
            )
        if self.schema_version != EVENT_SCHEMA_VERSION:
            raise ValidationError(
                f"unsupported event schema_version {self.schema_version}",
                details={"supported": EVENT_SCHEMA_VERSION},
            )
        if not isinstance(self.severity, Severity):
            object.__setattr__(self, "severity", Severity(self.severity))
        if not isinstance(self.sensitivity, Sensitivity):
            object.__setattr__(self, "sensitivity", Sensitivity(self.sensitivity))
        if not isinstance(self.payload, Mapping):
            raise ValidationError("event payload must be a mapping")
        if not is_json_value(self.payload):
            raise ValidationError(
                "event payload is not JSON-serializable",
                details={"event_type": self.event_type},
            )
        if not self.event_id:
            object.__setattr__(self, "event_id", str(uuid.uuid4()))
        if not self.timestamp:
            object.__setattr__(self, "timestamp", format_timestamp(utc_now()))

    @property
    def redacted_payload(self) -> dict[str, Any]:
        """Return the payload with sensitive members removed and size bounded."""
        return bounded_payload(self.payload)

    def to_dict(self) -> dict[str, Any]:
        """Return the wire representation of this event."""
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "source": self.source,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "severity": self.severity.value,
            "sensitivity": self.sensitivity.value,
            "payload": self.redacted_payload,
        }

    def to_json(self) -> str:
        """Return the canonical JSON encoding of this event."""
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Event":
        """Build an event from an untrusted mapping, validating every member.

        :param data: Decoded JSON object received over IPC or read from storage.
        :raises ValidationError: When a required member is missing or mistyped.
        """
        if not isinstance(data, Mapping):
            raise ValidationError("event must be a JSON object")
        required = ("schema_version", "event_id", "event_type", "timestamp", "source", "severity", "payload")
        missing = [key for key in required if key not in data]
        if missing:
            raise ValidationError(
                "event is missing required member(s): " + ", ".join(missing),
                details={"missing": missing},
            )
        for key in ("event_id", "event_type", "timestamp", "source"):
            if not isinstance(data[key], str):
                raise ValidationError(f"event.{key} must be a string", details={"member": key})
        for key in ("task_id", "session_id"):
            if data.get(key) is not None and not isinstance(data[key], str):
                raise ValidationError(f"event.{key} must be a string or null", details={"member": key})
        return cls(
            event_type=data["event_type"],
            payload=redact(data["payload"]),
            event_id=data["event_id"],
            timestamp=data["timestamp"],
            source=data["source"],
            task_id=data.get("task_id"),
            session_id=data.get("session_id"),
            severity=Severity(data["severity"]),
            sensitivity=Sensitivity(data.get("sensitivity", Sensitivity.NORMAL.value)),
            schema_version=int(data["schema_version"]),
        )


__all__ = [
    "EVENT_TYPES",
    "Event",
    "Sensitivity",
    "Severity",
    "format_timestamp",
    "parse_timestamp",
    "utc_now",
]
