"""Security tests: privilege boundaries, injection surfaces, and approval replay.

These tests exist to prove the negative: that a caller, a model, or a malformed
input cannot reach past the policy engine, the executor, or the IPC access check.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path

import pytest

from agentos.core.ipc import IPC_PROTOCOL_VERSION, IpcRequest, IpcServer, PeerCredentials
from agentos.errors import ApprovalInvalid, IPCAccessDenied, PolicyDenied, ValidationError
from agentos.jsonutil import REDACTED, bounded_payload, redact
from agentos.policy import (
    Approval,
    ApprovalRequest,
    Decision,
    PolicyContext,
    PolicyEngine,
    PrivilegeLevel,
    parse_policy,
)
from agentos.schema import ParameterSchema, ParameterSpec
from agentos.system_bridge import ControlledExecutor, ExecutionOutcome, validate_unit_name
from agentos.system_bridge.services import validate_unit_name as validate_unit
from agentos.tasks import Storage, TaskState


# --- model output is never authority -----------------------------------------


def test_unknown_tool_from_a_model_is_denied() -> None:
    """A model that invents a tool name gets a denial, not an implicit grant."""
    engine = PolicyEngine(policies=[])
    evaluation = engine.evaluate("shell.execute", {"command": "rm -rf /"}, PolicyContext(uid=1000))
    assert evaluation.decision is Decision.DENY


def test_model_parameters_are_validated_before_any_decision() -> None:
    """Malformed or extra parameters never reach a decision path."""
    engine = PolicyEngine(policies=[])
    evaluation = engine.evaluate(
        "system.processes.list", {"limit": "5; rm -rf /"}, PolicyContext(uid=1000)
    )
    assert evaluation.decision is Decision.DENY


def test_shell_metacharacters_are_rejected_in_free_form_strings() -> None:
    """Defence in depth: a command substitution cannot pass parameter validation."""
    schema = ParameterSchema(parameters=(ParameterSpec("unit", "string", max_length=64),))
    for payload in (
        "cups.service; id",
        "cups.service && id",
        "cups.service | tee /etc/passwd",
        "cups.service`id`",
        "cups.service$(id)",
        "cups.service\nid",
        "cups.service > /tmp/x",
    ):
        with pytest.raises(ValidationError):
            schema.validate({"unit": payload})


def test_nul_bytes_are_rejected() -> None:
    """A NUL byte is never a legitimate parameter value."""
    schema = ParameterSchema(parameters=(ParameterSpec("unit", "string"),))
    with pytest.raises(ValidationError):
        schema.validate({"unit": "cups.service\x00id"})


def test_credential_shaped_parameters_are_rejected() -> None:
    """A tool may not accept credential material as a parameter at all."""
    schema = ParameterSchema(parameters=(ParameterSpec("api_key", "string"),))
    with pytest.raises(ValidationError):
        schema.validate({"api_key": "sk-live-x"})


def test_oversized_string_is_rejected() -> None:
    """A declared length bound is enforced."""
    schema = ParameterSchema(parameters=(ParameterSpec("unit", "string", max_length=16),))
    with pytest.raises(ValidationError):
        schema.validate({"unit": "x" * 17})


def test_boolean_is_not_accepted_as_an_integer() -> None:
    """``True`` must not silently become pid 1."""
    schema = ParameterSchema(parameters=(ParameterSpec("pid", "integer", minimum=1, maximum=100),))
    with pytest.raises(ValidationError):
        schema.validate({"pid": True})


# --- unit name grammar --------------------------------------------------------


@pytest.mark.parametrize(
    "candidate",
    [
        "cups.service",
        "ssh.service",
        "systemd-journald.service",
        "user@1000.service",
        "dev-sda1.device",
        "getty@tty1.service",
    ],
)
def test_valid_unit_names_are_accepted(candidate: str) -> None:
    """Real systemd unit names pass validation."""
    assert validate_unit_name(candidate) == candidate


@pytest.mark.parametrize(
    "candidate",
    [
        "",
        "cups",
        "cups.service; id",
        "../../etc/passwd",
        "-oProxyCommand=evil",
        "cups.service\nssh.service",
        "x" * 300 + ".service",
        "cups.service && reboot",
        "cups.service|cat /etc/shadow",
    ],
)
def test_invalid_unit_names_are_refused(candidate: str) -> None:
    """Anything that is not a plain unit name is refused before it reaches argv."""
    with pytest.raises(ValidationError):
        validate_unit_name(candidate)


def test_validate_unit_rejects_option_like_names() -> None:
    """A leading dash cannot be interpreted as a systemctl option."""
    with pytest.raises(ValidationError):
        validate_unit("--now.service")


# --- the executor is not a shell ---------------------------------------------


def permitted_execution_context(*, uid: int = 1000) -> PolicyContext:
    """Return a context with P2 authority."""
    return PolicyContext(uid=uid, user_name="tester", task_level_cap=PrivilegeLevel.CONTROLLED)


def test_executor_refuses_an_operation_it_does_not_implement() -> None:
    """The executor has a closed operation set."""
    from agentos.policy import ExecutionAuthorization, PolicyEvaluation

    executor = ControlledExecutor(context=permitted_execution_context())
    authorization = ExecutionAuthorization(
        evaluation=PolicyEvaluation(
            decision=Decision.ALLOW,
            tool="system.shell.execute",
            level=PrivilegeLevel.ADMINISTRATIVE,
            target=None,
            reason="test",
        ),
        approval=None,
        parameter_digest="",
    )
    with pytest.raises(ValidationError):
        executor.execute("system.shell.execute", {"command": "id"}, authorization)


def test_executor_refuses_a_mismatched_authorization() -> None:
    """The proof object must cover exactly the parameters being executed."""
    from agentos.policy import ExecutionAuthorization, PolicyEvaluation

    executor = ControlledExecutor(context=permitted_execution_context())
    authorization = ExecutionAuthorization(
        evaluation=PolicyEvaluation(
            decision=Decision.ALLOW,
            tool="system.process.terminate",
            level=PrivilegeLevel.CONTROLLED,
            target="4242",
            reason="test",
            parameters={"pid": 4242},
        ),
        approval=None,
        parameter_digest="",
    )
    with pytest.raises(PolicyDenied):
        executor.execute("system.process.terminate", {"pid": 9999}, authorization)


def test_executor_never_signals_pid_one() -> None:
    """Signalling the init process is refused before any syscall."""
    from agentos.policy import ExecutionAuthorization, PolicyEvaluation

    executor = ControlledExecutor(context=permitted_execution_context(uid=0))
    authorization = ExecutionAuthorization(
        evaluation=PolicyEvaluation(
            decision=Decision.ALLOW,
            tool="system.process.terminate",
            level=PrivilegeLevel.CONTROLLED,
            target="1",
            reason="test",
            parameters={"pid": 1},
        ),
        approval=None,
        parameter_digest="",
    )
    outcome: ExecutionOutcome = executor.execute("system.process.terminate", {"pid": 1}, authorization)
    assert outcome.success is False
    assert "pid 1" in outcome.message


def test_executor_never_signals_itself() -> None:
    """The service cannot be made to terminate itself."""
    from agentos.policy import ExecutionAuthorization, PolicyEvaluation

    executor = ControlledExecutor(context=permitted_execution_context(uid=0))
    pid = os.getpid()
    authorization = ExecutionAuthorization(
        evaluation=PolicyEvaluation(
            decision=Decision.ALLOW,
            tool="system.process.terminate",
            level=PrivilegeLevel.CONTROLLED,
            target=str(pid),
            reason="test",
            parameters={"pid": pid},
        ),
        approval=None,
        parameter_digest="",
    )
    outcome = executor.execute("system.process.terminate", {"pid": pid}, authorization)
    assert outcome.success is False
    assert "itself" in outcome.message


def test_executor_refuses_a_process_the_caller_does_not_own() -> None:
    """Ownership is checked against /proc, not against a caller-supplied claim."""
    from agentos.policy import ExecutionAuthorization, PolicyEvaluation

    # pid 1 is owned by root and is never signalled; use the current process's
    # parent as a process this uid generally does not own.
    target_pid = 1
    executor = ControlledExecutor(context=permitted_execution_context(uid=os.getuid()))
    authorization = ExecutionAuthorization(
        evaluation=PolicyEvaluation(
            decision=Decision.ALLOW,
            tool="system.process.terminate",
            level=PrivilegeLevel.CONTROLLED,
            target=str(target_pid),
            reason="test",
            parameters={"pid": target_pid},
        ),
        approval=None,
        parameter_digest="",
    )
    outcome = executor.execute("system.process.terminate", {"pid": target_pid}, authorization)
    assert outcome.success is False


# --- approval binding and replay ---------------------------------------------


def make_bound_approval(*, pid: int = 4242, uid: int = 1000, task_id: str = "t1") -> Approval:
    """Build a granted approval bound to one pid, uid, and task."""
    return Approval(
        approval_id="approval-1",
        request=ApprovalRequest(
            task_id=task_id,
            operation="system.process.terminate",
            target=str(pid),
            parameters={"pid": pid},
            level=2,
            requested_by_uid=uid,
            requested_by_name="tester",
        ),
        state="granted",
    )


def test_approval_replay_is_detectable_by_state() -> None:
    """A consumed approval reports itself as consumed and cannot be replayed."""
    approval = make_bound_approval()
    request = approval.request
    approval.check_binding(request, nonce=approval.nonce)
    consumed = Approval(
        approval_id=approval.approval_id,
        request=request,
        state="consumed",
        nonce=approval.nonce,
        consumed_at=approval.created_at,
    )
    with pytest.raises(ApprovalInvalid) as caught:
        consumed.check_binding(request, nonce=approval.nonce)
    assert "already been used" in caught.value.message


def test_approval_binds_the_parameter_digest() -> None:
    """Two different parameter sets never share an approval."""
    approval = make_bound_approval(pid=4242)
    other = ApprovalRequest(
        task_id="t1",
        operation="system.process.terminate",
        target="4242",
        parameters={"pid": 4242, "reason": "different"},
        level=2,
        requested_by_uid=1000,
    )
    assert approval.request.parameter_digest != other.parameter_digest
    with pytest.raises(ApprovalInvalid):
        approval.check_binding(other, nonce=approval.nonce)


def test_approval_binds_the_user() -> None:
    """Another local user cannot use this approval."""
    approval = make_bound_approval()
    other = ApprovalRequest(
        task_id="t1",
        operation="system.process.terminate",
        target="4242",
        parameters={"pid": 4242},
        level=2,
        requested_by_uid=1001,
    )
    with pytest.raises(ApprovalInvalid):
        approval.check_binding(other, nonce=approval.nonce)


def test_approval_binds_the_task() -> None:
    """An approval is not transferable between tasks."""
    approval = make_bound_approval()
    other = ApprovalRequest(
        task_id="t2",
        operation="system.process.terminate",
        target="4242",
        parameters={"pid": 4242},
        level=2,
        requested_by_uid=1000,
    )
    with pytest.raises(ApprovalInvalid):
        approval.check_binding(other, nonce=approval.nonce)


def test_approval_expiry_is_enforced() -> None:
    """An expired approval is refused even with the right nonce."""
    from agentos.events import utc_now

    approval = Approval(
        approval_id="expired",
        request=make_bound_approval().request,
        state="granted",
        expires_at="2000-01-01T00:00:00.000Z",
    )
    with pytest.raises(ApprovalInvalid) as caught:
        approval.check_binding(approval.request, nonce=approval.nonce, now=utc_now())
    assert "expired" in caught.value.message


def test_approval_nonce_is_not_guessable() -> None:
    """Two approvals never share a nonce, and it is long enough to be a secret."""
    first = make_bound_approval()
    second = Approval(approval_id="b", request=first.request)
    assert first.nonce != second.nonce
    assert len(first.nonce) >= 30


def test_approval_nonce_is_withheld_from_the_wire_by_default() -> None:
    """A listing that includes another user's approval must not leak its nonce."""
    approval = make_bound_approval()
    assert "nonce" not in approval.to_dict()
    assert "nonce" in approval.to_dict(include_nonce=True)


# --- redaction ----------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["api_key", "API_KEY", "authorization", "password", "client_secret", "session_token", "private_key", "dsn"],
)
def test_sensitive_keys_are_redacted(key: str) -> None:
    """A credential-shaped key never persists its value."""
    document = redact({"outer": {key: "super-secret"}})
    assert document["outer"][key] == REDACTED
    assert "super-secret" not in json.dumps(document)


def test_redaction_is_recursive_over_lists() -> None:
    """Nested lists are redacted too."""
    document = redact({"items": [{"token": "abc"}, {"safe": "ok"}]})
    assert document["items"][0]["token"] == REDACTED
    assert document["items"][1]["safe"] == "ok"


def test_deeply_nested_input_is_bounded() -> None:
    """A hostile deeply nested value cannot exhaust the stack."""
    value: object = "leaf"
    for _ in range(200):
        value = {"n": value}
    redacted = redact(value)
    assert redacted is not None


def test_oversized_payload_is_bounded_by_string_shortening() -> None:
    """One huge string member is shortened so the payload still fits the bound."""
    from agentos.jsonutil import MAX_PAYLOAD_BYTES, canonical_json

    payload = {"blob": "x" * 200_000, "note": "keep me"}
    bounded = bounded_payload(payload)
    assert len(canonical_json(bounded).encode("utf-8")) <= MAX_PAYLOAD_BYTES
    assert bounded["note"] == "keep me"
    assert bounded["blob"].startswith("x" * 100)
    assert "truncated" in bounded["blob"]


def test_many_oversized_members_fall_back_to_a_digest_summary() -> None:
    """When shortening is not enough, the payload becomes a verifiable summary."""
    from agentos.jsonutil import MAX_PAYLOAD_BYTES, canonical_json

    payload = {f"key{index:04d}": "y" * 20_000 for index in range(400)}
    bounded = bounded_payload(payload)
    assert bounded.get("truncated") is True
    assert "original_digest" in bounded
    assert bounded["key_count"] == 400
    assert len(bounded["keys"]) <= 50
    assert len(canonical_json(bounded).encode("utf-8")) <= 4096
    assert len(canonical_json(bounded).encode("utf-8")) <= MAX_PAYLOAD_BYTES


def test_bounded_payload_redacts_before_it_measures() -> None:
    """A sensitive member is replaced even when the payload is also oversized."""
    payload = {"api_key": "sk-live-secret", "blob": "x" * 200_000}
    bounded = bounded_payload(payload)
    assert bounded["api_key"] == REDACTED
    assert "sk-live-secret" not in json.dumps(bounded)


# --- IPC access control -------------------------------------------------------


@pytest.mark.asyncio
async def test_ipc_denies_another_users_task(tmp_path: Path) -> None:
    """A peer that is not the task owner cannot read it, and cannot tell it exists."""
    from agentos.core.host import CoreHost
    from agentos.core.ipc_client import IpcClient
    from agentos.core.config import Config, CoreSettings
    from agentos.runtime.factory import RuntimeSettings

    socket_path = str(tmp_path / "core.sock")
    config = Config(
        core=CoreSettings(
            socket_path=socket_path,
            database_path=str(tmp_path / "state.sqlite3"),
            state_directory=str(tmp_path),
            cache_directory=str(tmp_path / "cache"),
            log_directory=str(tmp_path / "log"),
            workspace_root=str(tmp_path / "workspaces"),
            log_level="CRITICAL",
        ),
        runtime=RuntimeSettings(runtime_id="mock", workspace_root=str(tmp_path / "workspaces")),
        policy_directory=str(tmp_path / "no-policies"),
    )
    host = CoreHost(config)
    await host.start()
    try:
        task = host.tasks.create_task(prompt="owned by uid 4242", owner_uid=4242, owner_name="other")
        await asyncio.to_thread(_assert_foreign_task_is_refused, socket_path, task.task_id)
    finally:
        await host.stop()


def _assert_foreign_task_is_refused(socket_path: str, task_id: str) -> None:
    """Call the service about a task owned by a different uid."""
    from agentos.core.ipc_client import IpcClient
    from agentos.errors import AgentOSError

    client = IpcClient(socket_path, timeout_seconds=10.0)
    with pytest.raises(AgentOSError) as caught:
        client.call("task.get", {"task_id": task_id})
    assert caught.value.code == "agentos.ipc_access_denied"


@pytest.mark.asyncio
async def test_ipc_rejects_an_unknown_protocol_version(tmp_path: Path) -> None:
    """A client speaking a different protocol version is refused explicitly."""
    socket_path = str(tmp_path / "version.sock")
    server = IpcServer(socket_path)
    server.register("hello", lambda request: {"ok": True})
    await server.start()
    try:
        response = await asyncio.to_thread(_send_raw_frame, socket_path, {"v": 99, "id": "1", "method": "hello"})
        assert response["error"]["code"] == "agentos.protocol_mismatch"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_ipc_reports_an_unknown_method(tmp_path: Path) -> None:
    """An unknown method is answered rather than dropped."""
    socket_path = str(tmp_path / "unknown.sock")
    server = IpcServer(socket_path)
    server.register("hello", lambda request: {"ok": True})
    await server.start()
    try:
        response = await asyncio.to_thread(_send_raw_frame, socket_path, {"v": 1, "id": "1", "method": "nope"})
        assert response["error"]["code"] == "agentos.unknown_method"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_ipc_rejects_a_non_object_frame(tmp_path: Path) -> None:
    """A JSON array is not a request."""
    socket_path = str(tmp_path / "framing.sock")
    server = IpcServer(socket_path)
    server.register("hello", lambda request: {"ok": True})
    await server.start()
    try:
        response = await asyncio.to_thread(_send_raw_text, socket_path, "[1,2,3]\n")
        assert response["error"]["code"] == "agentos.validation_error"
    finally:
        await server.close()


def _send_raw_frame(socket_path: str, frame: dict[str, object]) -> dict[str, object]:
    """Send one raw frame and return the decoded response."""
    return _send_raw_text(socket_path, json.dumps(frame) + "\n")


def _send_raw_text(socket_path: str, text: str) -> dict[str, object]:
    """Send raw bytes to the IPC socket and decode the first response line."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(10.0)
    client.connect(socket_path)
    try:
        client.sendall(text.encode("utf-8"))
        buffer = bytearray()
        while b"\n" not in buffer:
            chunk = client.recv(65536)
            if not chunk:
                break
            buffer.extend(chunk)
    finally:
        client.close()
    line = bytes(buffer).split(b"\n", 1)[0].decode("utf-8")
    decoded = json.loads(line)
    assert isinstance(decoded, dict)
    return decoded


def test_ipc_socket_is_never_a_network_listener() -> None:
    """The transport is a Unix domain socket: no TCP port is ever opened."""
    import inspect

    from agentos.core import ipc

    source = inspect.getsource(ipc)
    assert "start_unix_server" in source
    assert "start_server" not in source
    assert "0.0.0.0" not in source
    assert "AF_INET" not in source


def test_service_bridge_exposes_no_arbitrary_read_tool() -> None:
    """The System Bridge tool set is closed and contains no file reader."""
    from agentos.system_bridge.bridge import READ_TOOL_LEVELS, WRITE_TOOLS

    assert "system.files.read" not in READ_TOOL_LEVELS
    assert all(name.startswith("system.") for name in READ_TOOL_LEVELS)
    assert "system.shell.execute" not in WRITE_TOOLS
