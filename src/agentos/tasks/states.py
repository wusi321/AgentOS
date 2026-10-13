"""Task state machine.

The legal transition table is the only place task state changes are decided, so
that the persistence layer, the IPC server, and the desktop UI cannot invent a
state change the rest of the system does not expect.
"""

from __future__ import annotations

from enum import Enum
from collections.abc import Mapping
from typing import Final

from ..errors import TaskStateError


class TaskState(str, Enum):
    """Lifecycle state of an AgentOS task."""

    CREATED = "CREATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    RECOVERING = "RECOVERING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"

    @property
    def is_terminal(self) -> bool:
        """Return whether no further transition is possible."""
        return self in TERMINAL_STATES

    @property
    def is_active(self) -> bool:
        """Return whether the task still occupies a worker or a queue slot."""
        return not self.is_terminal


#: States from which no transition is possible.
TERMINAL_STATES: Final[frozenset[TaskState]] = frozenset(
    {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.TIMED_OUT}
)

#: Legal transitions. Anything absent from this table is refused.
TRANSITIONS: Final[Mapping[TaskState, frozenset[TaskState]]] = {
    TaskState.CREATED: frozenset({TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.QUEUED: frozenset(
        {
            TaskState.RUNNING,
            TaskState.CANCELLED,
            TaskState.PAUSED,
            TaskState.FAILED,
            TaskState.TIMED_OUT,
        }
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.WAITING_APPROVAL,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.TIMED_OUT,
            TaskState.PAUSED,
            TaskState.RECOVERING,
        }
    ),
    TaskState.WAITING_APPROVAL: frozenset(
        {
            TaskState.RUNNING,
            TaskState.CANCELLED,
            TaskState.FAILED,
            TaskState.TIMED_OUT,
            TaskState.PAUSED,
        }
    ),
    TaskState.PAUSED: frozenset({TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.RECOVERING: frozenset(
        {TaskState.RUNNING, TaskState.QUEUED, TaskState.FAILED, TaskState.CANCELLED, TaskState.TIMED_OUT}
    ),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.TIMED_OUT: frozenset(),
}


def is_transition_allowed(current: TaskState, target: TaskState) -> bool:
    """Return whether ``current`` may move to ``target``."""
    if current is target:
        return False
    return target in TRANSITIONS[current]


def require_transition(current: TaskState, target: TaskState) -> None:
    """Raise :class:`TaskStateError` unless the transition is legal.

    :param current: Current persisted state.
    :param target: Requested next state.
    """
    if current is target:
        raise TaskStateError(
            f"task is already {current.value}",
            details={"current": current.value, "requested": target.value},
        )
    if target not in TRANSITIONS[current]:
        raise TaskStateError(
            f"illegal task transition {current.value} -> {target.value}",
            details={
                "current": current.value,
                "requested": target.value,
                "allowed": sorted(state.value for state in TRANSITIONS[current]),
            },
        )


def recovery_action(state: TaskState, *, side_effects_confirmed: bool) -> str:
    """Return what the service must do with a task found in ``state`` at startup.

    :param state: State read from durable storage.
    :param side_effects_confirmed: Whether the task had already begun a
        side-effecting operation whose outcome is unknown.
    :returns: ``resume``, ``requeue``, ``fail``, or ``leave``.
    """
    if state.is_terminal:
        return "leave"
    if state is TaskState.WAITING_APPROVAL:
        return "requeue"
    if state in {TaskState.CREATED, TaskState.QUEUED, TaskState.PAUSED}:
        return "requeue"
    if state in {TaskState.RUNNING, TaskState.RECOVERING}:
        # A task interrupted mid-run may or may not have executed its operation.
        # Requeueing is only safe when no side effect was started.
        return "requeue" if not side_effects_confirmed else "fail"
    return "leave"


__all__ = [
    "TERMINAL_STATES",
    "TRANSITIONS",
    "TaskState",
    "is_transition_allowed",
    "recovery_action",
    "require_transition",
]
