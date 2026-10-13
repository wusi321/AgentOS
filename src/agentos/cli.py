"""``agentosctl``: the operator and user command-line interface.

The CLI is a thin IPC client. It never touches the database or executes a system
operation itself: every action goes through the Core service, which is what
enforces policy and writes the audit trail. The few read-only commands that do
work locally (``doctor``, ``config render``, ``runtime capabilities``) exist so
that an operator can diagnose an installation whose service will not start.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from .constants import (
    DSH_LOCKED_COMMIT,
    DSH_LOCKED_VERSION,
    PROJECT_NAME,
    PROJECT_VERSION,
    SYSTEM_CONFIG_FILE,
    SYSTEM_SOCKET,
    TARGET_UBUNTU_MIN,
)
from .core.config import (
    DEFAULT_CONFIG_DOCUMENT,
    load_config,
    read_credential_environment,
    render_config,
    write_config_atomically,
)
from .core.ipc_client import IpcClient
from .errors import AgentOSError, ValidationError
from .policy import PolicyEngine
from .runtime import create_runtime, list_runtimes
from .runtime.factory import RuntimeSettings
from .system_bridge import EXECUTABLE_OPERATIONS
from .system_bridge.bridge import READ_TOOL_LEVELS
from .tasks import TaskState

EXIT_OK: Final = 0
EXIT_FAILURE: Final = 1
EXIT_USAGE: Final = 2
EXIT_DENIED: Final = 3
EXIT_UNAVAILABLE: Final = 4

#: Documentation URL printed by ``--help``.
DOCUMENTATION_URL: Final = "https://github.com/agentos-project/agentos"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for ``agentosctl``."""
    parser = argparse.ArgumentParser(
        prog="agentosctl",
        description=(
            f"{PROJECT_NAME} control interface. "
            "Every state-changing command is sent to the AgentOS Core service, which enforces policy."
        ),
        epilog=f"Documentation: {DOCUMENTATION_URL}",
    )
    parser.add_argument("--version", action="version", version=f"{PROJECT_NAME} {PROJECT_VERSION}")
    parser.add_argument(
        "--socket",
        default=os.environ.get("AGENTOS_SOCKET", SYSTEM_SOCKET),
        help=f"Core IPC socket path (default: {SYSTEM_SOCKET})",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="per-request timeout in seconds (default: 30)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    _add_doctor(subparsers)
    _add_health(subparsers)
    _add_hello(subparsers)
    _add_task(subparsers)
    _add_approval(subparsers)
    _add_policy(subparsers)
    _add_audit(subparsers)
    _add_system(subparsers)
    _add_events(subparsers)
    _add_config(subparsers)
    _add_runtime(subparsers)
    _add_demo(subparsers)
    return parser


def _add_doctor(subparsers: argparse._SubParsersAction) -> None:
    """Register ``doctor``."""
    doctor = subparsers.add_parser(
        "doctor",
        help="diagnose the installation without contacting the service",
    )
    doctor.set_defaults(handler=_cmd_doctor)


def _add_health(subparsers: argparse._SubParsersAction) -> None:
    """Register ``health``."""
    health = subparsers.add_parser("health", help="report service, runtime, and store health")
    health.set_defaults(handler=_cmd_health)


def _add_hello(subparsers: argparse._SubParsersAction) -> None:
    """Register ``hello``.

    The handshake is worth exposing on its own: it reports the service identity,
    the peer credentials the kernel recorded, and the exact IPC method set, which
    is what an operator needs when access is denied or a client is out of date.
    """
    hello = subparsers.add_parser("hello", help="report the service identity and this caller's credentials")
    hello.set_defaults(handler=_cmd_hello)


def _add_task(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``task`` command group."""
    task = subparsers.add_parser("task", help="create, inspect, and cancel tasks")
    nested = task.add_subparsers(dest="task_command", required=True)

    run = nested.add_parser("run", help="submit a prompt as a task")
    run.add_argument("prompt", help="the instruction to run")
    run.add_argument("--runtime", help="runtime to use (mock or dsh)")
    run.add_argument(
        "--level-cap",
        default="P0",
        choices=["P0", "P1", "P2", "P3"],
        help="highest privilege level the task may reach (default: P0)",
    )
    run.add_argument("--priority", type=int, default=0, help="queue priority, higher first")
    run.add_argument("--watch", action="store_true", help="follow the task until it settles")
    run.set_defaults(handler=_cmd_task_run)

    listing = nested.add_parser("list", help="list tasks")
    listing.add_argument("--state", action="append", default=[], help="restrict to a task state; repeatable")
    listing.add_argument("--limit", type=int, default=20, help="maximum rows (default: 20)")
    listing.add_argument("--all-owners", action="store_true", help="superuser only: include every owner")
    listing.set_defaults(handler=_cmd_task_list)

    show = nested.add_parser("show", help="show one task")
    show.add_argument("task_id")
    show.set_defaults(handler=_cmd_task_show)

    history = nested.add_parser("history", help="show a task's event history")
    history.add_argument("task_id")
    history.add_argument("--limit", type=int, default=100)
    history.set_defaults(handler=_cmd_task_history)

    cancel = nested.add_parser("cancel", help="request cancellation of a task")
    cancel.add_argument("task_id")
    cancel.set_defaults(handler=_cmd_task_cancel)

    watch = nested.add_parser("watch", help="stream events for a task until it settles")
    watch.add_argument("task_id")
    watch.add_argument("--timeout", type=float, default=900.0, help="give up after this many seconds")
    watch.set_defaults(handler=_cmd_task_watch)


def _add_approval(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``approval`` command group."""
    approval = subparsers.add_parser("approval", help="review and decide approvals")
    nested = approval.add_subparsers(dest="approval_command", required=True)

    listing = nested.add_parser("list", help="list approvals")
    listing.add_argument("--state", action="append", default=[], help="restrict to an approval state; repeatable")
    listing.add_argument("--limit", type=int, default=20)
    listing.set_defaults(handler=_cmd_approval_list)

    show = nested.add_parser("show", help="show one approval with its bound parameters")
    show.add_argument("approval_id")
    show.set_defaults(handler=_cmd_approval_show)

    request = nested.add_parser("request", help="create an approval request for one controlled operation")
    request.add_argument("task_id")
    request.add_argument("operation", choices=sorted(EXECUTABLE_OPERATIONS))
    request.add_argument("--target", required=True, help="the single object the operation acts on")
    request.add_argument("--reason", default="", help="why the operation is proposed")
    request.add_argument("--ttl", type=int, default=None, help="validity in seconds (clamped by policy)")
    request.set_defaults(handler=_cmd_approval_request)

    grant = nested.add_parser("grant", help="authorize an approval")
    grant.add_argument("approval_id")
    grant.add_argument("--note", default="")
    grant.set_defaults(handler=_cmd_approval_decide, grant=True)

    deny = nested.add_parser("deny", help="refuse an approval")
    deny.add_argument("approval_id")
    deny.add_argument("--note", default="")
    deny.set_defaults(handler=_cmd_approval_decide, grant=False)

    revoke = nested.add_parser("revoke", help="revoke an approval that has not been used")
    revoke.add_argument("approval_id")
    revoke.set_defaults(handler=_cmd_approval_revoke)


def _add_policy(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``policy`` command group."""
    policy = subparsers.add_parser("policy", help="inspect the capability and rule inventory")
    nested = policy.add_subparsers(dest="policy_command", required=True)

    inventory = nested.add_parser("inventory", help="list every declared tool with its privilege level")
    inventory.set_defaults(handler=_cmd_policy_inventory)

    evaluate = nested.add_parser("evaluate", help="evaluate a proposed tool call without running it")
    evaluate.add_argument("tool")
    evaluate.add_argument("--params", default="{}", help="JSON parameter object")
    evaluate.add_argument("--task-id", default=None, help="evaluate in the context of this task's authority")
    evaluate.set_defaults(handler=_cmd_policy_evaluate)


def _add_audit(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``audit`` command group."""
    audit = subparsers.add_parser("audit", help="query the audit trail")
    nested = audit.add_subparsers(dest="audit_command", required=True)

    query = nested.add_parser("query", help="list audit records")
    query.add_argument("--task-id", default=None)
    query.add_argument("--kind", default=None)
    query.add_argument("--operation", default=None)
    query.add_argument("--since", default=None, help="RFC 3339 lower bound")
    query.add_argument("--limit", type=int, default=50)
    query.set_defaults(handler=_cmd_audit_query)

    summary = nested.add_parser("summary", help="show audit counts")
    summary.set_defaults(handler=_cmd_audit_summary)


def _add_system(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``system`` command group."""
    system = subparsers.add_parser("system", help="read-only system inspection")
    nested = system.add_subparsers(dest="system_command", required=True)

    snapshot = nested.add_parser("snapshot", help="CPU, memory, disk, and top processes")
    snapshot.set_defaults(handler=_cmd_system_snapshot)

    processes = nested.add_parser("processes", help="list processes")
    processes.add_argument("--limit", type=int, default=20)
    processes.add_argument("--sort", default="cpu", choices=["cpu", "memory", "pid"])
    processes.add_argument("--min-cpu", type=float, default=0.0)
    processes.set_defaults(handler=_cmd_system_processes)

    disks = nested.add_parser("disks", help="filesystem capacity")
    disks.set_defaults(handler=_cmd_system_disks)

    services = nested.add_parser("services", help="list systemd units")
    services.add_argument("--state", default="failed", choices=["failed", "running", "all"])
    services.add_argument("--limit", type=int, default=50)
    services.set_defaults(handler=_cmd_system_services)

    service = nested.add_parser("service", help="show one systemd unit")
    service.add_argument("unit")
    service.set_defaults(handler=_cmd_system_service)

    logs = nested.add_parser("logs", help="read bounded journal entries")
    logs.add_argument("--unit", default=None)
    logs.add_argument("--lines", type=int, default=50)
    logs.add_argument("--priority", type=int, default=None)
    logs.add_argument("--since", default=None)
    logs.set_defaults(handler=_cmd_system_logs)

    read = nested.add_parser("read", help="run a read-only System Bridge tool by name")
    read.add_argument("tool", choices=sorted(READ_TOOL_LEVELS))
    read.add_argument("--params", default="{}", help="JSON parameter object")
    read.set_defaults(handler=_cmd_system_read)


def _add_events(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``events`` command group."""
    events = subparsers.add_parser("events", help="inspect the event stream")
    nested = events.add_subparsers(dest="events_command", required=True)

    recent = nested.add_parser("recent", help="print recent events")
    recent.add_argument("--limit", type=int, default=20)
    recent.set_defaults(handler=_cmd_events_recent)

    follow = nested.add_parser("follow", help="follow the event stream until interrupted")
    follow.add_argument("--timeout", type=float, default=0.0, help="stop after this many seconds; 0 means never")
    follow.set_defaults(handler=_cmd_events_follow)


def _add_config(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``config`` command group."""
    config = subparsers.add_parser("config", help="inspect the effective configuration")
    nested = config.add_subparsers(dest="config_command", required=True)

    show = nested.add_parser("show", help="print the effective configuration as JSON")
    show.set_defaults(handler=_cmd_config_show)

    render = nested.add_parser("render", help="print the default configuration document")
    render.set_defaults(handler=_cmd_config_render)

    path = nested.add_parser("path", help="print the paths that contribute to the configuration")
    path.set_defaults(handler=_cmd_config_path)


def _add_runtime(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``runtime`` command group."""
    runtime = subparsers.add_parser("runtime", help="inspect agent runtimes")
    nested = runtime.add_subparsers(dest="runtime_command", required=True)

    catalog = nested.add_parser("list", help="list the shipped runtimes")
    catalog.set_defaults(handler=_cmd_runtime_list)

    probe = nested.add_parser("probe", help="probe one runtime's availability and capabilities")
    probe.add_argument("runtime_id", nargs="?", default=None, help="defaults to the configured runtime")
    probe.set_defaults(handler=_cmd_runtime_probe)


def _add_demo(subparsers: argparse._SubParsersAction) -> None:
    """Register ``demo``."""
    demo = subparsers.add_parser(
        "demo",
        help="run the end-to-end keyless flow: task, policy check, approval, audit",
    )
    demo.add_argument(
        "--prompt",
        default="Check whether this system is healthy and explain the most likely cause of any pressure.",
    )
    demo.set_defaults(handler=_cmd_demo)


# -- command implementations ----------------------------------------------


def _client(args: argparse.Namespace) -> IpcClient:
    """Build an IPC client from parsed arguments."""
    return IpcClient(args.socket, timeout_seconds=args.timeout)


def _emit(args: argparse.Namespace, payload: Any, *, human: str = "") -> int:
    """Print a result either as JSON or as pre-rendered human text."""
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    elif human:
        print(human)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return EXIT_OK


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnose the installation from local facts only."""
    report: dict[str, Any] = {
        "project": PROJECT_NAME,
        "version": PROJECT_VERSION,
        "python": sys.version.split()[0],
        "target_ubuntu_minimum": TARGET_UBUNTU_MIN,
        "socket": args.socket,
        "config": {},
        "runtime": {},
        "service": {},
        "findings": [],
    }
    findings: list[dict[str, str]] = report["findings"]
    try:
        config = load_config()
        report["config"] = config.to_dict()
    except AgentOSError as error:
        findings.append({"severity": "error", "message": f"configuration invalid: {error.message}"})
        config = None
    client = _client(args)
    if client.available():
        try:
            hello = client.call("hello")
            report["service"] = {"reachable": True, "hello": hello}
        except AgentOSError as error:
            report["service"] = {"reachable": True, "error": error.message}
    else:
        report["service"] = {"reachable": False}
        findings.append(
            {
                "severity": "warning",
                "message": (
                    f"the Core service is not reachable on {args.socket}; "
                    "run 'systemctl status agentos-core' and check group membership for the agentos group"
                ),
            }
        )
    if config is not None:
        try:
            credentials = read_credential_environment()
        except AgentOSError as error:
            credentials = {}
            findings.append({"severity": "error", "message": f"credential file rejected: {error.message}"})
        runtime_settings = config.runtime
        selected = runtime_settings.runtime_id
        report["runtime"]["selected"] = selected
        report["runtime"]["catalog"] = list_runtimes()
        report["runtime"]["credential_present"] = bool(
            credentials.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        )
        try:
            runtime = create_runtime(runtime_settings)
            health = _run_awaitable(runtime.health_check())
            capabilities = _run_awaitable(runtime.capabilities())
            report["runtime"]["health"] = health.to_dict() if hasattr(health, "to_dict") else health
            report["runtime"]["capabilities"] = capabilities.to_dict() if hasattr(capabilities, "to_dict") else capabilities
            if not report["runtime"]["health"].get("available", False):
                findings.append(
                    {
                        "severity": "warning" if selected == "mock" else "error",
                        "message": f"runtime {selected!r} is not available: {report['runtime']['health'].get('detail', '')}",
                    }
                )
            if selected == "dsh" and not report["runtime"]["credential_present"]:
                findings.append(
                    {
                        "severity": "warning",
                        "message": (
                            "runtime_id is 'dsh' but no model credential is configured; "
                            "put DEEPSEEK_API_KEY in /etc/agentos/credentials.env (mode 0600)"
                        ),
                    }
                )
            _run_awaitable(runtime.close())
        except AgentOSError as error:
            findings.append({"severity": "error", "message": f"runtime probe failed: {error.message}"})
        report["runtime"]["locked_dsh"] = {"version": DSH_LOCKED_VERSION, "commit": DSH_LOCKED_COMMIT}
    try:
        policy = PolicyEngine.from_configuration()
        report["policy"] = {
            "documents": [policy_document.name for policy_document in policy.policies],
            "declared_tools": len(policy.manifests),
            "service_unit_allowlist": sorted(policy.service_unit_allowlist()),
            "tools": policy.inventory(),
        }
        if not policy.policies:
            findings.append(
                {
                    "severity": "warning",
                    "message": (
                        "no policy document was loaded from /etc/agentos/policies; "
                        "only P0 read-only tools are permitted"
                    ),
                }
            )
    except AgentOSError as error:
        findings.append({"severity": "error", "message": f"policy load failed: {error.message}"})
    healthy = not any(finding["severity"] == "error" for finding in findings)
    report["healthy"] = healthy
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"{PROJECT_NAME} {PROJECT_VERSION} doctor")
        print(f"  python            {report['python']}")
        print(f"  socket            {args.socket}")
        print(f"  service reachable {report['service'].get('reachable')}")
        if "selected" in report["runtime"]:
            health = report["runtime"].get("health", {})
            print(f"  runtime           {report['runtime']['selected']} available={health.get('available')}")
            if health.get("detail"):
                print(f"                    {health['detail']}")
        if "documents" in report.get("policy", {}):
            print(f"  policy documents  {report['policy']['documents'] or '(built-in read-only default)'}")
        if findings:
            print("  findings")
            for finding in findings:
                print(f"    [{finding['severity']}] {finding['message']}")
        else:
            print("  findings          none")
    return EXIT_OK if healthy else EXIT_FAILURE


def _run_awaitable(value: Any) -> Any:
    """Resolve an awaitable returned by an adapter that is otherwise synchronous."""
    if hasattr(value, "__await__"):
        return asyncio.run(value)
    return value


def _cmd_health(args: argparse.Namespace) -> int:
    """Print the Core health report."""
    result = _client(args).call("health")
    if args.json:
        return _emit(args, result)
    service = result.get("service", {})
    runtime = result.get("runtime", {})
    database = result.get("database", {})
    tasks = result.get("tasks", {})
    print(f"service   ok={service.get('ok')} version={service.get('version')} uptime={service.get('uptime_seconds')}s")
    print(f"database  ok={database.get('ok')} path={database.get('path')} tasks={database.get('task_count')}")
    print(f"runtime   {runtime.get('runtime_id')} available={runtime.get('available')} {runtime.get('detail', '')}")
    print(f"tasks     {tasks.get('by_state')}")
    print(f"approvals pending={tasks.get('pending_approvals')}")
    return EXIT_OK


def _cmd_hello(args: argparse.Namespace) -> int:
    """Print the service handshake, including the kernel-recorded caller identity."""
    result = _client(args).call("hello")
    if args.json:
        return _emit(args, result)
    peer = result.get("peer", {})
    print(f"service   {result.get('service')} {result.get('version')}")
    print(f"uptime    {result.get('uptime_seconds')}s")
    print(f"caller    uid={peer.get('uid')} gid={peer.get('gid')} pid={peer.get('pid')}")
    print("           these credentials come from the kernel (SO_PEERCRED), not from this client")
    methods = result.get("methods", [])
    print(f"methods   {len(methods)}")
    for name in methods:
        print(f"  {name}")
    return EXIT_OK


def _cmd_task_run(args: argparse.Namespace) -> int:
    """Submit a prompt as a task."""
    client = _client(args)
    created = client.call(
        "task.create",
        {
            "prompt": args.prompt,
            "runtime_id": args.runtime,
            "level_cap": args.level_cap,
            "priority": args.priority,
        },
    )
    task = created.get("task", {})
    if args.watch:
        return _watch_task(args, task.get("task_id", ""))
    if args.json:
        return _emit(args, created)
    print(f"created task {task.get('task_id')} state={task.get('state')} runtime={task.get('runtime_id')}")
    print(f"follow it with: agentosctl task watch {task.get('task_id')}")
    return EXIT_OK


def _cmd_task_list(args: argparse.Namespace) -> int:
    """List tasks."""
    result = _client(args).call(
        "task.list",
        {"states": args.state, "limit": args.limit, "all_owners": args.all_owners},
    )
    if args.json:
        return _emit(args, result)
    tasks = result.get("tasks", [])
    if not tasks:
        print("no tasks")
        return EXIT_OK
    print(f"{'TASK ID':36}  {'STATE':17}  {'RUNTIME':6}  TITLE")
    for task in tasks:
        print(
            f"{task.get('task_id', ''):36}  {task.get('state', ''):17}  "
            f"{task.get('runtime_id', ''):6}  {task.get('title', '')}"
        )
    return EXIT_OK


def _cmd_task_show(args: argparse.Namespace) -> int:
    """Show one task."""
    result = _client(args).call("task.get", {"task_id": args.task_id})
    task = result.get("task", {})
    if args.json:
        return _emit(args, result)
    print(f"task      {task.get('task_id')}")
    print(f"state     {task.get('state')}")
    print(f"runtime   {task.get('runtime_id')}  level cap {task.get('level_cap_label')}")
    print(f"owner     {task.get('owner_name') or task.get('owner_uid')}")
    print(f"created   {task.get('created_at')}")
    print(f"title     {task.get('title')}")
    if task.get("session_id"):
        print(f"session   {task.get('session_id')}")
    if task.get("error_message"):
        print(f"error     {task.get('error_message')}")
    if task.get("result_summary"):
        print("result")
        for line in str(task["result_summary"]).splitlines():
            print(f"  {line}")
    return EXIT_OK


def _cmd_task_history(args: argparse.Namespace) -> int:
    """Show a task's event history."""
    result = _client(args).call("task.history", {"task_id": args.task_id, "limit": args.limit})
    if args.json:
        return _emit(args, result)
    for entry in result.get("events", []):
        payload = json.dumps(entry.get("payload", {}), ensure_ascii=False)
        if len(payload) > 160:
            payload = payload[:157] + "..."
        print(f"{entry.get('sequence'):>5}  {entry.get('event_type'):32}  {payload}")
    return EXIT_OK


def _cmd_task_cancel(args: argparse.Namespace) -> int:
    """Request cancellation of a task."""
    result = _client(args).call("task.cancel", {"task_id": args.task_id})
    task = result.get("task", {})
    if args.json:
        return _emit(args, result)
    print(f"task {task.get('task_id')} is now {task.get('state')}")
    return EXIT_OK


def _watch_task(args: argparse.Namespace, task_id: str) -> int:
    """Follow one task until it settles, printing state changes and output."""
    if not task_id:
        print("cannot watch a task without an id", file=sys.stderr)
        return EXIT_FAILURE
    client = _client(args)
    timeout = float(getattr(args, "timeout", 900.0))
    return _follow(client, timeout=timeout, task_id=task_id, json_output=args.json)


def _cmd_task_watch(args: argparse.Namespace) -> int:
    """Stream events for one task until it settles."""
    return _follow(_client(args), timeout=args.timeout, task_id=args.task_id, json_output=args.json)


#: Task states that end a follow loop.
_TERMINAL: Final[frozenset[str]] = frozenset(state.value for state in TaskState if state.is_terminal)


def _follow(client: IpcClient, *, timeout: float, task_id: str | None, json_output: bool) -> int:
    """Poll the Core event subscription until a task settles or time runs out."""
    import time

    subscription = client.call("events.subscribe")
    token = subscription.get("token", "")
    deadline = None if timeout <= 0 else time.monotonic() + timeout
    final_state = ""
    try:
        while True:
            if deadline is not None and time.monotonic() > deadline:
                print("timed out waiting for the task to settle", file=sys.stderr)
                return EXIT_UNAVAILABLE
            batch = client.call("events.poll", {"token": token, "limit": 100})
            for item in batch.get("events", []):
                event = item.get("event", {}) if isinstance(item, Mapping) else {}
                if task_id is not None and event.get("task_id") not in {task_id, None}:
                    continue
                if json_output:
                    print(json.dumps(event, ensure_ascii=False))
                else:
                    _print_event(event)
                event_type = event.get("event_type", "")
                if event_type == "approval.requested":
                    print(
                        "  -> approval required; run 'agentosctl approval list' to review and decide",
                        file=sys.stderr,
                    )
                if event_type in {"task.completed", "task.failed", "task.cancelled"} and (
                    task_id is None or event.get("task_id") == task_id
                ):
                    final_state = str(event.get("payload", {}).get("state", event_type))
                    return EXIT_OK
            if task_id is not None and not final_state:
                snapshot = client.call("task.get", {"task_id": task_id}).get("task", {})
                state = str(snapshot.get("state", ""))
                if state in _TERMINAL:
                    if not json_output:
                        print(f"task settled: {state}")
                    return EXIT_OK
                if state == "WAITING_APPROVAL":
                    if not json_output:
                        print("task is waiting for an approval decision")
                    return EXIT_OK
            time.sleep(0.4)
    finally:
        try:
            client.call("events.unsubscribe", {"token": token})
        except AgentOSError:
            pass


def _print_event(event: Mapping[str, Any]) -> None:
    """Print one event in a compact human-readable form."""
    event_type = event.get("event_type", "?")
    severity = event.get("severity", "info")
    payload = event.get("payload", {})
    detail = ""
    if event_type == "runtime.message_delta":
        detail = str(payload.get("text", ""))
    elif event_type == "runtime.tool_call_started":
        detail = f"{payload.get('name')}({json.dumps(payload.get('arguments', {}), ensure_ascii=False)})"
    elif event_type == "runtime.tool_call_completed":
        detail = str(payload.get("summary", ""))[:120]
    elif event_type == "policy.decision":
        detail = f"{payload.get('tool')} -> {payload.get('decision')}: {payload.get('reason')}"
    elif event_type == "task.state_changed":
        detail = f"{payload.get('from')} -> {payload.get('to')}"
    elif event_type == "approval.requested":
        detail = f"{payload.get('operation')} on {payload.get('target')} ({payload.get('approval_id')})"
    else:
        detail = json.dumps(payload, ensure_ascii=False)[:160]
    if event_type == "runtime.message_delta":
        sys.stdout.write(detail)
        sys.stdout.flush()
        return
    print(f"[{severity}] {event_type}: {detail}")


def _cmd_approval_list(args: argparse.Namespace) -> int:
    """List approvals."""
    result = _client(args).call("approval.list", {"states": args.state, "limit": args.limit})
    if args.json:
        return _emit(args, result)
    approvals = result.get("approvals", [])
    if not approvals:
        print("no approvals")
        return EXIT_OK
    for approval in approvals:
        print(
            f"{approval.get('approval_id')}  {approval.get('state'):9}  "
            f"{approval.get('operation')}  target={approval.get('target')}  "
            f"expires={approval.get('expires_at')}"
        )
        if approval.get("effect"):
            print(f"    effect: {approval['effect']}")
    return EXIT_OK


def _cmd_approval_show(args: argparse.Namespace) -> int:
    """Show one approval."""
    result = _client(args).call("approval.get", {"approval_id": args.approval_id})
    approval = result.get("approval", {})
    if args.json:
        return _emit(args, result)
    print(f"approval    {approval.get('approval_id')}")
    print(f"state       {approval.get('state')}")
    print(f"operation   {approval.get('operation')}")
    print(f"target      {approval.get('target')}")
    print(f"level       {approval.get('level')}")
    print(f"requested by {approval.get('requested_by_name') or approval.get('requested_by_uid')}")
    print(f"expires     {approval.get('expires_at')}")
    print(f"parameters  {json.dumps(approval.get('parameters', {}), ensure_ascii=False)}")
    print(f"digest      {approval.get('parameter_digest')}")
    print(f"effect      {approval.get('effect')}")
    if approval.get("nonce"):
        print(f"nonce       {approval['nonce']}")
        print("            the nonce is single-use; the executor consumes it with the approval")
    return EXIT_OK


def _cmd_approval_request(args: argparse.Namespace) -> int:
    """Create an approval request for one controlled operation."""
    parameters: dict[str, Any]
    if args.operation == "system.service.restart":
        parameters = {"unit": args.target}
    else:
        try:
            parameters = {"pid": int(args.target)}
        except ValueError:
            print("--target must be a process id for system.process.terminate", file=sys.stderr)
            return EXIT_USAGE
    if args.reason:
        parameters["reason"] = args.reason
    result = _client(args).call(
        "approval.request",
        {
            "task_id": args.task_id,
            "tool": args.operation,
            "parameters": parameters,
            "reason": args.reason,
            "ttl_seconds": args.ttl,
        },
    )
    approval = result.get("approval", {})
    if args.json:
        return _emit(args, result)
    print(f"approval {approval.get('approval_id')} is {approval.get('state')} and expires {approval.get('expires_at')}")
    print(f"effect: {approval.get('effect')}")
    print(f"decide with: agentosctl approval grant {approval.get('approval_id')}")
    return EXIT_OK


def _cmd_approval_decide(args: argparse.Namespace) -> int:
    """Grant or deny an approval."""
    result = _client(args).call(
        "approval.decide",
        {"approval_id": args.approval_id, "grant": bool(args.grant), "note": args.note},
    )
    approval = result.get("approval", {})
    if args.json:
        return _emit(args, result)
    print(f"approval {approval.get('approval_id')} is now {approval.get('state')}")
    if args.grant and approval.get("nonce"):
        print(f"nonce {approval['nonce']} (single use, bound to this target and parameter set)")
    return EXIT_OK


def _cmd_approval_revoke(args: argparse.Namespace) -> int:
    """Revoke an approval."""
    result = _client(args).call("approval.revoke", {"approval_id": args.approval_id})
    approval = result.get("approval", {})
    if args.json:
        return _emit(args, result)
    print(f"approval {approval.get('approval_id')} is now {approval.get('state')}")
    return EXIT_OK


def _cmd_policy_inventory(args: argparse.Namespace) -> int:
    """List the capability inventory."""
    result = _client(args).call("policy.inventory")
    if args.json:
        return _emit(args, result)
    print("TOOL                              LEVEL                 APPROVAL  RULE")
    for tool in result.get("tools", []):
        print(
            f"{tool.get('name', ''):33} {tool.get('level_label', ''):21} "
            f"{str(tool.get('requires_approval')):9} {tool.get('rule_name') or '-'}"
        )
    documents = result.get("policies", [])
    print("")
    print(f"policy documents: {[document.get('name') for document in documents] or ['(built-in read-only default)']}")
    units = result.get("service_unit_allowlist", [])
    print(f"restartable units: {units or '(none configured)'}")
    return EXIT_OK


def _cmd_policy_evaluate(args: argparse.Namespace) -> int:
    """Evaluate a proposed tool call."""
    try:
        parameters = json.loads(args.params)
    except json.JSONDecodeError as error:
        print(f"--params is not valid JSON: {error}", file=sys.stderr)
        return EXIT_USAGE
    result = _client(args).call(
        "policy.evaluate",
        {"tool": args.tool, "parameters": parameters, "task_id": args.task_id},
    )
    if args.json:
        return _emit(args, result)
    print(f"tool      {result.get('tool')}")
    print(f"decision  {result.get('decision')}")
    print(f"level     {result.get('level_label')}")
    print(f"reason    {result.get('reason')}")
    return EXIT_OK if result.get("decision") == "allow" else EXIT_DENIED


def _cmd_audit_query(args: argparse.Namespace) -> int:
    """Query audit records."""
    result = _client(args).call(
        "audit.query",
        {
            "task_id": args.task_id,
            "kind": args.kind,
            "operation": args.operation,
            "since": args.since,
            "limit": args.limit,
        },
    )
    if args.json:
        return _emit(args, result)
    records = result.get("records", [])
    if not records:
        print("no audit records")
        return EXIT_OK
    for record in records:
        print(
            f"{record.get('timestamp')}  {record.get('kind'):18} {record.get('outcome'):8} "
            f"{record.get('operation') or record.get('action')}  "
            f"target={record.get('target')}  uid={record.get('actor_uid')}"
        )
        if record.get("reason"):
            print(f"    {record['reason']}")
    return EXIT_OK


def _cmd_audit_summary(args: argparse.Namespace) -> int:
    """Show audit counts."""
    result = _client(args).call("audit.summary")
    if args.json:
        return _emit(args, result)
    print(f"total {result.get('total')} (retained {result.get('retained_total')})")
    for kind, count in sorted(result.get("by_kind", {}).items()):
        print(f"  {kind:24} {count}")
    return EXIT_OK


def _cmd_system_snapshot(args: argparse.Namespace) -> int:
    """Print a system snapshot."""
    result = _client(args).call("system.snapshot")
    if args.json:
        return _emit(args, result)
    cpu = result.get("cpu", {})
    memory = result.get("memory", {})
    print(f"CPU       {cpu.get('utilised_percent')}% over {cpu.get('core_count')} cores, load {cpu.get('load_average_1m')}")
    print(f"Memory    {memory.get('used_percent')}% used ({memory.get('used_mib')} of {memory.get('total_mib')} MiB)")
    print(f"Swap      {memory.get('swap_used_percent')}% used of {memory.get('swap_total_mib')} MiB")
    print(f"Uptime    {result.get('uptime_seconds')}s, processes visible {result.get('process_count')}")
    print("Filesystems")
    for filesystem in result.get("filesystems", []):
        print(
            f"  {filesystem.get('mount_point'):24} {filesystem.get('used_percent'):6}% "
            f"({filesystem.get('used_gib')} of {filesystem.get('total_gib')} GiB)"
        )
    print("Top processes")
    for process in result.get("top_processes", []):
        print(
            f"  {process.get('pid'):>7}  {process.get('cpu_percent') or 0:>6}%  "
            f"{process.get('rss_mib'):>8} MiB  {process.get('name')}"
        )
    return EXIT_OK


def _cmd_system_processes(args: argparse.Namespace) -> int:
    """List processes."""
    result = _client(args).call(
        "system.read",
        {"tool": "system.processes.list", "parameters": {"limit": args.limit, "sort_by": args.sort, "min_cpu_percent": args.min_cpu}},
    )
    body = result.get("result", {})
    if args.json:
        return _emit(args, body)
    for process in body.get("processes", []):
        print(
            f"{process.get('pid'):>7}  {str(process.get('cpu_percent')):>7}%  "
            f"{process.get('rss_mib'):>9} MiB  {process.get('state'):12} {process.get('name')}"
        )
    return EXIT_OK


def _cmd_system_disks(args: argparse.Namespace) -> int:
    """Report filesystem capacity."""
    result = _client(args).call("system.read", {"tool": "system.disks.read", "parameters": {}})
    body = result.get("result", {})
    if args.json:
        return _emit(args, body)
    for filesystem in body.get("filesystems", []):
        print(
            f"{filesystem.get('mount_point'):24} {filesystem.get('used_percent'):6}%  "
            f"{filesystem.get('used_gib')}/{filesystem.get('total_gib')} GiB  {filesystem.get('filesystem_type')}"
        )
    return EXIT_OK


def _cmd_system_services(args: argparse.Namespace) -> int:
    """List systemd units."""
    result = _client(args).call("system.services", {"state": args.state, "limit": args.limit})
    if args.json:
        return _emit(args, result)
    if not result.get("available", False):
        print(f"systemd is unavailable: {result.get('error')}")
        return EXIT_UNAVAILABLE
    for unit in result.get("units", []):
        print(f"{unit.get('unit'):40} {unit.get('load_state'):10} {unit.get('active_state'):10} {unit.get('sub_state')}")
    return EXIT_OK


def _cmd_system_service(args: argparse.Namespace) -> int:
    """Show one systemd unit."""
    result = _client(args).call("system.read", {"tool": "system.service.status", "parameters": {"unit": args.unit}})
    body = result.get("result", {})
    if args.json:
        return _emit(args, body)
    for key in ("unit", "load_state", "active_state", "sub_state", "unit_file_state", "main_pid", "description"):
        print(f"{key:16} {body.get(key)}")
    return EXIT_OK


def _cmd_system_logs(args: argparse.Namespace) -> int:
    """Read bounded journal entries."""
    parameters: dict[str, Any] = {"lines": args.lines}
    if args.unit:
        parameters["unit"] = args.unit
    if args.priority is not None:
        parameters["priority"] = args.priority
    if args.since:
        parameters["since"] = args.since
    result = _client(args).call("system.read", {"tool": "system.logs.read", "parameters": parameters})
    body = result.get("result", {})
    if args.json:
        return _emit(args, body)
    for entry in body.get("entries", []):
        print(f"{entry.get('timestamp')}  {entry.get('identifier'):20} {entry.get('message')}")
    return EXIT_OK


def _cmd_system_read(args: argparse.Namespace) -> int:
    """Run a read-only System Bridge tool by name."""
    try:
        parameters = json.loads(args.params)
    except json.JSONDecodeError as error:
        print(f"--params is not valid JSON: {error}", file=sys.stderr)
        return EXIT_USAGE
    result = _client(args).call("system.read", {"tool": args.tool, "parameters": parameters})
    return _emit(args, result.get("result", {}))


def _cmd_events_recent(args: argparse.Namespace) -> int:
    """Print recent events."""
    result = _client(args).call("events.recent", {"limit": args.limit})
    if args.json:
        return _emit(args, result)
    for event in result.get("events", []):
        _print_event(event)
    return EXIT_OK


def _cmd_events_follow(args: argparse.Namespace) -> int:
    """Follow the event stream."""
    return _follow(_client(args), timeout=args.timeout, task_id=None, json_output=args.json)


def _cmd_config_show(args: argparse.Namespace) -> int:
    """Print the effective configuration."""
    config = load_config()
    print(json.dumps(config.to_dict(), ensure_ascii=False, indent=2))
    return EXIT_OK


def _cmd_config_render(args: argparse.Namespace) -> int:
    """Print the default configuration document."""
    if args.json:
        print(json.dumps({"document": DEFAULT_CONFIG_DOCUMENT, "path": SYSTEM_CONFIG_FILE}, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(DEFAULT_CONFIG_DOCUMENT)
    return EXIT_OK


def _cmd_config_path(args: argparse.Namespace) -> int:
    """Print configuration paths."""
    sources = load_config().sources
    if args.json:
        return _emit(args, {"sources": list(sources), "primary": SYSTEM_CONFIG_FILE})
    for source in sources:
        print(source)
    return EXIT_OK


def _cmd_runtime_list(args: argparse.Namespace) -> int:
    """List the shipped runtimes."""
    catalog = list_runtimes()
    if args.json:
        return _emit(args, {"runtimes": catalog})
    for runtime in catalog:
        print(f"{runtime.get('runtime_id'):8} {runtime.get('purpose')}")
    return EXIT_OK


def _cmd_runtime_probe(args: argparse.Namespace) -> int:
    """Probe one runtime's availability and capabilities."""
    config = load_config()
    settings = config.runtime
    if args.runtime_id is not None:
        settings = RuntimeSettings(
            **{**_settings_to_mapping(settings), "runtime_id": args.runtime_id}
        )
    runtime = create_runtime(settings)
    try:
        health = _run_awaitable(runtime.health_check())
        capabilities = _run_awaitable(runtime.capabilities())
    finally:
        _run_awaitable(runtime.close())
    report = {
        "runtime_id": settings.runtime_id,
        "health": health.to_dict() if hasattr(health, "to_dict") else health,
        "capabilities": capabilities.to_dict() if hasattr(capabilities, "to_dict") else capabilities,
    }
    if args.json:
        return _emit(args, report)
    print(f"runtime      {report['runtime_id']}")
    print(f"available    {report['health'].get('available')}")
    print(f"detail       {report['health'].get('detail')}")
    for key, value in sorted(report["capabilities"].items()):
        if key in {"notes"}:
            continue
        print(f"{key:20} {value}")
    for note in report["capabilities"].get("notes", []):
        print(f"note         {note}")
    return EXIT_OK


def _settings_to_mapping(settings: RuntimeSettings) -> dict[str, Any]:
    """Convert runtime settings to the mapping form ``from_mapping`` accepts."""
    return {
        "runtime_id": settings.runtime_id,
        "provider": settings.provider,
        "model": settings.model,
        "reasoning_effort": settings.reasoning_effort,
        "max_tokens": settings.max_tokens,
        "node_binary": settings.node_binary,
        "cli_entry": settings.cli_entry,
        "dsh_home": settings.dsh_home,
        "workspace_root": settings.workspace_root,
        "turn_timeout_seconds": settings.turn_timeout_seconds,
        "initialize_timeout_seconds": settings.initialize_timeout_seconds,
        "mock_scenario": dict(settings.mock_scenario),
    }


def _cmd_demo(args: argparse.Namespace) -> int:
    """Run the keyless end-to-end flow against the live service.

    The demo exercises the whole chain the design requires: task creation, a
    runtime turn, a policy evaluation, an approval request, a denial of the
    operation until it is approved, an audit trail, and a clean task state.
    """
    client = _client(args)
    if not client.available():
        print(
            f"the AgentOS Core service is not reachable on {args.socket}.\n"
            "Start it with: sudo systemctl start agentos-core",
            file=sys.stderr,
        )
        return EXIT_UNAVAILABLE
    steps: list[dict[str, Any]] = []

    def record(name: str, detail: Any) -> None:
        steps.append({"step": name, "detail": detail})
        if not args.json:
            print(f"== {name}")

    health = client.call("health")
    record("health", {"runtime": health.get("runtime", {}).get("runtime_id"), "available": health.get("runtime", {}).get("available")})

    created = client.call("task.create", {"prompt": args.prompt, "level_cap": "P0"})
    task_id = created.get("task", {}).get("task_id", "")
    record("task created", {"task_id": task_id, "policy": "the runtime may call P0 read-only tools"})
    _follow(client, timeout=120.0, task_id=task_id, json_output=args.json)
    settled = client.call("task.get", {"task_id": task_id}).get("task", {})
    record("task settled", {"state": settled.get("state"), "result": settled.get("result_summary", "")[:200]})

    evaluation = client.call(
        "policy.evaluate",
        {"tool": "system.processes.list", "parameters": {"limit": 5}, "task_id": task_id},
    )
    record("policy allows a read", {"tool": "system.processes.list", "decision": evaluation.get("decision")})

    denied = client.call(
        "policy.evaluate",
        {"tool": "system.packages.install", "parameters": {}, "task_id": task_id},
    )
    record("policy denies an administrative tool", {"tool": "system.packages.install", "decision": denied.get("decision"), "reason": denied.get("reason")})

    try:
        client.call(
            "approval.request",
            {
                "task_id": task_id,
                "tool": "system.service.restart",
                "parameters": {},
                "reason": "demo: prove that an unallowlisted target is refused",
            },
        )
        record("approval request rejected", {"expected": "a refusal", "observed": "accepted"})
    except AgentOSError as error:
        record("approval request rejected", {"code": error.code, "reason": error.message[:200]})

    pending = client.call(
        "approval.request",
        {
            "task_id": task_id,
            "tool": "system.process.terminate",
            "parameters": {"pid": os.getpid(), "reason": "demo: the service never signals itself"},
            "reason": "demo: an approval is bound to one pid",
        },
    )
    approval_id = pending.get("approval", {}).get("approval_id", "")
    record(
        "approval requested",
        {"approval_id": approval_id, "effect": pending.get("approval", {}).get("effect")},
    )
    decided = client.call("approval.decide", {"approval_id": approval_id, "grant": False, "note": "demo denial"})
    record("approval denied", {"state": decided.get("approval", {}).get("state")})

    audit = client.call("audit.summary")
    record("audit trail", audit)

    if args.json:
        print(json.dumps({"steps": steps}, ensure_ascii=False, indent=2))
    else:
        print("")
        print("demo complete: task -> runtime -> policy -> approval -> audit")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``agentosctl``.

    :param argv: Argument vector without the program name; defaults to ``sys.argv[1:]``.
    :returns: A process exit status.
    """
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        return int(handler(args))
    except KeyboardInterrupt:
        return 130
    except ValidationError as error:
        print(f"error: {error.message}", file=sys.stderr)
        return EXIT_USAGE
    except AgentOSError as error:
        print(f"error [{error.code}]: {error.message}", file=sys.stderr)
        if error.code in {"agentos.policy_denied", "agentos.ipc_access_denied"}:
            return EXIT_DENIED
        if error.code in {"agentos.core_unavailable", "agentos.runtime_unavailable"}:
            return EXIT_UNAVAILABLE
        return EXIT_FAILURE


__all__ = ["build_parser", "main"]
