"""Approval records: the durable, single-use authorization for one action.

An approval binds five facts together: which operation, on which target, with
which exact parameters, requested by which user, until when. Any change to those
facts invalidates it. That binding is what stops a model from obtaining a broad
grant and then redirecting it.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from collections.abc import Mapping
from typing import Any, Final

from ..errors import ApprovalInvalid, ValidationError
from ..events import format_timestamp, parse_timestamp, utc_now
from ..jsonutil import bounded_payload, digest

#: Default validity window for one approval.
DEFAULT_APPROVAL_TTL_SECONDS: Final = 900
#: Upper bound a caller may request, whatever the configuration says.
MAX_APPROVAL_TTL_SECONDS: Final = 3600

#: Lifecycle states an approval moves through.
APPROVAL_STATES: Final[frozenset[str]] = frozenset({"pending", "granted", "denied", "expired", "consumed", "revoked"})


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """The facts a human is asked to authorize.

    :param task_id: Owning task.
    :param operation: Tool name that will run.
    :param target: The single object acted on, when the tool declares one.
    :param parameters: Exact normalized parameters; the digest is derived from these.
    :param level: Required privilege level.
    :param requested_by_uid: Local user who initiated the task.
    :param requested_by_name: Display name of that user, when known.
    :param reason: Human-readable explanation of the proposed effect.
    """

    task_id: str
    operation: str
    target: str | None
    parameters: Mapping[str, Any]
    level: int
    requested_by_uid: int
    requested_by_name: str = ""
    reason: str = ""

    @property
    def parameter_digest(self) -> str:
        """Return the canonical digest of the normalized parameters."""
        return digest(dict(self.parameters))

    def summary(self) -> dict[str, Any]:
        """Return the operator-facing description shown in the approval UI."""
        return {
            "task_id": self.task_id,
            "operation": self.operation,
            "target": self.target,
            "parameter_digest": self.parameter_digest,
            "parameters": bounded_payload(dict(self.parameters), limit=2048),
            "level": self.level,
            "requested_by_uid": self.requested_by_uid,
            "requested_by_name": self.requested_by_name,
            "reason": self.reason,
            "effect": _effect_statement(self.operation, self.target),
        }


def _effect_statement(operation: str, target: str | None) -> str:
    """Return a plain statement of what granting this approval causes."""
    if operation == "system.service.restart":
        return f"Restart the systemd unit {target!r} now."
    if operation == "system.process.terminate":
        return f"Send SIGTERM to process {target!r}. Unsaved state in that process is lost."
    return f"Run {operation!r}" + (f" on {target!r}" if target else "") + "."


@dataclass(frozen=True, slots=True)
class Approval:
    """One durable approval record.

    :param approval_id: Stable identity shown to the user.
    :param request: The authorized facts.
    :param state: Member of :data:`APPROVAL_STATES`.
    :param nonce: Single-use secret that must accompany the execution call.
    :param created_at: Creation timestamp (RFC 3339, UTC).
    :param expires_at: Expiry timestamp; the request is invalid at or after it.
    :param decided_at: When a human decided, when they have.
    :param decided_by_uid: Local user who decided, when they have.
    :param decided_by_name: Display name of that user, when known.
    :param decision_note: Optional human note.
    :param consumed_at: When the approval was used.
    :param consumed_by_task: Task that consumed it.
    """

    approval_id: str
    request: ApprovalRequest
    state: str = "pending"
    nonce: str = ""
    created_at: str = ""
    expires_at: str = ""
    decided_at: str | None = None
    decided_by_uid: int | None = None
    decided_by_name: str = ""
    decision_note: str = ""
    consumed_at: str | None = None
    consumed_by_task: str | None = None

    def __post_init__(self) -> None:
        if self.state not in APPROVAL_STATES:
            raise ValidationError(
                f"unknown approval state {self.state!r}",
                details={"state": self.state, "known": sorted(APPROVAL_STATES)},
            )
        if not self.approval_id:
            object.__setattr__(self, "approval_id", str(uuid.uuid4()))
        if not self.nonce:
            object.__setattr__(self, "nonce", secrets.token_urlsafe(24))
        if not self.created_at:
            object.__setattr__(self, "created_at", format_timestamp(utc_now()))
        if not self.expires_at:
            object.__setattr__(
                self, "expires_at", format_timestamp(utc_now() + timedelta(seconds=DEFAULT_APPROVAL_TTL_SECONDS))
            )

    @property
    def is_expired(self) -> bool:
        """Return whether the approval has passed its expiry instant."""
        return parse_timestamp(self.expires_at) <= utc_now()

    @property
    def is_usable(self) -> bool:
        """Return whether the approval may authorize an execution right now."""
        return self.state == "granted" and not self.is_expired

    def check_binding(self, request: ApprovalRequest, *, nonce: str, now: datetime | None = None) -> None:
        """Verify that this approval authorizes exactly ``request``.

        :param request: The action about to be executed.
        :param nonce: Nonce presented by the executor.
        :param now: Evaluation instant; defaults to the current time.
        :raises ApprovalInvalid: On any mismatch, expiry, or reuse.
        """
        moment = now or utc_now()
        if self.state == "consumed":
            raise ApprovalInvalid(
                "this approval has already been used",
                details={"approval_id": self.approval_id, "consumed_at": self.consumed_at},
            )
        if self.state == "denied":
            raise ApprovalInvalid("this approval was denied", details={"approval_id": self.approval_id})
        if self.state == "expired" or parse_timestamp(self.expires_at) <= moment:
            raise ApprovalInvalid(
                "this approval has expired",
                details={"approval_id": self.approval_id, "expires_at": self.expires_at},
            )
        if self.state != "granted":
            raise ApprovalInvalid(
                f"this approval is {self.state}, not granted",
                details={"approval_id": self.approval_id, "state": self.state},
            )
        if not secrets.compare_digest(self.nonce, nonce):
            raise ApprovalInvalid(
                "the approval nonce does not match",
                details={"approval_id": self.approval_id},
            )
        if self.request.operation != request.operation:
            raise ApprovalInvalid(
                f"this approval authorizes {self.request.operation!r}, not {request.operation!r}",
                details={"approval_id": self.approval_id, "approved_operation": self.request.operation},
            )
        if (self.request.target or None) != (request.target or None):
            raise ApprovalInvalid(
                "the approval target does not match the requested target",
                details={
                    "approval_id": self.approval_id,
                    "approved_target": self.request.target,
                    "requested_target": request.target,
                },
            )
        if self.request.parameter_digest != request.parameter_digest:
            raise ApprovalInvalid(
                "the approval parameters do not match the requested parameters",
                details={
                    "approval_id": self.approval_id,
                    "approved_digest": self.request.parameter_digest,
                    "requested_digest": request.parameter_digest,
                },
            )
        if self.request.requested_by_uid != request.requested_by_uid:
            raise ApprovalInvalid(
                "the approval belongs to a different user",
                details={"approval_id": self.approval_id},
            )
        if self.request.task_id != request.task_id:
            raise ApprovalInvalid(
                "the approval belongs to a different task",
                details={"approval_id": self.approval_id, "approved_task": self.request.task_id},
            )

    def to_dict(self, *, include_nonce: bool = False) -> dict[str, Any]:
        """Return the wire representation, withholding the nonce by default."""
        document: dict[str, Any] = {
            "approval_id": self.approval_id,
            "state": self.state,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "decided_at": self.decided_at,
            "decided_by_uid": self.decided_by_uid,
            "decided_by_name": self.decided_by_name,
            "decision_note": self.decision_note,
            "consumed_at": self.consumed_at,
            "consumed_by_task": self.consumed_by_task,
            **self.request.summary(),
        }
        if include_nonce:
            document["nonce"] = self.nonce
        return document


__all__ = [
    "APPROVAL_STATES",
    "DEFAULT_APPROVAL_TTL_SECONDS",
    "MAX_APPROVAL_TTL_SECONDS",
    "Approval",
    "ApprovalRequest",
]
