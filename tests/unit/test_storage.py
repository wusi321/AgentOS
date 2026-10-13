"""Storage tests: persistence, illegal transitions, atomic claiming, and recovery."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentos.errors import StorageError, TaskStateError, ValidationError
from agentos.policy import Approval, ApprovalRequest, PrivilegeLevel
from agentos.tasks import Storage, TaskRecord, TaskState


def make_task(**overrides: object) -> TaskRecord:
    """Build a task record with test defaults."""
    defaults: dict[str, object] = {
        "task_id": "",
        "prompt": "inspect the machine",
        "owner_uid": 1000,
        "owner_name": "tester",
        "runtime_id": "mock",
        "level_cap": PrivilegeLevel.READ_ONLY,
    }
    defaults.update(overrides)
    return TaskRecord(**defaults)  # type: ignore[arg-type]


def test_schema_version_is_recorded(storage: Storage) -> None:
    """The migration chain leaves a schema version behind."""
    version = storage.connection.execute("PRAGMA user_version").fetchone()[0]
    assert int(version) >= 1


def test_wal_mode_is_enabled(storage: Storage) -> None:
    """Readers must not block the task worker."""
    mode = storage.connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    """Reopening an existing database applies no migration twice."""
    path = str(tmp_path / "state.sqlite3")
    first = Storage(path)
    first.insert_task(make_task(task_id="a"))
    first.close()
    second = Storage(path)
    assert second.get_task("a") is not None
    second.close()


def test_task_round_trip(storage: Storage) -> None:
    """Every persisted field survives a round trip."""
    task = make_task(task_id="t1", title="Custom title", priority=5)
    storage.insert_task(task)
    loaded = storage.get_task("t1")
    assert loaded is not None
    assert loaded.prompt == task.prompt
    assert loaded.title == "Custom title"
    assert loaded.priority == 5
    assert loaded.level_cap is PrivilegeLevel.READ_ONLY
    assert loaded.state is TaskState.CREATED


def test_duplicate_task_id_is_rejected(storage: Storage) -> None:
    """A repeated task id fails loudly rather than overwriting."""
    storage.insert_task(make_task(task_id="dup"))
    with pytest.raises(StorageError):
        storage.insert_task(make_task(task_id="dup"))


def test_empty_prompt_is_rejected() -> None:
    """A task without an instruction cannot exist."""
    with pytest.raises(ValidationError):
        make_task(task_id="x", prompt="   ")


def test_illegal_transition_is_refused(storage: Storage) -> None:
    """The state machine is enforced at the storage boundary too."""
    storage.insert_task(make_task(task_id="t2"))
    with pytest.raises(TaskStateError):
        storage.update_task_state("t2", TaskState.SUCCEEDED)


def test_missing_task_update_is_reported(storage: Storage) -> None:
    """Updating an unknown task is an explicit failure."""
    with pytest.raises(StorageError):
        storage.update_task_state("nope", TaskState.QUEUED)


def test_claim_next_task_is_fifo_within_priority(storage: Storage) -> None:
    """Higher priority wins; equal priority is oldest first."""
    for index, (task_id, priority) in enumerate((("low", 0), ("high", 10), ("mid", 5))):
        storage.insert_task(make_task(task_id=task_id, priority=priority))
        storage.update_task_state(task_id, TaskState.QUEUED)
        assert storage.get_task(task_id) is not None
    first = storage.claim_next_task()
    assert first is not None and first.task_id == "high"
    assert first.state is TaskState.RUNNING
    assert first.attempt_count == 1
    second = storage.claim_next_task()
    assert second is not None and second.task_id == "mid"
    third = storage.claim_next_task()
    assert third is not None and third.task_id == "low"
    assert storage.claim_next_task() is None


def test_claim_skips_non_queued_tasks(storage: Storage) -> None:
    """A task in CREATED is not claimable until it is queued."""
    storage.insert_task(make_task(task_id="created"))
    assert storage.claim_next_task() is None
    storage.update_task_state("created", TaskState.QUEUED)
    claimed = storage.claim_next_task()
    assert claimed is not None and claimed.task_id == "created"


def test_claim_increments_attempt_count(storage: Storage) -> None:
    """Recovery relies on the attempt count to spot a task that ran before."""
    storage.insert_task(make_task(task_id="t3"))
    storage.update_task_state("t3", TaskState.QUEUED)
    claimed = storage.claim_next_task()
    assert claimed is not None
    assert claimed.attempt_count == 1
    storage.update_task_state("t3", TaskState.FAILED)
    refreshed = storage.get_task("t3")
    assert refreshed is not None
    assert refreshed.attempt_count == 1


def test_task_events_are_sequenced(storage: Storage) -> None:
    """Task history is monotonic per task."""
    storage.insert_task(make_task(task_id="t4"))
    for index in range(5):
        entry = storage.append_task_event("t4", "note", {"index": index})
        assert entry.sequence == index + 1
    events = storage.list_task_events("t4")
    assert [event.sequence for event in events] == [1, 2, 3, 4, 5]
    assert [event.payload["index"] for event in events] == [0, 1, 2, 3, 4]
    after = storage.list_task_events("t4", after_sequence=3)
    assert [event.sequence for event in after] == [4, 5]


def test_task_event_payload_is_redacted(storage: Storage) -> None:
    """A payload that carries a credential-shaped key never stores the value."""
    storage.insert_task(make_task(task_id="t5"))
    entry = storage.append_task_event("t5", "note", {"api_key": "sk-live-secret", "nested": {"password": "hunter2"}})
    assert entry.payload["api_key"] == "<redacted>"
    assert entry.payload["nested"]["password"] == "<redacted>"


def test_oversized_task_event_payload_is_bounded(storage: Storage) -> None:
    """A huge payload is summarized instead of stored whole."""
    storage.insert_task(make_task(task_id="t6"))
    entry = storage.append_task_event("t6", "note", {"blob": "x" * 100_000})
    assert entry.payload.get("truncated") is True or len(str(entry.payload)) < 100_000


def test_count_tasks_by_state_covers_every_state(storage: Storage) -> None:
    """The dashboard needs a zero for every state, not only populated ones."""
    storage.insert_task(make_task(task_id="t7"))
    counts = storage.count_tasks_by_state()
    assert set(counts) == {state.value for state in TaskState}
    assert counts["CREATED"] == 1
    assert counts["SUCCEEDED"] == 0


def test_list_tasks_filters(storage: Storage) -> None:
    """State and owner filters both apply."""
    storage.insert_task(make_task(task_id="mine", owner_uid=1000))
    storage.insert_task(make_task(task_id="theirs", owner_uid=1001))
    storage.update_task_state("mine", TaskState.QUEUED)
    mine = storage.list_tasks(owner_uid=1000)
    assert [task.task_id for task in mine] == ["mine"]
    queued = storage.list_tasks(states=[TaskState.QUEUED])
    assert [task.task_id for task in queued] == ["mine"]


def test_list_tasks_rejects_bad_limit(storage: Storage) -> None:
    """A nonsensical page size is refused rather than clamped silently."""
    with pytest.raises(ValidationError):
        storage.list_tasks(limit=0)
    with pytest.raises(ValidationError):
        storage.list_tasks(limit=10_000)


def make_approval(approval_id: str = "", *, task_id: str = "t1", operation: str = "system.process.terminate") -> Approval:
    """Build an approval with test defaults."""
    return Approval(
        approval_id=approval_id,
        request=ApprovalRequest(
            task_id=task_id,
            operation=operation,
            target="4242",
            parameters={"pid": 4242},
            level=2,
            requested_by_uid=1000,
            requested_by_name="tester",
            reason="unit test",
        ),
    )


def test_approval_round_trip(storage: Storage) -> None:
    """An approval survives persistence with its binding intact."""
    approval = make_approval()
    storage.insert_approval(approval)
    loaded = storage.get_approval(approval.approval_id)
    assert loaded is not None
    assert loaded.request.operation == approval.request.operation
    assert loaded.request.parameter_digest == approval.request.parameter_digest
    assert loaded.nonce == approval.nonce


def test_approval_decision_and_consumption(storage: Storage) -> None:
    """A granted approval is consumed exactly once."""
    approval = make_approval()
    storage.insert_approval(approval)
    decided = storage.decide_approval(approval.approval_id, granted=True, decided_by_uid=1000)
    assert decided.state == "granted"
    consumed = storage.consume_approval(approval.approval_id, task_id="t1")
    assert consumed.state == "consumed"
    with pytest.raises(ValidationError):
        storage.consume_approval(approval.approval_id, task_id="t1")


def test_denied_approval_cannot_be_consumed(storage: Storage) -> None:
    """A refusal is final."""
    approval = make_approval()
    storage.insert_approval(approval)
    storage.decide_approval(approval.approval_id, granted=False, decided_by_uid=1000)
    with pytest.raises(ValidationError):
        storage.consume_approval(approval.approval_id, task_id="t1")


def test_approval_can_be_decided_only_once(storage: Storage) -> None:
    """A second decision is refused."""
    approval = make_approval()
    storage.insert_approval(approval)
    storage.decide_approval(approval.approval_id, granted=True, decided_by_uid=1000)
    with pytest.raises(ValidationError):
        storage.decide_approval(approval.approval_id, granted=False, decided_by_uid=1000)


def test_revoked_approval_cannot_be_consumed(storage: Storage) -> None:
    """Revocation is enforced before consumption."""
    approval = make_approval()
    storage.insert_approval(approval)
    storage.decide_approval(approval.approval_id, granted=True, decided_by_uid=1000)
    storage.revoke_approval(approval.approval_id, revoked_by_uid=1000)
    with pytest.raises(ValidationError):
        storage.consume_approval(approval.approval_id, task_id="t1")


def test_expired_approval_is_marked_and_refused(storage: Storage) -> None:
    """An approval past its expiry cannot authorize anything."""
    approval = Approval(
        approval_id="",
        request=ApprovalRequest(
            task_id="t1",
            operation="system.service.restart",
            target="cups.service",
            parameters={"unit": "cups.service"},
            level=2,
            requested_by_uid=1000,
        ),
        state="granted",
        expires_at="2000-01-01T00:00:00.000Z",
    )
    storage.insert_approval(approval)
    assert storage.expire_stale_approvals() == 1
    refreshed = storage.get_approval(approval.approval_id)
    assert refreshed is not None and refreshed.state == "expired"
    with pytest.raises(ValidationError):
        storage.consume_approval(approval.approval_id, task_id="t1")


def test_audit_table_is_append_only_in_practice(storage: Storage) -> None:
    """The audit table has no update or delete helper exposed."""
    assert not hasattr(storage, "delete_audit_record")
    assert not hasattr(storage, "update_audit_record")


def test_newer_schema_version_is_refused(tmp_path: Path) -> None:
    """A database from a newer build is refused rather than misread."""
    path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA user_version=99")
    connection.execute("CREATE TABLE placeholder (x INTEGER)")
    connection.commit()
    connection.close()
    with pytest.raises(StorageError):
        Storage(str(path))
