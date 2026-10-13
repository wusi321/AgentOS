"""The System Bridge facade: one entry point for reads and controlled writes.

The bridge is the only component that knows both the system collectors and the
policy engine. A read tool is dispatched directly; a write tool is evaluated by
policy, an approval is requested when the decision demands one, and the executor
receives a proof object rather than raw parameters.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from collections.abc import Callable, Mapping
from typing import Any, Final

from ..errors import CapabilityUnsupported, PolicyDenied, SystemBridgeError, ValidationError
from ..policy import (
    Approval,
    ApprovalRequest,
    Decision,
    ExecutionAuthorization,
    PolicyContext,
    PolicyEngine,
    PrivilegeLevel,
)
from .executor import EXECUTABLE_OPERATIONS, ControlledExecutor, ExecutionOutcome
from .metrics import CpuSample, read_cpu_sample, read_filesystems, read_load_average, read_memory_sample, read_uptime
from .processes import ProcessSample, cpu_counter_snapshot, find_process, list_processes
from .services import JournalEntry, ServiceStatus, list_services, read_journal, service_status

#: Capability levels each read tool requires; every entry is P0.
READ_TOOL_LEVELS: Final[Mapping[str, PrivilegeLevel]] = {
    "system.metrics.read": PrivilegeLevel.READ_ONLY,
    "system.processes.list": PrivilegeLevel.READ_ONLY,
    "system.process.inspect": PrivilegeLevel.READ_ONLY,
    "system.disks.read": PrivilegeLevel.READ_ONLY,
    "system.uptime.read": PrivilegeLevel.READ_ONLY,
    "system.services.list": PrivilegeLevel.READ_ONLY,
    "system.service.status": PrivilegeLevel.READ_ONLY,
    "system.logs.read": PrivilegeLevel.READ_ONLY,
}

#: Tools the bridge treats as writes and hands to the executor.
WRITE_TOOLS: Final[frozenset[str]] = EXECUTABLE_OPERATIONS

ApprovalResolver = Callable[[ApprovalRequest], Approval | None]
AuditSink = Callable[[str, Mapping[str, Any]], None]


@dataclass(slots=True)
class SystemSnapshot:
    """A consistent set of read-only system facts.

    :param cpu: CPU sample; ``utilised_percent`` is present only with a prior sample.
    :param memory: Memory and swap sample.
    :param filesystems: Capacity per real mounted filesystem.
    :param load_average: One, five, and fifteen minute load averages.
    :param uptime_seconds: Seconds since boot.
    :param process_count: Number of visible processes.
    :param top_processes: Highest-CPU processes at snapshot time.
    """

    cpu: CpuSample
    memory: Any
    filesystems: list[Any] = field(default_factory=list)
    load_average: tuple[float, float, float] = (0.0, 0.0, 0.0)
    uptime_seconds: float = 0.0
    process_count: int = 0
    top_processes: list[ProcessSample] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable description of this snapshot."""
        return {
            "cpu": self.cpu.to_dict(),
            "memory": self.memory.to_dict(),
            "filesystems": [sample.to_dict() for sample in self.filesystems],
            "load_average": {
                "1m": round(self.load_average[0], 3),
                "5m": round(self.load_average[1], 3),
                "15m": round(self.load_average[2], 3),
            },
            "uptime_seconds": round(self.uptime_seconds, 1),
            "process_count": self.process_count,
            "top_processes": [sample.to_dict() for sample in self.top_processes],
        }


class SystemBridge:
    """Serve read tools and route write tools through policy and the executor.

    :param policy: Policy engine used for write authorization.
    :param resolve_approval: Callback returning the approval named by the caller,
        or ``None`` when it cannot be found.
    :param audit: Optional sink receiving ``(action, payload)`` for every outcome.
    """

    def __init__(
        self,
        *,
        policy: PolicyEngine,
        resolve_approval: ApprovalResolver | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self._policy = policy
        self._resolve_approval = resolve_approval
        self._audit = audit
        self._previous_cpu: CpuSample | None = None
        self._previous_process_counters: dict[int, float] | None = None
        self._previous_process_time: float | None = None

    @property
    def policy(self) -> PolicyEngine:
        """Return the policy engine this bridge enforces."""
        return self._policy

    def _emit(self, action: str, payload: Mapping[str, Any]) -> None:
        """Send an audit event, containing sink failures."""
        if self._audit is None:
            return
        try:
            self._audit(action, payload)
        except Exception:  # noqa: BLE001 - a broken sink must not fail an operation
            pass

    # -- read tools --------------------------------------------------------

    def snapshot(self) -> SystemSnapshot:
        """Collect a consistent read-only snapshot of system state."""
        cpu = read_cpu_sample(self._previous_cpu)
        self._previous_cpu = cpu
        memory = read_memory_sample()
        filesystems = read_filesystems()
        load = read_load_average()
        uptime, _idle = read_uptime()
        counters = self._previous_process_counters
        elapsed = None if self._previous_process_time is None else max(0.0, _monotonic() - self._previous_process_time)
        processes = list_processes(limit=10, sort_by="cpu", previous_cpu=counters, elapsed_seconds=elapsed)
        self._previous_process_counters = cpu_counter_snapshot(processes)
        self._previous_process_time = _monotonic()
        return SystemSnapshot(
            cpu=cpu,
            memory=memory,
            filesystems=filesystems,
            load_average=load,
            uptime_seconds=uptime,
            process_count=len(os.listdir("/proc")) if os.path.isdir("/proc") else 0,
            top_processes=processes,
        )

    def read(self, tool: str, parameters: Mapping[str, Any] | None = None) -> Any:
        """Run one read-only tool and return its JSON-serializable result.

        :param tool: Read tool name.
        :param parameters: Tool parameters, validated by the capability manifest.
        :raises CapabilityUnsupported: When ``tool`` is not a read tool.
        :raises PolicyDenied: When the manifest denies the tool.
        :raises ValidationError: When parameters are invalid.
        """
        parameters = dict(parameters or {})
        if tool in WRITE_TOOLS:
            raise CapabilityUnsupported(
                f"{tool!r} is a controlled write operation; call execute() with an authorization",
                details={"tool": tool},
            )
        if tool not in READ_TOOL_LEVELS:
            raise CapabilityUnsupported(f"{tool!r} is not a System Bridge read tool", details={"tool": tool})
        manifest = self._policy.manifest(tool)
        if manifest is None:
            raise CapabilityUnsupported(f"{tool!r} has no capability manifest", details={"tool": tool})
        normalized = manifest.validate_parameters(parameters)
        if tool == "system.metrics.read":
            return self.snapshot().to_dict()
        if tool == "system.processes.list":
            counters = self._previous_process_counters
            elapsed = (
                None if self._previous_process_time is None else max(0.0, _monotonic() - self._previous_process_time)
            )
            samples = list_processes(
                limit=int(normalized.get("limit", 25)),
                sort_by=str(normalized.get("sort_by", "cpu")),
                min_cpu_percent=float(normalized.get("min_cpu_percent", 0.0)),
                previous_cpu=counters,
                elapsed_seconds=elapsed,
            )
            self._previous_process_counters = cpu_counter_snapshot(samples)
            self._previous_process_time = _monotonic()
            return {"count": len(samples), "processes": [sample.to_dict() for sample in samples]}
        if tool == "system.process.inspect":
            sample = find_process(int(normalized["pid"]))
            if sample is None:
                return {"found": False, "pid": int(normalized["pid"])}
            return {"found": True, "process": sample.to_dict()}
        if tool == "system.disks.read":
            samples = read_filesystems(include_pseudo=False)
            return {"count": len(samples), "filesystems": [sample.to_dict() for sample in samples]}
        if tool == "system.uptime.read":
            uptime, idle = read_uptime()
            load = read_load_average()
            return {
                "uptime_seconds": round(uptime, 1),
                "idle_seconds": round(idle, 1),
                "load_average": {"1m": load[0], "5m": load[1], "15m": load[2]},
            }
        if tool == "system.services.list":
            statuses = list_services(
                state=str(normalized.get("state", "all")),
                limit=int(normalized.get("limit", 50)),
            )
            return {"count": len(statuses), "units": [status.to_dict() for status in statuses]}
        if tool == "system.service.status":
            status = service_status(str(normalized["unit"]))
            return status.to_dict()
        if tool == "system.logs.read":
            entries = read_journal(
                unit=None if "unit" not in normalized else str(normalized["unit"]),
                priority=None if "priority" not in normalized else int(normalized["priority"]),
                lines=int(normalized.get("lines", 50)),
                since=None if "since" not in normalized else str(normalized["since"]),
            )
            return {"count": len(entries), "entries": [entry.to_dict() for entry in entries]}
        raise CapabilityUnsupported(f"{tool!r} has no System Bridge implementation", details={"tool": tool})

    # -- controlled writes -------------------------------------------------

    def propose(
        self,
        tool: str,
        parameters: Mapping[str, Any],
        context: PolicyContext,
    ) -> dict[str, Any]:
        """Evaluate a write tool without executing it.

        :param tool: Write tool name.
        :param parameters: Proposed parameters.
        :param context: Caller facts.
        :returns: The policy evaluation as a dictionary.
        :raises ValidationError: When ``tool`` is not a controlled operation.
        """
        if tool not in WRITE_TOOLS:
            raise ValidationError(
                f"{tool!r} is not a controlled write operation",
                details={"tool": tool, "implemented": sorted(WRITE_TOOLS)},
            )
        evaluation = self._policy.evaluate(tool, parameters, context)
        self._emit(
            "policy.decision",
            {
                "tool": tool,
                "decision": evaluation.decision.value,
                "reason": evaluation.reason,
                "actor_uid": context.uid,
            },
        )
        return evaluation.to_dict()

    def execute(
        self,
        tool: str,
        parameters: Mapping[str, Any],
        context: PolicyContext,
        *,
        approval_id: str | None = None,
        nonce: str | None = None,
        consume_approval: Callable[[str, str], Approval] | None = None,
    ) -> ExecutionOutcome:
        """Authorize and run one controlled operation.

        :param tool: Write tool name.
        :param parameters: Exact parameters to execute.
        :param context: Caller facts.
        :param approval_id: Approval to present when policy requires one.
        :param nonce: Single-use nonce that came with the approval.
        :param consume_approval: Callback that atomically marks the approval used.
        :raises ValidationError: When the tool is unknown.
        :raises PolicyDenied: When policy denies the operation.
        :raises ApprovalRequired: When an approval is needed but missing.
        :raises ApprovalInvalid: When the presented approval does not match.
        """
        if tool not in WRITE_TOOLS:
            raise ValidationError(
                f"{tool!r} is not a controlled write operation",
                details={"tool": tool, "implemented": sorted(WRITE_TOOLS)},
            )
        approval: Approval | None = None
        if approval_id is not None:
            if self._resolve_approval is None:
                raise ValidationError("no approval resolver is configured")
            approval = self._resolve_approval(approval_id)
            if approval is None:
                raise ValidationError(f"approval {approval_id} does not exist", details={"approval_id": approval_id})
        evaluation = self._policy.evaluate(tool, parameters, context)
        self._emit(
            "policy.decision",
            {
                "tool": tool,
                "decision": evaluation.decision.value,
                "reason": evaluation.reason,
                "actor_uid": context.uid,
                "target": evaluation.target,
            },
        )
        authorization: ExecutionAuthorization = self._policy.authorize_execution(
            tool,
            parameters,
            context,
            approval=approval,
            nonce=nonce,
        )
        if authorization.approval is not None and consume_approval is not None:
            consume_approval(authorization.approval.approval_id, context.task_id or "")
        executor = ControlledExecutor(context=context)
        outcome = executor.execute(tool, dict(parameters), authorization)
        self._emit(
            "tool.executed" if outcome.success else "tool.failed",
            {
                "tool": tool,
                "target": outcome.target,
                "success": outcome.success,
                "message": outcome.message,
                "actor_uid": context.uid,
                "approval_id": None if authorization.approval is None else authorization.approval.approval_id,
            },
        )
        return outcome


def _monotonic() -> float:
    """Indirection that keeps the sampling clock in one place for tests."""
    import time

    return time.monotonic()


__all__ = ["READ_TOOL_LEVELS", "WRITE_TOOLS", "SystemBridge", "SystemSnapshot"]
