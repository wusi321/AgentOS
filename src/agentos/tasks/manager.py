"""Task manager: creation, queueing, execution, cancellation, and recovery.

The manager owns the lifecycle of a task. It never executes a side-effecting
operation itself; it drives an :class:`~agentos.runtime.api.AgentRuntime`,
records what happened, and asks the policy engine before anything with a side
effect is allowed to run.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from collections.abc import Callable, Iterable, Mapping
from typing import Any, AsyncIterator, Final

from ..errors import AgentOSError, StorageError, TaskStateError, ValidationError
from ..events import Event, Severity, utc_now
from ..policy import PolicyContext, PolicyEngine, PrivilegeLevel
from ..runtime import AgentRuntime, RuntimeEvent, RuntimeRequest
from .states import TaskState, recovery_action, require_transition
from .storage import Storage, TaskRecord

#: Default deadline for a queued task before it is marked timed out.
DEFAULT_QUEUE_TIMEOUT_SECONDS: Final = 3600.0

EventSink = Callable[[Event], None]


@dataclass(frozen=True, slots=True)
class TaskWorkerResult:
    """Outcome of running one task to settlement.

    :param task_id: Task that ran.
    :param state: Terminal or approval-waiting state reached.
    :param assistant_text: Accumulated assistant output.
    :param events: Normalized runtime events observed, in order.
    :param error: Failure explanation, when the run failed.
    """

    task_id: str
    state: TaskState
    assistant_text: str = ""
    events: tuple[RuntimeEvent, ...] = ()
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a compact JSON-serializable summary of this result."""
        return {
            "task_id": self.task_id,
            "state": self.state.value,
            "assistant_text": self.assistant_text,
            "event_count": len(self.events),
            "error": self.error,
        }


class TaskManager:
    """Own the task lifecycle on top of :class:`Storage` and an ``AgentRuntime``.

    :param storage: Durable store for tasks and history.
    :param runtime: Runtime that executes prompts.
    :param policy: Policy engine used for tool decisions.
    :param emit: Optional sink for AgentOS events; failures there never break a task.
    :param workspace_root: Root directory under which per-task workspaces are created.
    :param max_concurrent_tasks: Upper bound on tasks running at once.
    """

    def __init__(
        self,
        *,
        storage: Storage,
        runtime: AgentRuntime,
        policy: PolicyEngine,
        emit: EventSink | None = None,
        workspace_root: str = "/var/lib/agentos/workspaces",
        max_concurrent_tasks: int = 2,
    ) -> None:
        if max_concurrent_tasks < 1:
            raise ValidationError("max_concurrent_tasks must be at least 1")
        self._storage = storage
        self._runtime = runtime
        self._policy = policy
        self._emit = emit
        self._workspace_root = workspace_root
        self._semaphore = asyncio.Semaphore(max_concurrent_tasks)
        self._running: dict[str, asyncio.Task[TaskWorkerResult]] = {}

    # -- introspection -----------------------------------------------------

    @property
    def storage(self) -> Storage:
        """Return the durable store."""
        return self._storage

    @property
    def runtime(self) -> AgentRuntime:
        """Return the active runtime."""
        return self._runtime

    @property
    def policy(self) -> PolicyEngine:
        """Return the policy engine."""
        return self._policy

    def running_task_ids(self) -> list[str]:
        """Return the ids of tasks currently executing in this process."""
        return sorted(self._running)

    # -- event publishing --------------------------------------------------

    def publish(self, event: Event) -> None:
        """Publish an event to the configured sink, containing sink failures."""
        if self._emit is None:
            return
        try:
            self._emit(event)
        except Exception:  # noqa: BLE001 - a broken consumer must not fail a task
            pass

    # -- creation and lifecycle -------------------------------------------

    def create_task(
        self,
        *,
        prompt: str,
        owner_uid: int,
        owner_name: str = "",
        runtime_id: str | None = None,
        level_cap: PrivilegeLevel = PrivilegeLevel.READ_ONLY,
        priority: int = 0,
        title: str = "",
    ) -> TaskRecord:
        """Create a task in ``CREATED`` and immediately queue it.

        :param prompt: User instruction; must be non-empty.
        :param owner_uid: Local user who owns the task.
        :param owner_name: Display name of that user, when known.
        :param runtime_id: Runtime to use; defaults to the manager's runtime id.
        :param level_cap: Highest privilege level the task may reach.
        :param priority: Queue priority; higher runs first.
        :param title: Optional display title.
        :raises ValidationError: On an unusable prompt or identity.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValidationError("task prompt must be a non-empty string")
        if len(prompt) > 32_768:
            raise ValidationError("task prompt exceeds 32768 characters")
        if isinstance(owner_uid, bool) or not isinstance(owner_uid, int) or owner_uid < 0:
            raise ValidationError("owner_uid must be a non-negative integer")
        if isinstance(priority, bool) or not isinstance(priority, int) or not -100 <= priority <= 100:
            raise ValidationError("priority must be an integer between -100 and 100")
        if not isinstance(level_cap, PrivilegeLevel):
            level_cap = PrivilegeLevel(level_cap)
        task = TaskRecord(
            task_id="",
            prompt=prompt,
            owner_uid=owner_uid,
            owner_name=owner_name,
            runtime_id=runtime_id or getattr(self._runtime, "runtime_id", "unknown"),
            level_cap=level_cap,
            priority=priority,
            title=title,
            workspace=self.workspace_for(prompt),
        )
        self._storage.insert_task(task)
        self._storage.append_task_event(
            task.task_id, "task.created", {"runtime_id": task.runtime_id, "level_cap": int(level_cap)}
        )
        self.publish(
            Event(
                event_type="task.created",
                task_id=task.task_id,
                payload={
                    "owner_uid": owner_uid,
                    "runtime_id": task.runtime_id,
                    "title": task.title,
                    "level_cap": int(level_cap),
                },
            )
        )
        queued = self._storage.update_task_state(task.task_id, TaskState.QUEUED)
        self._record_state_change(queued, previous=TaskState.CREATED)
        return queued

    def workspace_for(self, prompt: str) -> str:
        """Return the workspace directory a new task should use.

        Each task gets its own directory under the configured root so that a
        runtime account cannot read another task's working files.
        """
        import uuid

        return os.path.join(self._workspace_root, uuid.uuid4().hex[:16])

    def _record_state_change(self, task: TaskRecord, *, previous: TaskState) -> None:
        """Record a state change in the task history and on the event bus."""
        self._storage.append_task_event(
            task.task_id,
            "task.state_changed",
            {"from": previous.value, "to": task.state.value},
        )
        self.publish(
            Event(
                event_type="task.state_changed",
                task_id=task.task_id,
                severity=Severity.INFO,
                payload={"from": previous.value, "to": task.state.value, "runtime_id": task.runtime_id},
            )
        )

    def cancel_task(self, task_id: str, *, actor_uid: int) -> TaskRecord:
        """Request cancellation of a task.

        A queued task moves straight to ``CANCELLED``. A running task has
        ``cancel_requested`` set and its worker is asked to stop.

        :param task_id: Task to cancel.
        :param actor_uid: User requesting cancellation.
        :raises StorageError: When the task does not exist.
        """
        task = self._require_task(task_id)
        if task.state.is_terminal:
            return task
        marked = self._storage.request_cancellation(task_id)
        if task.state in {TaskState.CREATED, TaskState.QUEUED, TaskState.PAUSED}:
            cancelled = self._storage.update_task_state(task_id, TaskState.CANCELLED)
            self._record_state_change(cancelled, previous=task.state)
            return cancelled
        worker = self._running.get(task_id)
        if worker is not None:
            worker.cancel()
        self.publish(
            Event(
                event_type="task.cancelled",
                task_id=task_id,
                severity=Severity.NOTICE,
                payload={"requested_by_uid": actor_uid, "state": marked.state.value},
            )
        )
        return marked

    def _require_task(self, task_id: str) -> TaskRecord:
        """Return a task or raise a storage failure."""
        task = self._storage.get_task(task_id)
        if task is None:
            raise StorageError(f"task {task_id} does not exist", details={"task_id": task_id})
        return task

    # -- execution ---------------------------------------------------------

    async def run_task(self, task_id: str) -> TaskWorkerResult:
        """Execute one task to settlement.

        :param task_id: Task to run.
        :returns: The settled outcome. A task that stops at an approval gate
            returns ``WAITING_APPROVAL`` rather than a terminal state.
        """
        async with self._semaphore:
            return await self._run_task_inner(task_id)

    async def _run_task_inner(self, task_id: str) -> TaskWorkerResult:
        """Run one claimed task, publishing every normalized runtime event."""
        task = self._require_task(task_id)
        if task.state not in {TaskState.QUEUED, TaskState.RUNNING, TaskState.RECOVERING}:
            raise TaskStateError(
                f"task {task_id} is {task.state.value} and cannot be run",
                details={"task_id": task_id, "state": task.state.value},
            )
        self._ensure_workspace(task.workspace)
        context = PolicyContext(
            uid=task.owner_uid,
            user_name=task.owner_name,
            task_level_cap=task.level_cap,
        )
        collected: list[RuntimeEvent] = []
        assistant_parts: list[str] = []
        session_id = task.session_id or task.task_id
        try:
            await self._runtime.start()
            session_id = await self._runtime.create_session(workspace=task.workspace, session_id=session_id)
        except AgentOSError as error:
            return self._fail(task, f"runtime unavailable: {error.message}", collected)
        if session_id != task.session_id:
            task = self._storage.update_task_state(task.task_id, TaskState.RUNNING, session_id=session_id)

        try:
            async for event in self._runtime.send_message(
                RuntimeRequest(
                    session_id=session_id,
                    message=task.prompt,
                    workspace=task.workspace,
                    task_id=task.task_id,
                )
            ):
                collected.append(event)
                self._persist_runtime_event(task, event)
                if event.type == "message.delta":
                    text = event.payload.get("text")
                    if isinstance(text, str):
                        assistant_parts.append(text)
        except asyncio.CancelledError:
            cancelled = self._storage.update_task_state(task.task_id, TaskState.CANCELLED)
            self._record_state_change(cancelled, previous=TaskState.RUNNING)
            raise
        except AgentOSError as error:
            if error.code == "agentos.capability_unsupported":
                return self._fail(task, f"runtime cannot serve this task: {error.message}", collected)
            return self._fail(task, error.message, collected)
        except Exception as error:  # noqa: BLE001 - a runtime crash must fail the task, not the service
            return self._fail(task, f"runtime failure: {type(error).__name__}: {error}", collected)

        if self._storage.get_task(task.task_id) is not None:
            current = self._storage.get_task(task.task_id)
            if current is not None and current.cancel_requested:
                cancelled = self._storage.update_task_state(task.task_id, TaskState.CANCELLED)
                self._record_state_change(cancelled, previous=TaskState.RUNNING)
                return TaskWorkerResult(
                    task_id=task.task_id,
                    state=TaskState.CANCELLED,
                    assistant_text="".join(assistant_parts),
                    events=tuple(collected),
                    error="cancelled",
                )
        text = "".join(assistant_parts)
        if not text:
            text = self._last_completed_text(collected)
        policy_error = self._screen_tool_events(task, collected, context)
        if policy_error is not None:
            return self._fail(task, policy_error, collected, assistant_text=text)
        succeeded = self._storage.update_task_state(
            task.task_id, TaskState.SUCCEEDED, result_summary=text
        )
        self._record_state_change(succeeded, previous=TaskState.RUNNING)
        self.publish(
            Event(
                event_type="task.completed",
                task_id=task.task_id,
                payload={"state": succeeded.state.value, "runtime_id": succeeded.runtime_id},
            )
        )
        return TaskWorkerResult(
            task_id=task.task_id,
            state=TaskState.SUCCEEDED,
            assistant_text=text,
            events=tuple(collected),
        )

    @staticmethod
    def _last_completed_text(events: Iterable[RuntimeEvent]) -> str:
        """Return the last completed message text, used when no deltas arrived."""
        text = ""
        for event in events:
            if event.type == "message.completed":
                payload_text = event.payload.get("text")
                if isinstance(payload_text, str) and payload_text:
                    text = payload_text
        return text

    def _screen_tool_events(
        self,
        task: TaskRecord,
        events: Iterable[RuntimeEvent],
        context: PolicyContext,
    ) -> str | None:
        """Apply policy to every tool call the runtime reported.

        The runtime's own tools are not AgentOS tools, so the outcome here is a
        record of what the model attempted. A tool name that matches an AgentOS
        capability and is denied produces a task failure, which is the honest
        outcome: the model tried to act outside the task's authority.
        """
        for event in events:
            if event.type != "tool.call.started":
                continue
            name = event.payload.get("name")
            if not isinstance(name, str):
                continue
            arguments = event.payload.get("arguments")
            parameters = arguments if isinstance(arguments, Mapping) else {}
            evaluation = self._policy.evaluate(name, parameters, context)
            self._storage.append_task_event(
                task.task_id,
                "policy.decision",
                {
                    "tool": name,
                    "decision": evaluation.decision.value,
                    "reason": evaluation.reason,
                    "rule_name": evaluation.rule_name,
                    "level": int(evaluation.level),
                },
            )
            self.publish(
                Event(
                    event_type="policy.decision",
                    task_id=task.task_id,
                    session_id=event.session_id,
                    severity=Severity.NOTICE if evaluation.decision.value != "allow" else Severity.INFO,
                    payload={
                        "tool": name,
                        "decision": evaluation.decision.value,
                        "reason": evaluation.reason,
                        "level": int(evaluation.level),
                    },
                )
            )
            if evaluation.decision.value == "deny" and self._policy.manifest(name) is not None:
                return f"policy denied tool {name!r}: {evaluation.reason}"
        return None

    def _persist_runtime_event(self, task: TaskRecord, event: RuntimeEvent) -> None:
        """Store and publish one normalized runtime event."""
        self._storage.append_task_event(
            task.task_id,
            f"runtime.{event.type}",
            {
                "runtime_id": event.runtime_id,
                "session_id": event.session_id,
                "sequence": event.sequence,
                "native_event_type": event.native_event_type,
                "payload": dict(event.payload),
            },
        )
        event_type = {
            "session.created": "runtime.session_created",
            "message.delta": "runtime.message_delta",
            "message.completed": "runtime.message_completed",
            "tool.call.started": "runtime.tool_call_started",
            "tool.call.completed": "runtime.tool_call_completed",
            "approval.required": "approval.requested",
            "runtime.warning": "runtime.warning",
            "runtime.error": "runtime.error",
            "runtime.exited": "runtime.exited",
        }.get(event.type)
        if event_type is None:
            return
        self.publish(
            Event(
                event_type=event_type,
                task_id=task.task_id,
                session_id=event.session_id,
                severity=event.severity,
                payload=dict(event.payload),
            )
        )

    def _fail(
        self,
        task: TaskRecord,
        message: str,
        events: Iterable[RuntimeEvent],
        *,
        assistant_text: str = "",
    ) -> TaskWorkerResult:
        """Move a task to ``FAILED`` and report the outcome."""
        failed = self._storage.update_task_state(task.task_id, TaskState.FAILED, error_message=message)
        self._record_state_change(failed, previous=task.state)
        self.publish(
            Event(
                event_type="task.failed",
                task_id=task.task_id,
                severity=Severity.ERROR,
                payload={"error": message, "runtime_id": task.runtime_id},
            )
        )
        return TaskWorkerResult(
            task_id=task.task_id,
            state=TaskState.FAILED,
            assistant_text=assistant_text,
            events=tuple(events),
            error=message,
        )

    @staticmethod
    def _ensure_workspace(path: str) -> None:
        """Create the task workspace with owner-only permissions."""
        if not path:
            raise ValidationError("task workspace path is empty")
        os.makedirs(path, mode=0o700, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass

    # -- recovery ----------------------------------------------------------

    def recover(self) -> list[dict[str, Any]]:
        """Reconcile tasks left non-terminal by a service restart.

        A task interrupted mid-run is failed rather than requeued when it could
        already have executed a side effect, because re-running an unconfirmed
        operation is worse than reporting it.

        :returns: One entry per task whose state changed.
        """
        report: list[dict[str, Any]] = []
        for state in (
            TaskState.CREATED,
            TaskState.QUEUED,
            TaskState.RUNNING,
            TaskState.WAITING_APPROVAL,
            TaskState.PAUSED,
            TaskState.RECOVERING,
        ):
            for task in self._storage.list_tasks(states=[state], limit=500):
                action = recovery_action(task.state, side_effects_confirmed=task.side_effects_possible)
                if action == "leave":
                    continue
                if action == "requeue" and task.state in {TaskState.QUEUED, TaskState.CREATED}:
                    continue
                if action == "requeue":
                    if task.state in {TaskState.WAITING_APPROVAL, TaskState.PAUSED}:
                        updated = self._storage.update_task_state(task.task_id, TaskState.QUEUED)
                    else:
                        updated = self._storage.update_task_state(task.task_id, TaskState.RECOVERING)
                        updated = self._storage.update_task_state(task.task_id, TaskState.QUEUED)
                    report.append({"task_id": task.task_id, "action": "requeue", "state": updated.state.value})
                    continue
                if action == "fail":
                    updated = self._storage.update_task_state(
                        task.task_id,
                        TaskState.FAILED,
                        error_message=(
                            "the service restarted while this task was running and its side effects are unconfirmed; "
                            "re-run the task after checking the target's state"
                        ),
                    )
                    report.append({"task_id": task.task_id, "action": "fail", "state": updated.state.value})
        if report:
            self.publish(
                Event(
                    event_type="audit.note",
                    severity=Severity.NOTICE,
                    payload={"recovery": report, "at": str(utc_now())},
                )
            )
        return report

    # -- queue pump --------------------------------------------------------

    async def run_queue(self, *, poll_interval_seconds: float = 0.5) -> None:
        """Claim and run queued tasks until cancelled.

        :param poll_interval_seconds: Delay between empty queue polls. The pump
            is deliberately a pull loop rather than an event callback: it keeps
            claim and execution in one place and survives a producer crash.
        """
        if poll_interval_seconds <= 0:
            raise ValidationError("poll_interval_seconds must be positive")
        try:
            while True:
                claimed = self._storage.claim_next_task()
                if claimed is None:
                    await asyncio.sleep(poll_interval_seconds)
                    continue
                worker = asyncio.create_task(self._run_task_inner(claimed.task_id))
                self._running[claimed.task_id] = worker
                worker.add_done_callback(lambda _task, task_id=claimed.task_id: self._running.pop(task_id, None))
        except asyncio.CancelledError:
            for worker in list(self._running.values()):
                worker.cancel()
            raise

    async def run_until_idle(self, *, max_tasks: int = 100) -> list[TaskWorkerResult]:
        """Run queued tasks until the queue is empty; used by tests and the CLI."""
        if max_tasks < 1:
            raise ValidationError("max_tasks must be at least 1")
        results: list[TaskWorkerResult] = []
        for _ in range(max_tasks):
            claimed = self._storage.claim_next_task()
            if claimed is None:
                break
            results.append(await self.run_task(claimed.task_id))
        return results

    def history(self, task_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Return a task's history as JSON-serializable dictionaries."""
        return [entry.to_dict() for entry in self._storage.list_task_events(task_id, limit=limit)]

    def summary(self) -> dict[str, Any]:
        """Return counts used by the dashboard."""
        return {
            "by_state": self._storage.count_tasks_by_state(),
            "pending_approvals": self._storage.count_pending_approvals(),
            "running_here": self.running_task_ids(),
        }


__all__ = ["DEFAULT_QUEUE_TIMEOUT_SECONDS", "EventSink", "TaskManager", "TaskWorkerResult"]
