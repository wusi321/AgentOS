# ADR-008: Persist tasks in SQLite behind one state machine, and never retry an unconfirmed side effect

**Status:** accepted
**Date:** 2026-10-13

## Context

A task outlives the window that started it, the process that ran it, and possibly
the boot. The service can be killed at any moment, including between "the operation
started" and "the operation finished". The design requires that a restart not
re-run an unconfirmed side effect, and that illegal state transitions be refused.

## Options considered

1. **In-memory task state**, with the database as a log only.
2. **A row per task with a free-form status string.**
3. **A row per task plus an event history, with one explicit state machine
   module owning every transition, and a `side_effects_possible` flag.**

## Decision

Option 3.

* `agentos/tasks/states.py` is the only place a legal transition is defined.
* `Storage.update_task_state` calls `require_transition` before writing, so the
  database cannot hold a state the machine does not permit.
* Every state change also appends a task-history row and publishes an event.
* `TaskRecord.side_effects_possible` is set before a side-effecting operation
  begins, and recovery consults it.

## Reasoning

**Why the state machine is a separate module.** Scattering "set state to X" across
the task manager, the IPC server, and the UI is how a system acquires states nobody
designed. Putting the transition table in one place makes an illegal transition a
raised error with the allowed set in its details, and makes the table testable
exhaustively: the suite asserts every legal pair is accepted and every other pair
is refused.

**Why an event history rather than only the current row.** The row answers "what
is it now". An operator asking "what happened to this task" needs the sequence,
including the policy decisions. Storing both means the audit trail and the task view
agree, and that a failure is explicable after the fact.

**Why `side_effects_possible` exists.** This is the decision this ADR is really
about. When the service restarts and finds a task in `RUNNING`, only two things can
be true: the operation had not started, or it had and its outcome is unknown.
Requeueing is correct in the first case and dangerous in the second, because
re-running an unconfirmed operation can double its effect — a second restart, a
second signal to a pid that has since been reused. Since the two cases cannot be
distinguished after the fact, the flag records what was known *before* the
operation started.

Consequently:

| State found at startup | `side_effects_possible` | Action |
|---|---|---|
| `CREATED`, `QUEUED`, `PAUSED` | either | requeue: nothing had run |
| `WAITING_APPROVAL` | either | requeue: awaiting a decision, no operation |
| `RUNNING`, `RECOVERING` | false | requeue: the operation had not begun |
| `RUNNING`, `RECOVERING` | true | **fail**, with a message telling the operator to check the target |
| any terminal state | either | leave alone |

**Why failing is the right answer.** A failed task with an explicit message is
recoverable: the operator checks the target and resubmits. A silently doubled
restart is not, and neither is a signal delivered to a recycled pid. Reporting an
uncertainty is strictly better than acting on it.

**Why SQLite, with these settings.** WAL so that the CLI and the desktop can read
while the worker writes; `busy_timeout` so a concurrent reader is not an error;
`foreign_keys=ON` so a task's history cannot outlive it. One connection guarded by
a lock, opened with `check_same_thread=False` because the event loop serves IPC
while the task manager runs work — with the lock as the real serialization point.

## Schema migration

`PRAGMA user_version` carries a monotonic schema version and a registered migration
function per step. A database whose version is **newer** than the build is refused
rather than read, because reading a schema the code does not know is how data is
silently corrupted. Every migration runs inside one transaction; a failure rolls
back and the service refuses to start.

## Security impact

* The database is written only by the service account, in a directory the service
  owns, under `ProtectSystem=strict`.
* Event payloads and task results are redacted and size-bounded on the way in, so a
  credential-shaped key cannot be persisted even if a caller supplies one.
* Removing or purging the package does not delete the database, so the record of
  what an agent did survives an uninstall. Deleting it is a deliberate act.
* The task history is what makes an approval decision explicable afterwards: it
  carries the policy decision, the rule name, and the reason alongside the outcome.

## Evidence

* `tests/unit/test_task_states.py` — every legal pair accepted, every other pair
  refused, and the recovery rule asserted for each state and each flag value.
* `tests/unit/test_storage.py` — round trips, illegal transitions refused at the
  storage layer, atomic claiming, sequenced history, redaction, and a newer schema
  version refused.
* `scripts/smoke-core.py` — a real service on a real socket shows the state
  sequence and the history of a task that ran.

## Maintenance cost

Low. Adding a schema change means a new migration function and a version bump. The
transition table is a single dictionary; adding a state means deciding its outgoing
transitions in one place and letting the exhaustive test tell you what you forgot.

## Review trigger

Revisit when a task can contain more than one side-effecting operation or a DAG of
them, because a single boolean is then too coarse: the flag would have to become a
per-operation record with its own confirmation state. Also revisit if a second
service instance is ever required, since the single-writer assumption would need to
become an explicit claim protocol.
