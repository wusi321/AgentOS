"""Controlled executor for the small set of side-effecting operations AgentOS allows.

This module is the only place in AgentOS that changes system state. It is
deliberately narrow: it accepts structured operation identifiers and
schema-validated parameters, never a command string, and it re-verifies the
policy authorization immediately before acting.

The first release implements two operations:

``system.service.restart``
    Restart one unit named in the policy allowlist.

``system.process.terminate``
    Send ``SIGTERM`` to one process the requesting user owns.
"""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass, field
from typing import Any, Final

from ..errors import PolicyDenied, SystemBridgeError, ValidationError
from ..policy import ExecutionAuthorization, PolicyContext
from .processes import find_process
from .services import HelperResult, restart_unit

#: Process ids that may never be signalled.
_FORBIDDEN_PIDS: Final[frozenset[int]] = frozenset({0, 1})

#: Operations the executor implements.
EXECUTABLE_OPERATIONS: Final[frozenset[str]] = frozenset(
    {"system.service.restart", "system.process.terminate"}
)


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """Result of one controlled operation.

    :param operation: Operation identifier that ran.
    :param target: Object acted on.
    :param success: Whether the operation completed without error.
    :param exit_code: Helper exit status, when a helper ran.
    :param message: Operator-facing result explanation.
    :param duration_seconds: Wall-clock duration of the attempt.
    :param side_effects_possible: Whether system state may have changed. Set
        before the attempt so that crash recovery can see it.

    Note: the executor never reports an unknown outcome. An operation that
    returned an error after starting is reported as failed with the helper's own
    message, so the caller can decide whether to re-run it.
    """

    operation: str
    target: str
    success: bool
    exit_code: int | None = None
    message: str = ""
    duration_seconds: float = 0.0
    side_effects_possible: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable description of this outcome."""
        return {
            "operation": self.operation,
            "target": self.target,
            "success": self.success,
            "exit_code": self.exit_code,
            "message": self.message,
            "duration_seconds": round(self.duration_seconds, 3),
            "side_effects_possible": self.side_effects_possible,
            "details": self.details,
        }


class ControlledExecutor:
    """Execute allowlisted operations after an authorization check.

    :param context: Caller facts used for the re-check and for ownership tests.
    """

    def __init__(self, *, context: PolicyContext) -> None:
        self._context = context

    @property
    def context(self) -> PolicyContext:
        """Return the caller facts bound to this executor."""
        return self._context

    def execute(
        self,
        operation: str,
        parameters: dict[str, Any],
        authorization: ExecutionAuthorization,
    ) -> ExecutionOutcome:
        """Run one authorized operation.

        :param operation: Operation identifier.
        :param parameters: Validated parameters as normalized by policy.
        :param authorization: Proof produced by the policy engine for exactly
            these parameters. A mismatch raises rather than proceeding.
        :raises PolicyDenied: When the authorization does not cover this call.
        :raises ValidationError: When the operation is unknown.
        """
        if operation not in EXECUTABLE_OPERATIONS:
            raise ValidationError(
                f"no controlled executor implements {operation!r}",
                details={"operation": operation, "implemented": sorted(EXECUTABLE_OPERATIONS)},
            )
        evaluation = authorization.evaluation
        if evaluation.tool != operation:
            raise PolicyDenied(
                "the authorization does not cover this operation",
                details={"authorized": evaluation.tool, "requested": operation},
            )
        authorized_parameters = dict(evaluation.parameters)
        if authorized_parameters != dict(parameters):
            raise PolicyDenied(
                "the authorization does not cover these parameters",
                details={"authorized_digest": evaluation.tool, "requested_digest": operation},
            )
        if operation == "system.service.restart":
            return self._restart_unit(str(parameters["unit"]))
        return self._terminate_process(int(parameters["pid"]), str(parameters.get("reason", "")))

    # -- operations --------------------------------------------------------

    def _restart_unit(self, unit: str) -> ExecutionOutcome:
        """Restart one allowlisted unit and verify the resulting state."""
        started = time.monotonic()
        try:
            result: HelperResult = restart_unit(unit)
        except SystemBridgeError as error:
            return ExecutionOutcome(
                operation="system.service.restart",
                target=unit,
                success=False,
                message=error.message,
                duration_seconds=time.monotonic() - started,
                side_effects_possible=True,
            )
        from .services import service_status

        try:
            status = service_status(unit)
            observed = status.active_state
        except SystemBridgeError:
            observed = "unknown"
        success = result.ok
        return ExecutionOutcome(
            operation="system.service.restart",
            target=unit,
            success=success,
            exit_code=result.return_code,
            message=(
                f"systemctl restart {unit} succeeded; observed ActiveState={observed}"
                if success
                else f"systemctl restart {unit} exited {result.return_code}: {result.stderr[-512:]}"
            ),
            duration_seconds=time.monotonic() - started,
            side_effects_possible=True,
            details={"active_state": observed, "argv": list(result.argv)},
        )

    def _terminate_process(self, pid: int, reason: str) -> ExecutionOutcome:
        """Send ``SIGTERM`` to one process owned by the requesting user."""
        started = time.monotonic()
        if pid in _FORBIDDEN_PIDS:
            return ExecutionOutcome(
                operation="system.process.terminate",
                target=str(pid),
                success=False,
                message="signalling pid 1 or pid 0 is never permitted",
            )
        if pid == os.getpid():
            return ExecutionOutcome(
                operation="system.process.terminate",
                target=str(pid),
                success=False,
                message="the service never signals itself",
            )
        sample = find_process(pid)
        if sample is None:
            return ExecutionOutcome(
                operation="system.process.terminate",
                target=str(pid),
                success=False,
                message=f"process {pid} does not exist",
            )
        if self._context.uid != 0 and sample.uid not in {self._context.uid, -1}:
            return ExecutionOutcome(
                operation="system.process.terminate",
                target=str(pid),
                success=False,
                message=(
                    f"process {pid} belongs to uid {sample.uid}, not to uid {self._context.uid}; "
                    "AgentOS signals only processes the requesting user owns"
                ),
                details={"owner_uid": sample.uid},
            )
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return ExecutionOutcome(
                operation="system.process.terminate",
                target=str(pid),
                success=False,
                message=f"process {pid} exited before the signal was delivered",
            )
        except PermissionError as error:
            return ExecutionOutcome(
                operation="system.process.terminate",
                target=str(pid),
                success=False,
                message=f"permission denied signalling {pid}: {error}",
            )
        except OSError as error:
            return ExecutionOutcome(
                operation="system.process.terminate",
                target=str(pid),
                success=False,
                message=f"cannot signal {pid}: {error}",
            )
        return ExecutionOutcome(
            operation="system.process.terminate",
            target=str(pid),
            success=True,
            exit_code=0,
            message=f"SIGTERM delivered to {pid} ({sample.name}); reason: {reason or 'not stated'}",
            duration_seconds=time.monotonic() - started,
            side_effects_possible=True,
            details={"process_name": sample.name},
        )


__all__ = ["EXECUTABLE_OPERATIONS", "ControlledExecutor", "ExecutionOutcome"]
