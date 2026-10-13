"""Task subsystem: the state machine, durable records, and storage."""

from __future__ import annotations

from .manager import TaskManager, TaskWorkerResult
from .states import TERMINAL_STATES, TRANSITIONS, TaskState, is_transition_allowed, recovery_action, require_transition
from .storage import MAX_RESULT_BYTES, Storage, TaskEventRecord, TaskRecord, store_digest

__all__ = [
    "MAX_RESULT_BYTES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "Storage",
    "TaskEventRecord",
    "TaskManager",
    "TaskRecord",
    "TaskState",
    "TaskWorkerResult",
    "is_transition_allowed",
    "recovery_action",
    "require_transition",
    "store_digest",
]
