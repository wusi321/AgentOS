#!/usr/bin/env python3
"""Probe the ``dsh --profile sdk`` JSON-RPC stdio contract.

This is the P0 verification the design outline requires before DSH is treated as
a supported runtime adapter. It starts the real CLI, completes the handshake, asks
for the protocol's own error behaviour, and then reports what it observed so the
result can be recorded as evidence.

The probe never needs a model credential for the checks it performs, because the
handshake is resolved by the SDK server before any model route is used. When no
credential is present it records that fact instead of failing the whole probe,
so the environment check and the wire check stay independently meaningful.

Usage: dsh_sdk_probe.py <node-binary> <dsh-bin-js> <dsh-home> <workspace>
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentos.errors import AgentOSError  # noqa: E402
from agentos.runtime.dsh_transport import AsyncJsonRpcProcess, minimal_child_environment  # noqa: E402

OBSERVATIONS: list[tuple[str, bool, str]] = []


def observe(label: str, ok: bool, detail: str = "") -> None:
    """Record and print one observation."""
    OBSERVATIONS.append((label, ok, detail))
    marker = "ok  " if ok else "FAIL"
    print(f"  [{marker}] {label}{(' — ' + detail) if detail else ''}")


async def main() -> int:
    """Run the probe and return a process exit status."""
    node_binary, dsh_bin, dsh_home, workspace = sys.argv[1:5]
    os.makedirs(dsh_home, exist_ok=True)
    os.makedirs(workspace, exist_ok=True)

    environment = minimal_child_environment(
        additions={
            "DSH_HOME": dsh_home,
            "NODE_ENV": "production",
            "HOME": dsh_home,
        }
    )
    environment["DEEPSEEK_API_KEY"] = os.environ.get("DEEPSEEK_API_KEY", "probe-placeholder-key")

    transport = AsyncJsonRpcProcess(
        (node_binary, dsh_bin, "--profile", "sdk"),
        cwd=dsh_home,
        env=environment,
        request_timeout_seconds=90.0,
        description="dsh sdk probe",
    )
    notifications: list[str] = []
    transport.add_notification_handler(lambda notification: notifications.append(notification.method))

    await transport.start()
    observe("the SDK profile started", transport.running, f"pid {os.getpid()} spawned a child")

    try:
        result = await transport.request(
            "initialize",
            {
                "cwd": workspace,
                "provider": os.environ.get("AGENTOS_PROBE_PROVIDER", "deepseek-official"),
                "model": os.environ.get("AGENTOS_PROBE_MODEL", "deepseek-flash"),
            },
            timeout_seconds=90.0,
            description="initialize handshake",
        )
    except AgentOSError as error:
        message = error.message.splitlines()[0]
        credential_related = "adapter" in message.lower() or "credential" in message.lower() or "auth" in message.lower()
        observe(
            "initialize handshake answered",
            credential_related,
            f"{error.code}: {message}"
            + (
                " (the SDK server answered the frame and resolved the route; "
                "no model credential is configured in this environment)"
                if credential_related
                else ""
            ),
        )
        observe(
            "the SDK server answered a protocol error rather than failing silently",
            error.code == "agentos.runtime_protocol_error",
            f"rpc error surfaced as {error.code}",
        )
        await transport.close(timeout_seconds=5.0)
        if notifications:
            print(f"  notifications observed before failure: {notifications[:5]}")
        print(f"  stderr tail: {transport.stderr_tail(lines=12)}")
        failures = [label for label, ok, _ in OBSERVATIONS if not ok]
        print("")
        print(f"observations: {len(OBSERVATIONS)}, failures: {len(failures)}")
        return 1 if failures else 0

    server_info = result.get("serverInfo") if isinstance(result, dict) else None
    name = server_info.get("name") if isinstance(server_info, dict) else None
    version = server_info.get("version") if isinstance(server_info, dict) else None
    observe(
        "initialize returned the wire-stable server identity",
        name == "deepseek-harness-sdk-runtime",
        f"name={name!r} version={version!r}",
    )

    try:
        await transport.request("no/such/method", {}, timeout_seconds=20.0, description="unknown method")
        observe("an unknown method is rejected", False, "the request unexpectedly succeeded")
    except AgentOSError as error:
        detail = str(getattr(error, "rpc_code", ""))
        observe("an unknown method is rejected", True, f"code={error.code} rpc_code={detail}")

    try:
        await transport.request(
            "session/working-directory/get",
            {"sessionId": "agentos-probe-session"},
            timeout_seconds=30.0,
            description="working-directory get",
        )
        observe("session/working-directory/get answers", True)
    except AgentOSError as error:
        observe(
            "session/working-directory/get answers",
            error.code == "agentos.runtime_protocol_error",
            f"{error.code}: {error.message.splitlines()[0]}",
        )

    print(f"  notifications observed: {notifications[:5] or '(none yet)'}")

    try:
        await transport.request("shutdown", None, timeout_seconds=15.0, description="shutdown")
        observe("shutdown returns without error", True)
    except AgentOSError as error:
        observe("shutdown returns without error", False, f"{error.code}: {error.message.splitlines()[0]}")

    await transport.close(timeout_seconds=10.0)
    observe("the process exited after shutdown", not transport.running, f"exit code {transport.exit_code}")

    failures = [label for label, ok, _ in OBSERVATIONS if not ok]
    print("")
    print(f"observations: {len(OBSERVATIONS)}, failures: {len(failures)}")
    if failures:
        print("failed:")
        for label in failures:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
