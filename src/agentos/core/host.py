"""The Core service host: configuration, storage, runtime, policy, and IPC wiring.

``CoreHost`` is what the systemd unit starts. It owns exactly one of each
singleton — storage, runtime adapter, policy engine, task manager, audit store —
and exposes them over IPC. Handlers here own authorization: every task and
approval carries an owner, and a peer may act only on its own objects.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, Final

from ..audit import AuditRecord, AuditStore
from ..constants import PROJECT_SLUG, PROJECT_VERSION
from ..errors import (
    AgentOSError,
    IPCAccessDenied,
    PolicyDenied,
    StorageError,
    ValidationError,
)
from ..events import Event, Sensitivity, Severity, format_timestamp, utc_now
from ..policy import (
    Approval,
    ApprovalRequest,
    Decision,
    PolicyContext,
    PolicyEngine,
    PrivilegeLevel,
)
from ..runtime import create_runtime, list_runtimes
from ..runtime.factory import RUNTIME_IDS
from ..system_bridge import SystemBridge, list_services
from ..tasks import Storage, TaskManager, TaskState
from .config import Config, ConfigurationLoader, load_config, read_credential_environment
from .ipc import IpcRequest, IpcServer, new_request_id

#: Maximum events retained per subscriber before the oldest are dropped.
SUBSCRIBER_BACKLOG: Final = 256
#: Maximum subscribers a single peer may hold.
MAX_SUBSCRIBERS_PER_UID: Final = 4


def configure_logging(level: str = "INFO", *, stream: Any = None) -> logging.Logger:
    """Configure the process logger for journald.

    The format is one JSON object per line so that ``journalctl -o json`` and
    plain text both stay readable, and so that a field can be filtered without
    pattern matching prose.

    :param level: Minimum level to emit.
    :param stream: Destination stream; defaults to stderr.
    """
    logger = logging.getLogger(PROJECT_SLUG)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(_JsonLineFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger


class _JsonLineFormatter(logging.Formatter):
    """Render log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the JSON encoding of one log record."""
        document: dict[str, Any] = {
            "ts": format_timestamp(utc_now()),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("task_id", "runtime_id", "actor_uid", "operation", "target", "method"):
            value = getattr(record, key, None)
            if value is not None:
                document[key] = value
        if record.exc_info:
            document["exception"] = self.formatException(record.exc_info)
        return json.dumps(document, ensure_ascii=False)


@dataclass(slots=True)
class Subscriber:
    """One IPC event subscriber.

    :param token: Subscription token handed to the client.
    :param uid: Owner of the subscription.
    :param queue: Pending event documents for this subscriber.
    """

    token: str
    uid: int
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=lambda: asyncio.Queue(maxsize=SUBSCRIBER_BACKLOG))

    def offer(self, document: dict[str, Any]) -> None:
        """Enqueue an event, dropping the oldest when the backlog is full."""
        try:
            self.queue.put_nowait(document)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(document)


class CoreHost:
    """Own every Core singleton and serve the IPC method set.

    :param config: Effective configuration.
    :param logger: Process logger.
    """

    def __init__(self, config: Config, *, logger: logging.Logger | None = None) -> None:
        self._loader = ConfigurationLoader(config)
        self._logger = logger or configure_logging(config.core.log_level)
        self._storage = Storage(config.core.database_path)
        self._audit = AuditStore(self._storage.connection)
        self._policy = PolicyEngine.from_configuration(
            directory=config.policy_directory,
            service_unit_allowlist=config.service_unit_allowlist,
        )
        self._runtime = create_runtime(config.runtime)
        self._bridge = SystemBridge(
            policy=self._policy,
            resolve_approval=self._storage.get_approval,
            audit=self._audit_bridge_event,
        )
        self._tasks = TaskManager(
            storage=self._storage,
            runtime=self._runtime,
            policy=self._policy,
            emit=self._on_event,
            workspace_root=config.core.workspace_root,
            max_concurrent_tasks=config.core.max_concurrent_tasks,
        )
        self._ipc = IpcServer(config.core.socket_path, group="agentos")
        self._subscribers: dict[str, Subscriber] = {}
        self._recent_events: list[dict[str, Any]] = []
        self._started_at = time.time()
        self._shutdown = asyncio.Event()
        self._register_methods()

    # -- accessors ---------------------------------------------------------

    @property
    def config(self) -> Config:
        """Return the active configuration."""
        return self._loader.current

    @property
    def storage(self) -> Storage:
        """Return the durable store."""
        return self._storage

    @property
    def policy(self) -> PolicyEngine:
        """Return the policy engine."""
        return self._policy

    @property
    def tasks(self) -> TaskManager:
        """Return the task manager."""
        return self._tasks

    @property
    def bridge(self) -> SystemBridge:
        """Return the system bridge."""
        return self._bridge

    @property
    def audit(self) -> AuditStore:
        """Return the audit store."""
        return self._audit

    @property
    def logger(self) -> logging.Logger:
        """Return the process logger."""
        return self._logger

    # -- events ------------------------------------------------------------

    def _on_event(self, event: Event) -> None:
        """Record and fan out one AgentOS event."""
        document = event.to_dict()
        self._recent_events.append(document)
        if len(self._recent_events) > 200:
            del self._recent_events[:-200]
        if event.task_id is not None:
            with contextlib.suppress(AgentOSError):
                self._storage.append_task_event(event.task_id, event.event_type, document["payload"])
        for subscriber in list(self._subscribers.values()):
            if event.task_id is None or self._owns_task(subscriber.uid, event.task_id):
                subscriber.offer({"event": document})

    def _audit_bridge_event(self, action: str, payload: Mapping[str, Any]) -> None:
        """Receive System Bridge audit callbacks."""
        try:
            self._audit.record(
                AuditRecord(
                    kind="tool.executed" if action.startswith("tool.") else "policy.decision",
                    action=action,
                    operation=str(payload.get("tool") or "") or None,
                    target=None if payload.get("target") is None else str(payload.get("target")),
                    decision=None if payload.get("decision") is None else str(payload.get("decision")),
                    outcome="ok" if payload.get("success", True) else "error",
                    reason=str(payload.get("message") or payload.get("reason") or ""),
                    actor_uid=None if payload.get("actor_uid") is None else int(payload["actor_uid"]),
                    payload=dict(payload),
                )
            )
        except AgentOSError:
            pass

    def recent_events(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recent events, newest last."""
        return list(self._recent_events[-limit:])

    # -- ownership ---------------------------------------------------------

    def _owns_task(self, uid: int, task_id: str) -> bool:
        """Return whether ``uid`` may act on ``task_id``."""
        if uid == 0:
            return True
        task = self._storage.get_task(task_id)
        return task is not None and task.owner_uid == uid

    def _require_task_access(self, request: IpcRequest, task_id: str) -> None:
        """Raise :class:`IPCAccessDenied` when the peer may not act on a task.

        A task that does not exist is reported the same way as one owned by
        another user, so the IPC surface cannot be used to enumerate tasks.
        """
        if not self._owns_task(request.peer.uid, task_id):
            self._audit.record(
                AuditRecord(
                    kind="ipc.denied",
                    action=request.method,
                    actor_uid=request.peer.uid,
                    operation=request.method,
                    target=task_id,
                    outcome="denied",
                    reason="the task does not exist or belongs to another user",
                )
            )
            raise IPCAccessDenied(
                "the task does not exist or belongs to another user",
                details={"task_id": task_id},
            )

    def _policy_context(self, request: IpcRequest, *, task_id: str | None = None) -> PolicyContext:
        """Build the caller context for one request."""
        cap = PrivilegeLevel.READ_ONLY
        if task_id is not None:
            task = self._storage.get_task(task_id)
            if task is not None:
                cap = task.level_cap
        elif request.is_root:
            cap = PrivilegeLevel.ADMINISTRATIVE
        return PolicyContext(
            uid=request.peer.uid,
            user_name=_user_name(request.peer.uid),
            task_level_cap=cap,
            task_id=task_id,
        )

    # -- method registration ----------------------------------------------

    def _register_methods(self) -> None:
        """Register every IPC method this host serves."""
        self._ipc.register("hello", self._handle_hello)
        self._ipc.register("health", self._handle_health)
        self._ipc.register("system.snapshot", self._handle_system_snapshot)
        self._ipc.register("system.read", self._handle_system_read)
        self._ipc.register("system.services", self._handle_system_services)
        self._ipc.register("policy.inventory", self._handle_policy_inventory)
        self._ipc.register("policy.evaluate", self._handle_policy_evaluate)
        self._ipc.register("task.create", self._handle_task_create)
        self._ipc.register("task.get", self._handle_task_get)
        self._ipc.register("task.list", self._handle_task_list)
        self._ipc.register("task.history", self._handle_task_history)
        self._ipc.register("task.cancel", self._handle_task_cancel)
        self._ipc.register("approval.list", self._handle_approval_list)
        self._ipc.register("approval.get", self._handle_approval_get)
        self._ipc.register("approval.request", self._handle_approval_request)
        self._ipc.register("approval.decide", self._handle_approval_decide)
        self._ipc.register("approval.revoke", self._handle_approval_revoke)
        self._ipc.register("audit.query", self._handle_audit_query)
        self._ipc.register("audit.summary", self._handle_audit_summary)
        self._ipc.register("events.recent", self._handle_events_recent)
        self._ipc.register("events.subscribe", self._handle_events_subscribe)
        self._ipc.register("events.unsubscribe", self._handle_events_unsubscribe)
        self._ipc.register("events.poll", self._handle_events_poll)

    # -- handlers ----------------------------------------------------------

    def _handle_hello(self, request: IpcRequest) -> dict[str, Any]:
        """Return project identity and the caller's kernel-verified credentials."""
        return {
            "service": PROJECT_SLUG + "-core",
            "version": PROJECT_VERSION,
            "started_at": format_timestamp(utc_now()),
            "uptime_seconds": round(time.time() - self._started_at, 1),
            "peer": request.peer.to_dict(),
            "methods": self._ipc.methods(),
        }

    def _handle_health(self, request: IpcRequest) -> dict[str, Any]:
        """Report service, runtime, and store health without performing model work."""
        report = self._health_report()
        return report

    def _health_report(self) -> dict[str, Any]:
        """Compose the health report shared by IPC and the CLI."""
        runtime_health = self._runtime_health_sync()
        database: dict[str, Any] = {"ok": True}
        try:
            self._storage.connection.execute("SELECT 1").fetchone()
            database["task_count"] = sum(self._storage.count_tasks_by_state().values())
            database["audit_records"] = self._audit.count()
        except AgentOSError as error:
            database = {"ok": False, "error": error.message}
        return {
            "service": {"ok": True, "version": PROJECT_VERSION, "uptime_seconds": round(time.time() - self._started_at, 1)},
            "database": {**database, "path": self._storage.path},
            "runtime": runtime_health,
            "policy": {
                "documents": [policy.name for policy in self._policy.policies],
                "max_level": int(self._policy.service_unit_allowlist() and PrivilegeLevel.CONTROLLED or self._max_policy_level()),
                "max_level_label": self._max_policy_level().label,
                "declared_tools": len(self._policy.manifests),
            },
            "tasks": self._tasks.summary(),
        }

    def _max_policy_level(self) -> PrivilegeLevel:
        """Return the highest level the loaded policies permit."""
        policies = self._policy.policies
        if not policies:
            return PrivilegeLevel.READ_ONLY
        return max(policy.max_level for policy in policies)

    def _runtime_health_sync(self) -> dict[str, Any]:
        """Probe the runtime and return a JSON-serializable health report.

        The probe is typically synchronous because the mock runtime and the DSH
        environment probe both complete without awaiting; when an adapter returns
        a coroutine that cannot be awaited from this context, the report says so
        instead of blocking the event loop.
        """
        probe = self._runtime.health_check()
        if asyncio.iscoroutine(probe):
            probe.close()
            return {
                "available": None,
                "runtime_id": getattr(self._runtime, "runtime_id", "unknown"),
                "detail": "the runtime reports health asynchronously; run agentosctl doctor for the result",
            }
        return probe.to_dict()

    def _handle_system_snapshot(self, request: IpcRequest) -> dict[str, Any]:
        """Return the read-only system snapshot."""
        return self._bridge.read("system.metrics.read", {})

    def _handle_system_read(self, request: IpcRequest) -> dict[str, Any]:
        """Run one read-only System Bridge tool."""
        request.require("tool")
        tool = str(request.param("tool"))
        parameters = request.param("parameters", {})
        if not isinstance(parameters, Mapping):
            raise ValidationError("parameters must be a JSON object")
        result = self._bridge.read(tool, parameters)
        self._audit.record(
            AuditRecord(
                kind="ipc.request",
                action="system.read",
                actor_uid=request.peer.uid,
                operation=tool,
                outcome="ok",
                parameter_digest="",
                payload={"tool": tool},
            )
        )
        return {"tool": tool, "result": result}

    def _handle_system_services(self, request: IpcRequest) -> dict[str, Any]:
        """Return the systemd unit inventory, reporting an unavailable systemd."""
        try:
            statuses = list_services(state=str(request.param("state", "all")), limit=int(request.param("limit", 100)))
        except AgentOSError as error:
            return {"available": False, "error": error.message, "units": []}
        return {"available": True, "units": [status.to_dict() for status in statuses]}

    def _handle_policy_inventory(self, request: IpcRequest) -> dict[str, Any]:
        """Return every declared tool with its level and effective rule."""
        return {
            "policies": [policy.to_dict() for policy in self._policy.policies],
            "tools": self._policy.inventory(),
            "service_unit_allowlist": sorted(self._policy.service_unit_allowlist()),
        }

    def _handle_policy_evaluate(self, request: IpcRequest) -> dict[str, Any]:
        """Evaluate a proposed tool call without running it."""
        request.require("tool")
        tool = str(request.param("tool"))
        parameters = request.param("parameters", {})
        if not isinstance(parameters, Mapping):
            raise ValidationError("parameters must be a JSON object")
        task_id = request.param("task_id")
        if task_id is not None:
            self._require_task_access(request, str(task_id))
        context = self._policy_context(request, task_id=None if task_id is None else str(task_id))
        evaluation = self._policy.evaluate(tool, parameters, context)
        return evaluation.to_dict()

    def _handle_task_create(self, request: IpcRequest) -> dict[str, Any]:
        """Create and queue a task owned by the caller."""
        request.require("prompt")
        prompt = request.param("prompt")
        if not isinstance(prompt, str):
            raise ValidationError("prompt must be a string")
        level_cap = self._parse_level_cap(request.param("level_cap"), request=request)
        priority = request.param("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValidationError("priority must be an integer")
        runtime_id = request.param("runtime_id")
        if runtime_id is not None and str(runtime_id) not in RUNTIME_IDS:
            raise ValidationError(
                f"unknown runtime {runtime_id!r}",
                details={"known": list(RUNTIME_IDS)},
            )
        task = self._tasks.create_task(
            prompt=prompt,
            owner_uid=request.peer.uid,
            owner_name=_user_name(request.peer.uid),
            runtime_id=None if runtime_id is None else str(runtime_id),
            level_cap=level_cap,
            priority=priority,
            title="" if request.param("title") is None else str(request.param("title")),
        )
        self._audit.record(
            AuditRecord(
                kind="task.created",
                action="create",
                task_id=task.task_id,
                actor_uid=request.peer.uid,
                actor_name=_user_name(request.peer.uid),
                outcome="ok",
                payload={"runtime_id": task.runtime_id, "level_cap": int(task.level_cap)},
            )
        )
        asyncio.get_running_loop().create_task(self._tasks.run_task(task.task_id))
        return {"task": task.to_dict()}

    def _parse_level_cap(self, value: Any, *, request: IpcRequest) -> PrivilegeLevel:
        """Parse a requested task privilege cap against the caller's authority."""
        if value is None:
            return PrivilegeLevel.READ_ONLY
        if isinstance(value, bool):
            raise ValidationError("level_cap must be P0..P2 or an integer")
        cap: PrivilegeLevel
        if isinstance(value, int):
            try:
                cap = PrivilegeLevel(value)
            except ValueError as error:
                raise ValidationError("level_cap must be between 0 and 3") from error
        elif isinstance(value, str):
            table = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
            text = value.strip().upper()
            if text not in table:
                raise ValidationError("level_cap must be P0..P3")
            cap = PrivilegeLevel(table[text])
        else:
            raise ValidationError("level_cap must be P0..P3 or an integer")
        if cap > PrivilegeLevel.CONTROLLED and not request.is_root:
            raise PolicyDenied(
                "only the superuser may create a task with P3 administrative authority",
                details={"requested_level": int(cap)},
            )
        if cap > self._max_policy_level():
            raise PolicyDenied(
                f"the loaded policy permits at most {self._max_policy_level().label}",
                details={"requested_level": int(cap), "policy_max": int(self._max_policy_level())},
            )
        return cap

    def _handle_task_get(self, request: IpcRequest) -> dict[str, Any]:
        """Return one task the caller owns."""
        request.require("task_id")
        task_id = str(request.param("task_id"))
        self._require_task_access(request, task_id)
        task = self._storage.get_task(task_id)
        if task is None:
            raise StorageError(f"task {task_id} does not exist", details={"task_id": task_id})
        return {"task": task.to_dict()}

    def _handle_task_list(self, request: IpcRequest) -> dict[str, Any]:
        """List tasks, restricted to the caller's own unless the caller is root."""
        raw_states = request.param("states", [])
        states: list[TaskState] = []
        if raw_states:
            if not isinstance(raw_states, list):
                raise ValidationError("states must be a list of task state names")
            for name in raw_states:
                try:
                    states.append(TaskState(str(name)))
                except ValueError as error:
                    raise ValidationError(f"unknown task state {name!r}") from error
        owner_uid = request.param("owner_uid")
        if owner_uid is None:
            effective_owner: int | None = None if request.is_root and request.param("all_owners") else request.peer.uid
        else:
            if not request.is_root and int(owner_uid) != request.peer.uid:
                raise IPCAccessDenied("only the superuser may list another user's tasks")
            effective_owner = int(owner_uid)
        tasks = self._storage.list_tasks(
            states=states,
            owner_uid=effective_owner,
            limit=int(request.param("limit", 50)),
            offset=int(request.param("offset", 0)),
        )
        return {"count": len(tasks), "tasks": [task.to_dict() for task in tasks], "by_state": self._storage.count_tasks_by_state()}

    def _handle_task_history(self, request: IpcRequest) -> dict[str, Any]:
        """Return one task's event history."""
        request.require("task_id")
        task_id = str(request.param("task_id"))
        self._require_task_access(request, task_id)
        return {
            "task_id": task_id,
            "events": self._tasks.history(task_id, limit=int(request.param("limit", 200))),
        }

    def _handle_task_cancel(self, request: IpcRequest) -> dict[str, Any]:
        """Request cancellation of a task the caller owns."""
        request.require("task_id")
        task_id = str(request.param("task_id"))
        self._require_task_access(request, task_id)
        task = self._tasks.cancel_task(task_id, actor_uid=request.peer.uid)
        self._audit.record(
            AuditRecord(
                kind="task.cancelled",
                action="cancel",
                task_id=task_id,
                actor_uid=request.peer.uid,
                actor_name=_user_name(request.peer.uid),
                outcome="ok",
                payload={"state": task.state.value},
            )
        )
        return {"task": task.to_dict()}

    def _handle_approval_list(self, request: IpcRequest) -> dict[str, Any]:
        """List approvals, restricted to the caller's own unless the caller is root."""
        states = request.param("states", [])
        if states and not isinstance(states, list):
            raise ValidationError("states must be a list of approval state names")
        approvals = self._storage.list_approvals(
            states=[str(state) for state in states] if states else (),
            limit=int(request.param("limit", 50)),
        )
        visible = [
            approval.to_dict(include_nonce=approval.request.requested_by_uid == request.peer.uid)
            for approval in approvals
            if request.is_root or approval.request.requested_by_uid == request.peer.uid
        ]
        return {"count": len(visible), "approvals": visible}

    def _handle_approval_get(self, request: IpcRequest) -> dict[str, Any]:
        """Return one approval the caller requested."""
        request.require("approval_id")
        approval = self._storage.get_approval(str(request.param("approval_id")))
        if approval is None:
            raise StorageError(
                f"approval {request.param('approval_id')} does not exist",
                details={"approval_id": request.param("approval_id")},
            )
        if not request.is_root and approval.request.requested_by_uid != request.peer.uid:
            raise IPCAccessDenied("the approval belongs to another user")
        return {"approval": approval.to_dict(include_nonce=approval.request.requested_by_uid == request.peer.uid)}

    def _handle_approval_request(self, request: IpcRequest) -> dict[str, Any]:
        """Create an approval request for one controlled operation.

        The caller must own the task, and the policy engine decides whether the
        operation is permitted at all. An operation the policy denies is refused
        here rather than recorded as a pending approval, so a model cannot create
        approval prompts for actions AgentOS would never allow.
        """
        request.require("task_id", "tool", "parameters")
        task_id = str(request.param("task_id"))
        self._require_task_access(request, task_id)
        tool = str(request.param("tool"))
        parameters = request.param("parameters")
        if not isinstance(parameters, Mapping):
            raise ValidationError("parameters must be a JSON object")
        context = self._policy_context(request, task_id=task_id)
        evaluation = self._policy.evaluate(tool, parameters, context)
        if evaluation.decision is Decision.DENY:
            self._audit.record(
                AuditRecord(
                    kind="policy.decision",
                    action="approval_request",
                    task_id=task_id,
                    actor_uid=request.peer.uid,
                    operation=tool,
                    target=evaluation.target,
                    decision="deny",
                    outcome="denied",
                    reason=evaluation.reason,
                )
            )
            raise PolicyDenied(f"policy denied {tool!r}: {evaluation.reason}", details=evaluation.to_dict())
        ttl = request.param("ttl_seconds")
        approval = Approval(
            approval_id="",
            request=ApprovalRequest(
                task_id=task_id,
                operation=tool,
                target=evaluation.target,
                parameters=evaluation.parameters,
                level=int(evaluation.level),
                requested_by_uid=request.peer.uid,
                requested_by_name=_user_name(request.peer.uid),
                reason=str(request.param("reason") or evaluation.reason),
            ),
            expires_at=self._policy.approval_expiry(None if ttl is None else int(ttl)),
        )
        self._storage.insert_approval(approval)
        self._audit.record(
            AuditRecord(
                kind="approval.requested",
                action="request",
                task_id=task_id,
                actor_uid=request.peer.uid,
                actor_name=_user_name(request.peer.uid),
                operation=tool,
                target=evaluation.target,
                decision="require_approval",
                outcome="pending",
                reason=approval.request.reason,
                parameter_digest=approval.request.parameter_digest,
            )
        )
        self._on_event(
            Event(
                event_type="approval.requested",
                task_id=task_id,
                severity=Severity.NOTICE,
                sensitivity=Sensitivity.INTERNAL,
                payload={
                    "approval_id": approval.approval_id,
                    "operation": tool,
                    "target": evaluation.target,
                    "level": int(evaluation.level),
                },
            )
        )
        return {"approval": approval.to_dict(include_nonce=True)}

    def _handle_approval_decide(self, request: IpcRequest) -> dict[str, Any]:
        """Record a human decision on an approval."""
        request.require("approval_id", "grant")
        approval_id = str(request.param("approval_id"))
        approval = self._storage.get_approval(approval_id)
        if approval is None:
            raise StorageError(f"approval {approval_id} does not exist", details={"approval_id": approval_id})
        self._require_task_access(request, approval.request.task_id)
        grant = request.param("grant")
        if not isinstance(grant, bool):
            raise ValidationError("grant must be a boolean")
        decided = self._storage.decide_approval(
            approval_id,
            granted=grant,
            decided_by_uid=request.peer.uid,
            decided_by_name=_user_name(request.peer.uid),
            note=str(request.param("note") or ""),
        )
        self._audit.record(
            AuditRecord(
                kind="approval.decided",
                action="grant" if grant else "deny",
                task_id=decided.request.task_id,
                actor_uid=request.peer.uid,
                actor_name=_user_name(request.peer.uid),
                operation=decided.request.operation,
                target=decided.request.target,
                decision="allow" if grant else "deny",
                outcome="ok" if grant else "denied",
                reason=decided.decision_note,
                parameter_digest=decided.request.parameter_digest,
            )
        )
        self._on_event(
            Event(
                event_type="approval.granted" if grant else "approval.denied",
                task_id=decided.request.task_id,
                severity=Severity.NOTICE,
                payload={
                    "approval_id": approval_id,
                    "operation": decided.request.operation,
                    "target": decided.request.target,
                    "decided_by_uid": request.peer.uid,
                },
            )
        )
        return {"approval": decided.to_dict(include_nonce=grant)}

    def _handle_approval_revoke(self, request: IpcRequest) -> dict[str, Any]:
        """Revoke an approval that has not been consumed."""
        request.require("approval_id")
        approval_id = str(request.param("approval_id"))
        approval = self._storage.get_approval(approval_id)
        if approval is None:
            raise StorageError(f"approval {approval_id} does not exist", details={"approval_id": approval_id})
        self._require_task_access(request, approval.request.task_id)
        revoked = self._storage.revoke_approval(approval_id, revoked_by_uid=request.peer.uid)
        self._audit.record(
            AuditRecord(
                kind="approval.decided",
                action="revoke",
                task_id=revoked.request.task_id,
                actor_uid=request.peer.uid,
                operation=revoked.request.operation,
                target=revoked.request.target,
                decision="deny",
                outcome="denied",
                reason="revoked by the owner",
            )
        )
        return {"approval": revoked.to_dict(include_nonce=False)}

    def _handle_audit_query(self, request: IpcRequest) -> dict[str, Any]:
        """Query the audit trail, restricted to the caller's own records."""
        task_id = request.param("task_id")
        if task_id is not None:
            self._require_task_access(request, str(task_id))
        records = self._audit.query(
            task_id=None if task_id is None else str(task_id),
            kind=None if request.param("kind") is None else str(request.param("kind")),
            since=None if request.param("since") is None else str(request.param("since")),
            until=None if request.param("until") is None else str(request.param("until")),
            operation=None if request.param("operation") is None else str(request.param("operation")),
            limit=int(request.param("limit", 100)),
            offset=int(request.param("offset", 0)),
        )
        if request.is_root or task_id is not None:
            visible = records
        else:
            visible = [record for record in records if record.get("actor_uid") == request.peer.uid]
        return {"count": len(visible), "records": visible}

    def _handle_audit_summary(self, request: IpcRequest) -> dict[str, Any]:
        """Return audit counts, scoped to the caller's own records unless root."""
        own = self._audit.summary()
        if request.is_root:
            return own
        records = self._audit.query(limit=1000)
        mine = [record for record in records if record.get("actor_uid") == request.peer.uid]
        by_kind: dict[str, int] = {}
        by_outcome: dict[str, int] = {}
        for record in mine:
            by_kind[record["kind"]] = by_kind.get(record["kind"], 0) + 1
            by_outcome[record["outcome"]] = by_outcome.get(record["outcome"], 0) + 1
        return {"by_kind": by_kind, "by_outcome": by_outcome, "total": len(mine), "retained_total": own.get("total", 0)}

    def _handle_events_recent(self, request: IpcRequest) -> dict[str, Any]:
        """Return recent events, filtered to the caller's own tasks."""
        events = [
            event
            for event in self.recent_events(limit=int(request.param("limit", 20)))
            if event.get("task_id") is None or self._owns_task(request.peer.uid, str(event["task_id"]))
        ]
        return {"count": len(events), "events": events}

    def _handle_events_subscribe(self, request: IpcRequest) -> dict[str, Any]:
        """Register an event subscription for this peer."""
        mine = [subscriber for subscriber in self._subscribers.values() if subscriber.uid == request.peer.uid]
        if len(mine) >= MAX_SUBSCRIBERS_PER_UID:
            raise ValidationError(
                f"at most {MAX_SUBSCRIBERS_PER_UID} event subscriptions per user",
                details={"uid": request.peer.uid},
            )
        token = new_request_id()
        self._subscribers[token] = Subscriber(token=token, uid=request.peer.uid)
        return {"token": token}

    def _handle_events_unsubscribe(self, request: IpcRequest) -> dict[str, Any]:
        """Drop an event subscription."""
        request.require("token")
        token = str(request.param("token"))
        subscriber = self._subscribers.get(token)
        if subscriber is None:
            return {"removed": False}
        if subscriber.uid != request.peer.uid and not request.is_root:
            raise IPCAccessDenied("the subscription belongs to another user")
        self._subscribers.pop(token, None)
        return {"removed": True}

    def _handle_events_poll(self, request: IpcRequest) -> dict[str, Any]:
        """Return and clear the events queued for a subscription.

        Polling is offered alongside streaming so that a short-lived client — and
        the GNOME extension's session broker — never has to hold a socket open.
        """
        request.require("token")
        token = str(request.param("token"))
        subscriber = self._subscribers.get(token)
        if subscriber is None:
            raise ValidationError("unknown subscription token", details={"token": token})
        if subscriber.uid != request.peer.uid and not request.is_root:
            raise IPCAccessDenied("the subscription belongs to another user")
        drain = int(request.param("limit", 50))
        events: list[dict[str, Any]] = []
        while len(events) < drain:
            try:
                events.append(subscriber.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return {"count": len(events), "events": events}

    # -- service lifecycle -------------------------------------------------

    def recover(self) -> list[dict[str, Any]]:
        """Reconcile tasks left behind by a previous process."""
        report = self._tasks.recover()
        if report:
            self._logger.info("task recovery applied", extra={"method": "recover"})
            self._audit.record(
                AuditRecord(kind="system.note", action="recovery", outcome="ok", payload={"report": report})
            )
        return report

    async def start(self) -> None:
        """Start the IPC server and the task queue pump."""
        self.recover()
        await self._ipc.start()
        self._logger.info("agentos-core listening", extra={"method": "start"})
        self._queue_task = asyncio.create_task(
            self._tasks.run_queue(poll_interval_seconds=self.config.core.queue_poll_seconds)
        )

    async def run_forever(self) -> None:
        """Serve until :meth:`stop` is called or a termination signal arrives."""
        await self.start()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(signum, self._shutdown.set)
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(signal.SIGHUP, self._reload_configuration)
        await self._shutdown.wait()
        await self.stop()

    def _reload_configuration(self) -> None:
        """Apply a SIGHUP reload, keeping the previous configuration on failure."""
        fresh, error = self._loader.reload()
        if error is not None:
            self._logger.error("configuration reload failed: %s", error, extra={"method": "reload"})
            return
        self._logger.info("configuration reloaded", extra={"method": "reload"})
        self._audit.record(
            AuditRecord(kind="config.reloaded", action="reload", outcome="ok", payload={"sources": list(fresh.sources)})
        )

    async def stop(self) -> None:
        """Stop the queue pump, the runtime, and the IPC server."""
        queue_task = getattr(self, "_queue_task", None)
        if queue_task is not None:
            queue_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await queue_task
        for subscriber in list(self._subscribers.values()):
            subscriber.offer({"event": {"event_type": "runtime.exited", "payload": {"reason": "service stopping"}}})
        with contextlib.suppress(AgentOSError):
            removed = self._audit.prune(
                retain_days=self.config.core.audit_retain_days,
                max_records=self.config.core.audit_max_records,
            )
            if removed:
                self._logger.info("audit retention removed %d record(s)", removed, extra={"method": "stop"})
        with contextlib.suppress(Exception):
            await self._runtime.close()
        await self._ipc.close()
        self._storage.close()
        self._logger.info("agentos-core stopped", extra={"method": "stop"})


def _user_name(uid: int) -> str:
    """Return a display name for ``uid``, tolerating an unknown account."""
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError, OSError):
        return ""


def load_effective_config() -> Config:
    """Load configuration and merge credentials from the root-owned environment file.

    Credentials are placed into the process environment only when the file exists
    and is private; they are never written to the database, the audit trail, or an
    event payload.
    """
    config = load_config()
    credentials = read_credential_environment()
    for name, value in credentials.items():
        os.environ.setdefault(name, value)
    return config


__all__ = [
    "MAX_SUBSCRIBERS_PER_UID",
    "SUBSCRIBER_BACKLOG",
    "CoreHost",
    "Subscriber",
    "configure_logging",
    "load_effective_config",
    "runtime_catalog",
]


def runtime_catalog() -> list[dict[str, Any]]:
    """Return the shipped runtime catalog for documentation and the doctor command."""
    return list_runtimes()
