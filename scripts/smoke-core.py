#!/usr/bin/env python3
"""End-to-end smoke test of the AgentOS Core service.

Run from a source checkout:

    python3 scripts/smoke-core.py

It starts a Core service on temporary paths, drives it over its real IPC socket
with the real client, and checks the whole chain: task creation, a runtime turn,
a policy denial, an approval request, a bound-parameter refusal, and the audit
trail. No model credential and no network access are required.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentos.core.config import Config, CoreSettings  # noqa: E402
from agentos.core.host import CoreHost, configure_logging  # noqa: E402
from agentos.core.ipc_client import IpcClient  # noqa: E402
from agentos.errors import AgentOSError  # noqa: E402
from agentos.runtime.factory import RuntimeSettings  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record and print one assertion."""
    status = "ok  " if condition else "FAIL"
    print(f"  [{status}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(label)


def wait_for_socket(path: str, timeout: float = 15.0) -> bool:
    """Wait until the IPC socket accepts a connection."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.5)
            try:
                probe.connect(path)
                probe.close()
                return True
            except OSError:
                pass
            finally:
                probe.close()
        time.sleep(0.1)
    return False


def main() -> int:
    """Run the smoke test and return a process exit status."""
    workspace = Path(tempfile.mkdtemp(prefix="agentos-smoke-"))
    interface = tempfile.mkdtemp(prefix="agentos-if-")
    service = os.path.join(workspace, "core.sock")
    config = Config(
        core=CoreSettings(
            socket_path=service,
            database_path=os.path.join(workspace, "state.sqlite3"),
            state_directory=workspace,
            cache_directory=os.path.join(workspace, "cache"),
            log_directory=os.path.join(workspace, "log"),
            log_level="WARNING",
            workspace_root=os.path.join(workspace, "workspaces"),
        ),
        runtime=RuntimeSettings(runtime_id="mock", workspace_root=os.path.join(workspace, "workspaces")),
        # The shipped default policy is read-only; use the example site policy so
        # that the P2 approval path can be exercised end to end.
        policy_directory=str(REPO_ROOT / "configs" / "policies-example"),
        service_unit_allowlist=(),
    )
    print(f"workspace: {workspace}")
    print(f"socket:    {service}")
    logger = configure_logging("WARNING")
    host = CoreHost(config, logger=logger)

    import asyncio
    import threading

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def serve() -> None:
        """Run the Core event loop for the duration of the smoke test."""
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    service_thread = threading.Thread(target=serve, name="agentos-smoke-loop", daemon=True)
    service_thread.start()
    ready.wait(5.0)
    try:
        asyncio.run_coroutine_threadsafe(host.start(), loop).result(30.0)
        print("service")
        check("core socket is accepting connections", wait_for_socket(service), service)
        client = IpcClient(service, timeout_seconds=30.0)
        hello = client.call("hello")
        check("hello returns the service identity", hello.get("service") == "agentos-core", str(hello.get("version")))
        check("kernel-reported peer uid matches this process", hello["peer"]["uid"] == os.getuid())

        print("health")
        health = client.call("health")
        check("health reports the database", health["database"]["ok"] is True)
        check(
            "health reports the mock runtime as available",
            health["runtime"].get("available") is not False,
            str(health["runtime"].get("detail")),
        )

        print("read-only system tools")
        snapshot = client.call("system.snapshot")
        check("snapshot reports CPU cores", snapshot["cpu"]["core_count"] >= 1)
        check("snapshot reports memory", snapshot["memory"]["total_mib"] > 0)
        processes = client.call("system.read", {"tool": "system.processes.list", "parameters": {"limit": 5}})
        check("process listing returns rows", processes["result"]["count"] >= 1)

        print("tasks")
        created = client.call("task.create", {"prompt": "Summarise system pressure.", "level_cap": "P0"})
        task_id = created["task"]["task_id"]
        check("task is queued", created["task"]["state"] == "QUEUED", task_id)
        settled = None
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            settled = client.call("task.get", {"task_id": task_id})["task"]
            if settled["state"] in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}:
                break
            time.sleep(0.2)
        check("task reaches SUCCEEDED with the mock runtime", settled["state"] == "SUCCEEDED", str(settled.get("error_message")))
        check("task recorded assistant output", bool(settled["result_summary"]), settled["result_summary"][:80])
        history = client.call("task.history", {"task_id": task_id})["events"]
        check("task history recorded runtime events", len(history) >= 3, f"{len(history)} entries")

        print("policy")
        allowed = client.call("policy.evaluate", {"tool": "system.processes.list", "parameters": {"limit": 3}})
        check("P0 read is allowed", allowed["decision"] == "allow", allowed["reason"])
        denied = client.call("policy.evaluate", {"tool": "system.packages.install", "parameters": {}})
        check("P3 administrative tool is denied", denied["decision"] == "deny", denied["reason"])
        undeclared = client.call("policy.evaluate", {"tool": "system.made.up", "parameters": {}})
        check("undeclared tool is denied", undeclared["decision"] == "deny", undeclared["reason"])
        bad_params = client.call("policy.evaluate", {"tool": "system.processes.list", "parameters": {"limit": 9999}})
        check("out-of-range parameter is denied", bad_params["decision"] == "deny", bad_params["reason"])

        print("approvals")
        try:
            client.call(
                "approval.request",
                {"task_id": task_id, "tool": "system.service.restart", "parameters": {"unit": "sshd.service"}},
            )
            check("unallowlisted restart target is refused", False, "the request was accepted")
        except AgentOSError as error:
            check("unallowlisted restart target is refused", error.code == "agentos.policy_denied", error.code)
        try:
            client.call(
                "approval.request",
                {"task_id": task_id, "tool": "system.process.terminate", "parameters": {"pid": os.getpid()}},
            )
            check("a P0 task cannot obtain a P2 approval", False, "the request was accepted")
        except AgentOSError as error:
            check(
                "a P0 task cannot obtain a P2 approval",
                error.code == "agentos.policy_denied" and "cap" in error.message,
                error.code,
            )

        elevated = client.call("task.create", {"prompt": "Controlled operation demo.", "level_cap": "P2"})
        elevated_id = elevated["task"]["task_id"]
        check("a P2 task can be created", elevated["task"]["level_cap_label"].startswith("P2"), elevated["task"]["level_cap_label"])
        requested = client.call(
            "approval.request",
            {
                "task_id": elevated_id,
                "tool": "system.process.terminate",
                "parameters": {"pid": os.getpid()},
                "reason": "smoke test",
            },
        )
        approval = requested["approval"]
        check("approval is pending", approval["state"] == "pending", approval["approval_id"])
        check("approval carries a nonce for its owner", bool(approval.get("nonce")))
        check(
            "approval states the effect in plain language",
            "SIGTERM" in str(approval.get("effect")),
            str(approval.get("effect")),
        )
        decided = client.call("approval.decide", {"approval_id": approval["approval_id"], "grant": True, "note": "smoke"})
        check("approval is granted", decided["approval"]["state"] == "granted")
        nonce = decided["approval"]["nonce"]
        check("grant returns the single-use nonce", bool(nonce))
        replay = client.call("approval.get", {"approval_id": approval["approval_id"]})["approval"]
        check("approval stays granted until consumed", replay["state"] == "granted")
        revoked = client.call("approval.revoke", {"approval_id": approval["approval_id"]})["approval"]
        check("a granted approval can be revoked", revoked["state"] == "revoked")

        print("audit")
        summary = client.call("audit.summary")
        check("audit recorded the task", summary["total"] >= 1, json.dumps(summary["by_kind"]))
        records = client.call("audit.query", {"limit": 50})["records"]
        check("audit recorded the approval decision", any(r["kind"] == "approval.decided" for r in records))

        print("events")
        subscription = client.call("events.subscribe")
        token = subscription["token"]
        client.call("task.create", {"prompt": "Second task for event coverage."})
        seen = 0
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and seen == 0:
            batch = client.call("events.poll", {"token": token, "limit": 50})
            seen += batch["count"]
            time.sleep(0.2)
        check("event subscription delivered events", seen >= 1, f"{seen} events")
        check("unsubscribe succeeds", client.call("events.unsubscribe", {"token": token})["removed"] is True)

        print("access control")
        try:
            client.call("task.get", {"task_id": "00000000-0000-0000-0000-000000000000"})
            check("a foreign task id is refused", False, "the read succeeded")
        except AgentOSError as error:
            check("a foreign task id is refused", error.code == "agentos.ipc_access_denied", error.code)
    finally:
        try:
            asyncio.run_coroutine_threadsafe(host.stop(), loop).result(30.0)
        except Exception:  # noqa: BLE001 - teardown must not mask the result
            pass
        loop.call_soon_threadsafe(loop.stop)
        service_thread.join(timeout=10.0)
        loop.close()
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(interface, ignore_errors=True)

    print("")
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
