"""Task state machine tests: every legal transition and every refusal."""

from __future__ import annotations

import itertools

import pytest

from agentos.errors import TaskStateError
from agentos.tasks.states import (
    TERMINAL_STATES,
    TRANSITIONS,
    TaskState,
    is_transition_allowed,
    recovery_action,
    require_transition,
)


def test_every_state_declares_a_transition_set() -> None:
    """Every state must appear in the transition table, terminal states included."""
    assert set(TRANSITIONS) == set(TaskState)


def test_terminal_states_have_no_outgoing_transitions() -> None:
    """A terminal task can never move again."""
    for state in TERMINAL_STATES:
        assert TRANSITIONS[state] == frozenset()
        assert state.is_terminal is True
        assert state.is_active is False


def test_non_terminal_states_are_active() -> None:
    """An unfinished task is reported as active."""
    for state in set(TaskState) - TERMINAL_STATES:
        assert state.is_active is True
        assert state.is_terminal is False


def test_legal_transitions_are_accepted() -> None:
    """Each declared transition is accepted by the guard."""
    for current, targets in TRANSITIONS.items():
        for target in targets:
            require_transition(current, target)
            assert is_transition_allowed(current, target) is True


def test_illegal_transitions_are_refused() -> None:
    """Every undeclared pair is refused with the allowed set in the details."""
    for current, target in itertools.product(TaskState, repeat=2):
        if target in TRANSITIONS[current]:
            continue
        with pytest.raises(TaskStateError) as caught:
            require_transition(current, target)
        assert caught.value.details["current"] == current.value
        assert caught.value.details["requested"] == target.value


def test_same_state_transition_is_refused() -> None:
    """A no-op transition is an error rather than a silent success."""
    with pytest.raises(TaskStateError) as caught:
        require_transition(TaskState.RUNNING, TaskState.RUNNING)
    assert "already RUNNING" in caught.value.message


def test_creation_path_reaches_success() -> None:
    """The documented happy path is walkable end to end."""
    path = [
        (TaskState.CREATED, TaskState.QUEUED),
        (TaskState.QUEUED, TaskState.RUNNING),
        (TaskState.RUNNING, TaskState.SUCCEEDED),
    ]
    for current, target in path:
        require_transition(current, target)


def test_approval_path_is_walkable() -> None:
    """A task may pause for an approval and resume."""
    require_transition(TaskState.RUNNING, TaskState.WAITING_APPROVAL)
    require_transition(TaskState.WAITING_APPROVAL, TaskState.RUNNING)
    require_transition(TaskState.WAITING_APPROVAL, TaskState.CANCELLED)


def test_recovery_requeues_when_no_side_effect_started() -> None:
    """An interrupted task with no confirmed side effect is requeued."""
    assert recovery_action(TaskState.RUNNING, side_effects_confirmed=False) == "requeue"
    assert recovery_action(TaskState.RECOVERING, side_effects_confirmed=False) == "requeue"
    assert recovery_action(TaskState.QUEUED, side_effects_confirmed=False) == "requeue"
    assert recovery_action(TaskState.WAITING_APPROVAL, side_effects_confirmed=False) == "requeue"


def test_recovery_fails_when_side_effect_is_unconfirmed() -> None:
    """A task that may already have acted is failed, never silently re-run."""
    assert recovery_action(TaskState.RUNNING, side_effects_confirmed=True) == "fail"
    assert recovery_action(TaskState.RECOVERING, side_effects_confirmed=True) == "fail"


def test_recovery_leaves_terminal_tasks_alone() -> None:
    """A settled task is never touched by recovery."""
    for state in TERMINAL_STATES:
        assert recovery_action(state, side_effects_confirmed=False) == "leave"
        assert recovery_action(state, side_effects_confirmed=True) == "leave"
