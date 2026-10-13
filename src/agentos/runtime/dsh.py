"""DshAdapter: drive a locked DeepSeek Harness runtime through the SDK profile.

The adapter speaks the JSON-RPC stdio protocol served by ``dsh --profile sdk``.
It is the only module in AgentOS that knows DSH's event vocabulary, and it maps
every DSH session event onto AgentOS's normalized runtime events. Where the SDK
protocol offers no equivalent — cancellation and runtime-side approvals — the
adapter reports the capability as unsupported instead of inventing an event.

Verified against DSH ``0.2.1-alpha.2`` (commit ``d743267``).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, AsyncIterator, Final

from ..constants import (
    DSH_CLI_ENTRY,
    DSH_HOME_DIR,
    DSH_LOCKED_COMMIT,
    DSH_LOCKED_VERSION,
    DSH_NODE_BINARY,
    DSH_NODE_ENGINES,
    SYSTEM_STATE_DIR,
)
from ..errors import (
    CapabilityUnsupported,
    RuntimeProtocolError,
    RuntimeUnavailable,
    ValidationError,
)
from ..jsonutil import reasoning_from_blocks, text_from_blocks, truncate_text
from .api import RuntimeCapabilities, RuntimeEvent, RuntimeHealth, RuntimeRequest
from .dsh_transport import AsyncJsonRpcProcess, JsonRpcNotification, minimal_child_environment

#: SDK profile served by the locked DSH revision.
DSH_SDK_PROFILE: Final = "sdk"
#: Wire-stable server identity returned by the SDK handshake.
DSH_SERVER_NAME: Final = "deepseek-harness-sdk-runtime"

#: Node engines requirement expressed as accepted major ranges.
_SUPPORTED_NODE_MAJORS: Final = frozenset({22, 23, 24, 25, 26})
_MIN_NODE_22_MINOR: Final = 19

_VERSION_PATTERN: Final = re.compile(r"(\d+)\.(\d+)\.(\d+)")

#: Time budget for one prompt turn when the caller does not set one.
DEFAULT_TURN_TIMEOUT_SECONDS: Final = 900.0
#: Time budget for the initialize handshake.
DEFAULT_INITIALIZE_TIMEOUT_SECONDS: Final = 60.0


@dataclass(frozen=True, slots=True)
class NodeVersion:
    """A parsed Node.js version triple."""

    major: int
    minor: int
    patch: int
    raw: str

    def __str__(self) -> str:
        return self.raw


def parse_node_version(text: str) -> NodeVersion:
    """Parse ``v22.19.1`` style output.

    :param text: Raw ``node --version`` output.
    :raises ValidationError: When no version triple can be found.
    """
    match = _VERSION_PATTERN.search(text or "")
    if match is None:
        raise ValidationError(f"cannot parse a Node.js version from {text!r}")
    return NodeVersion(int(match.group(1)), int(match.group(2)), int(match.group(3)), match.group(0))


def node_version_satisfies(version: NodeVersion) -> bool:
    """Return whether ``version`` satisfies the locked DSH engines range."""
    if version.major == 22:
        return version.minor >= _MIN_NODE_22_MINOR
    return version.major in _SUPPORTED_NODE_MAJORS


@dataclass(slots=True)
class DshSettings:
    """Deployment facts the adapter needs to launch DSH.

    :param node_binary: Node.js executable; defaults to the packaged runtime.
    :param cli_entry: ``dsh`` CLI JavaScript entry point.
    :param dsh_home: Harness home directory; DSH never falls back to ``~/.dsh`` implicitly.
    :param profile: DSH profile to serve.
    :param provider: Model provider route.
    :param model: Model name.
    :param reasoning_effort: Optional adapter-owned reasoning effort id.
    :param max_tokens: Optional positive output-token cap.
    :param turn_timeout_seconds: Deadline for one prompt turn.
    :param initialize_timeout_seconds: Deadline for the SDK handshake.
    :param extra_environment: Additional non-secret environment for the child.
    :param workspace_root: Directory inside which per-task workspaces are created.
    :param node_search_path: Additional locations tried for a Node.js executable.
    """

    node_binary: str = DSH_NODE_BINARY
    cli_entry: str = DSH_CLI_ENTRY
    dsh_home: str = DSH_HOME_DIR
    profile: str = DSH_SDK_PROFILE
    provider: str = ""
    model: str = ""
    reasoning_effort: str | None = None
    max_tokens: int | None = None
    turn_timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS
    initialize_timeout_seconds: float = DEFAULT_INITIALIZE_TIMEOUT_SECONDS
    extra_environment: Mapping[str, str] = field(default_factory=dict)
    workspace_root: str = SYSTEM_STATE_DIR + "/workspaces"
    node_search_path: tuple[str, ...] = ("/usr/bin/node", "/usr/local/bin/node", "/usr/bin/nodejs")

    def validate(self) -> None:
        """Reject settings that cannot produce a runnable child.

        :raises ValidationError: On a blank or syntactically unsafe value.
        """
        for name, value in (("provider", self.provider), ("model", self.model)):
            if value and not re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", value):
                raise ValidationError(f"dsh {name} contains unsupported characters", details={"field": name})
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise ValidationError("dsh max_tokens must be a positive integer")
        if self.profile != DSH_SDK_PROFILE:
            raise ValidationError(
                f"AgentOS drives DSH through the {DSH_SDK_PROFILE!r} profile only",
                details={"profile": self.profile},
            )


def resolve_node_binary(settings: DshSettings) -> str | None:
    """Return the first usable Node.js executable, or ``None`` when none exists."""
    candidates = [settings.node_binary, *settings.node_search_path]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("node")
    return found


class DshAdapter:
    """An :class:`~agentos.runtime.api.AgentRuntime` backed by a DSH subprocess."""

    def __init__(self, settings: DshSettings | None = None) -> None:
        self._settings = settings or DshSettings()
        self._process: AsyncJsonRpcProcess | None = None
        self._sessions: dict[str, str] = {}
        self._session_workspaces: dict[str, str] = {}
        self._sequences: dict[str, int] = {}
        self._initialized_route: tuple[str, str, str | None, int | None] | None = None
        self._pending: dict[str, asyncio.Queue[RuntimeEvent | BaseException | None]] = {}
        self._completed_turns: dict[str, bool] = {}
        self._start_lock = asyncio.Lock()
        self._version: str | None = None
        self._last_health: RuntimeHealth | None = None

    # -- identity ----------------------------------------------------------

    @property
    def runtime_id(self) -> str:
        """Return the stable adapter identity."""
        return "dsh"

    @property
    def settings(self) -> DshSettings:
        """Return the effective launch settings (credentials are never part of them)."""
        return self._settings

    # -- capabilities and health ------------------------------------------

    async def capabilities(self) -> RuntimeCapabilities:
        """Report the verified DSH capability set.

        Cancellation and runtime-side approval events are reported as
        unsupported: the SDK profile exposes neither a cancel method nor an
        approval notification, and AgentOS does not fabricate them.
        """
        return RuntimeCapabilities(
            runtime_id="dsh",
            version=DSH_LOCKED_VERSION,
            streaming=True,
            tool_events=True,
            cancel=False,
            resume=True,
            session_persistence=True,
            approval_events=False,
            multi_session=True,
            notes=(
                "cancellation is unsupported by the SDK profile; AgentOS interrupts "
                "a turn by stopping the runtime process, after which the session is "
                "reopened from persisted history",
                "runtime-side approval prompts are unavailable: AgentOS policy is the "
                "only authority for side-effecting operations",
                "incremental text arrives as compacted delta runs, not per-character chunks",
            ),
        )

    async def health_check(self) -> RuntimeHealth:
        """Probe the packaged Node.js runtime, the CLI entry, and the home directory."""
        health = self._probe_environment()
        self._last_health = health
        return health

    def _probe_environment(self) -> RuntimeHealth:
        """Collect local facts; never starts the runtime or contacts the network."""
        diagnostics: dict[str, Any] = {
            "locked_version": DSH_LOCKED_VERSION,
            "locked_commit": DSH_LOCKED_COMMIT,
            "required_node_engines": DSH_NODE_ENGINES,
            "cli_entry": self._settings.cli_entry,
            "dsh_home": self._settings.dsh_home,
            "credential_configured": bool(os.environ.get("DEEPSEEK_API_KEY")),
        }
        node = resolve_node_binary(self._settings)
        if node is None:
            return RuntimeHealth(
                available=False,
                runtime_id="dsh",
                detail=(
                    "no Node.js runtime found; install agentos-runtime-dsh, which ships the "
                    f"Node.js {DSH_NODE_ENGINES} runtime DSH requires"
                ),
                version=DSH_LOCKED_VERSION,
                node_version=None,
                diagnostics=diagnostics,
            )
        diagnostics["node_binary"] = node
        try:
            node_version = self._read_node_version(node)
        except (ValidationError, OSError) as error:
            return RuntimeHealth(
                available=False,
                runtime_id="dsh",
                detail=f"cannot determine the Node.js version of {node}: {error}",
                version=DSH_LOCKED_VERSION,
                node_version=None,
                diagnostics=diagnostics,
            )
        diagnostics["node_version"] = str(node_version)
        if not node_version_satisfies(node_version):
            return RuntimeHealth(
                available=False,
                runtime_id="dsh",
                detail=(
                    f"Node.js {node_version} does not satisfy the DSH requirement {DSH_NODE_ENGINES}; "
                    "install the agentos-runtime-dsh package rather than a distribution Node.js"
                ),
                version=DSH_LOCKED_VERSION,
                node_version=str(node_version),
                diagnostics=diagnostics,
            )
        if not os.path.isfile(self._settings.cli_entry):
            return RuntimeHealth(
                available=False,
                runtime_id="dsh",
                detail=f"the dsh CLI entry point is missing: {self._settings.cli_entry}",
                version=DSH_LOCKED_VERSION,
                node_version=str(node_version),
                diagnostics=diagnostics,
            )
        if not os.path.isdir(self._settings.dsh_home):
            diagnostics["dsh_home_missing"] = True
        if self._process is not None and self._process.running:
            return RuntimeHealth(
                available=True,
                runtime_id="dsh",
                detail="runtime process is running",
                version=DSH_LOCKED_VERSION,
                node_version=str(node_version),
                diagnostics=diagnostics,
            )
        return RuntimeHealth(
            available=True,
            runtime_id="dsh",
            detail="Node.js requirement satisfied; the runtime starts on first use",
            version=DSH_LOCKED_VERSION,
            node_version=str(node_version),
            diagnostics=diagnostics,
        )

    def _read_node_version(self, node_binary: str) -> NodeVersion:
        """Run ``node --version`` synchronously; the call is short and cached by the caller."""
        import subprocess  # local import keeps module import cost off the hot path

        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [node_binary, "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if completed.returncode != 0:
            raise ValidationError(
                f"node --version exited {completed.returncode}: {truncate_text(completed.stderr.strip(), limit=200)}"
            )
        return parse_node_version(completed.stdout or completed.stderr)

    # -- process lifecycle -------------------------------------------------

    def _build_argv(self) -> tuple[str, ...]:
        """Compose the child argv for the SDK profile."""
        node = resolve_node_binary(self._settings)
        if node is None:
            raise RuntimeUnavailable("no Node.js runtime is available for DSH")
        return (node, self._settings.cli_entry, "--profile", self._settings.profile)

    def _build_environment(self) -> dict[str, str]:
        """Compose the allowlisted child environment."""
        additions: dict[str, str] = {
            "DSH_HOME": self._settings.dsh_home,
            "NODE_ENV": "production",
        }
        additions.update({str(key): str(value) for key, value in self._settings.extra_environment.items()})
        environment = minimal_child_environment(additions=additions)
        # DSH home and workspace must be writable by the runtime worker identity.
        environment.setdefault("HOME", self._settings.dsh_home)
        return environment

    async def start(self) -> None:
        """Start the DSH subprocess if it is not already running.

        :raises RuntimeUnavailable: When the environment cannot support DSH.
        """
        async with self._start_lock:
            if self._process is not None and self._process.running:
                return
            self._settings.validate()
            health = self._probe_environment()
            self._last_health = health
            if not health.available:
                raise RuntimeUnavailable(health.detail, details=dict(health.diagnostics))
            os.makedirs(self._settings.dsh_home, exist_ok=True)
            process = AsyncJsonRpcProcess(
                self._build_argv(),
                cwd=self._settings.dsh_home,
                env=self._build_environment(),
                request_timeout_seconds=self._settings.turn_timeout_seconds,
                description="dsh sdk runtime",
            )
            process.add_notification_handler(self._on_notification)
            await process.start()
            self._process = process
            self._initialized_route = None

    async def close(self) -> None:
        """Stop the runtime process and release queues."""
        process = self._process
        self._process = None
        self._initialized_route = None
        for session_id in list(self._pending):
            self._fail_session(session_id, RuntimeUnavailable("runtime is shutting down"))
        if process is not None:
            with contextlib.suppress(Exception):
                await process.request("shutdown", None, timeout_seconds=5.0)
            await process.close(timeout_seconds=5.0)

    async def _ensure_started(self) -> AsyncJsonRpcProcess:
        """Return a running transport, starting one when needed."""
        if self._process is None or not self._process.running:
            await self.start()
        assert self._process is not None  # start() either sets it or raises
        return self._process

    # -- session and prompt ------------------------------------------------

    async def create_session(self, *, workspace: str, session_id: str | None = None) -> str:
        """Register a session id and remember its workspace.

        DSH creates the agent lazily on the first prompt, so this records intent
        only; the handshake is completed by :meth:`_ensure_initialize`.
        """
        if not workspace:
            raise ValidationError("workspace must be a non-empty absolute path")
        resolved = session_id or str(uuid.uuid4())
        self._sessions[resolved] = workspace
        self._session_workspaces[resolved] = workspace
        self._sequences.setdefault(resolved, 0)
        self._completed_turns.setdefault(resolved, True)
        return resolved

    async def _ensure_initialize(self, workspace: str) -> None:
        """Complete the SDK handshake for the configured route and workspace."""
        settings = self._settings
        if not settings.provider or not settings.model:
            raise RuntimeUnavailable(
                "no model route is configured: set runtime.provider and runtime.model in "
                "/etc/agentos/agentos.toml, and provide the credential through the service "
                "environment rather than the command line"
            )
        route = (settings.provider, settings.model, settings.reasoning_effort, settings.max_tokens)
        process = await self._ensure_started()
        if self._initialized_route is not None and self._initialized_route != route:
            raise RuntimeUnavailable(
                "the model route changed while the runtime was running; restart agentos-core to apply it",
                details={"active_route": list(self._initialized_route), "requested_route": list(route)},
            )
        if self._initialized_route == route:
            return
        params: dict[str, Any] = {
            "cwd": workspace,
            "provider": settings.provider,
            "model": settings.model,
        }
        if settings.reasoning_effort:
            params["reasoningEffort"] = settings.reasoning_effort
        if settings.max_tokens:
            params["maxTokens"] = settings.max_tokens
        try:
            result = await process.request(
                "initialize",
                params,
                timeout_seconds=settings.initialize_timeout_seconds,
                description="dsh initialize handshake",
            )
        except RuntimeProtocolError as error:
            raise RuntimeUnavailable(
                f"DSH refused the {settings.provider}/{settings.model} route: {error.message}",
                details=dict(error.details),
            ) from error
        server_info = result.get("serverInfo") if isinstance(result, Mapping) else None
        if isinstance(server_info, Mapping):
            name = server_info.get("name")
            if isinstance(name, str) and name != DSH_SERVER_NAME:
                raise RuntimeProtocolError(
                    f"unexpected SDK server identity {name!r}; expected {DSH_SERVER_NAME!r}",
                    details={"server_info": dict(server_info)},
                )
            version = server_info.get("version")
            if isinstance(version, str):
                self._version = version
        self._initialized_route = route

    def send_message(self, request: RuntimeRequest) -> AsyncIterator[RuntimeEvent]:
        """Return the event stream for one prompt."""
        return self._stream_turn(request)

    async def _stream_turn(self, request: RuntimeRequest) -> AsyncIterator[RuntimeEvent]:
        """Prompt the runtime and yield normalized events until the turn settles."""
        if not request.message.strip():
            raise ValidationError("prompt message must not be empty")
        session_id = request.session_id
        workspace = request.workspace or self._session_workspaces.get(session_id, "")
        if session_id not in self._sessions:
            await self.create_session(workspace=workspace, session_id=session_id)
        await self._ensure_initialize(workspace)
        process = await self._ensure_started()

        queue: asyncio.Queue[RuntimeEvent | BaseException | None] = asyncio.Queue()
        self._pending[session_id] = queue
        self._completed_turns[session_id] = False
        self._sequences.setdefault(session_id, 0)
        yield RuntimeEvent(
            type="session.created",
            session_id=session_id,
            runtime_id="dsh",
            payload={"workspace": workspace, "runtime_version": self._version or DSH_LOCKED_VERSION},
            sequence=0,
            native_event_type="initialize",
        )
        try:
            await process.request(
                "session/prompt",
                {"sessionId": session_id, "contentBlocks": [{"type": "text", "text": request.message}]},
                description="dsh session/prompt",
            )
        except BaseException:
            self._pending.pop(session_id, None)
            raise
        try:
            deadline = (
                None
                if self._settings.turn_timeout_seconds <= 0
                else asyncio.get_running_loop().time() + self._settings.turn_timeout_seconds
            )
            while True:
                remaining = None if deadline is None else max(0.0, deadline - asyncio.get_running_loop().time())
                if remaining is not None and remaining <= 0:
                    raise RuntimeUnavailable(
                        "the turn exceeded its time budget",
                        details={"session_id": session_id, "timeout_seconds": self._settings.turn_timeout_seconds},
                    )
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError as error:
                    raise RuntimeUnavailable(
                        "the turn exceeded its time budget",
                        details={"session_id": session_id, "timeout_seconds": self._settings.turn_timeout_seconds},
                    ) from error
                if item is None:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
                if item.type in {"message.completed", "runtime.error"} and item.sequence > 0:
                    if item.type == "message.completed":
                        return
        finally:
            self._pending.pop(session_id, None)

    async def cancel(self, session_id: str) -> None:
        """Report that the SDK profile offers no cancellation.

        :raises CapabilityUnsupported: Always, until the runtime exposes a cancel method.
        """
        raise CapabilityUnsupported(
            "the DSH SDK profile has no cancel method; AgentOS stops the runtime process "
            "instead, and the task is marked cancelled at the task layer",
            details={"session_id": session_id, "runtime_id": "dsh"},
        )

    async def interrupt_process(self) -> None:
        """Stop the runtime process, abandoning any in-flight turn.

        This is the mechanism the task layer uses to honour a cancellation. The
        session remains durable inside DSH and is reopened by id on the next
        prompt.
        """
        process = self._process
        self._process = None
        self._initialized_route = None
        for session_id in list(self._pending):
            self._fail_session(session_id, RuntimeUnavailable("the runtime process was stopped"))
        if process is not None:
            await process.close(timeout_seconds=5.0)

    # -- notification handling --------------------------------------------

    def _fail_session(self, session_id: str, error: BaseException) -> None:
        """Push ``error`` into the session's queue, if a turn is waiting."""
        queue = self._pending.get(session_id)
        if queue is not None:
            queue.put_nowait(error)

    def _on_notification(self, notification: JsonRpcNotification) -> None:
        """Translate one DSH notification into zero or more runtime events."""
        if notification.method == "session.event":
            for event in self._translate_session_event(notification.payload):
                self._enqueue(event)
            return
        if notification.method == "session.status":
            status = notification.payload.get("status")
            session_id = notification.payload.get("sessionId")
            if not isinstance(session_id, str):
                return
            if status == "running":
                return
            if status == "idle" and not self._completed_turns.get(session_id, True):
                self._enqueue(
                    self._next_event(session_id, "message.completed", {"text": "", "synthetic": True}, "session.status")
                )
        elif notification.method == "subagent.started":
            parent = notification.payload.get("parentSessionId")
            if isinstance(parent, str):
                self._enqueue(
                    self._next_event(
                        parent,
                        "runtime.warning",
                        {
                            "reason": "runtime spawned a subagent",
                            "child_session_id": notification.payload.get("childSessionId"),
                        },
                        "subagent.started",
                    )
                )
        elif notification.method == "subagent.finished":
            parent = notification.payload.get("parentSessionId")
            if isinstance(parent, str):
                self._enqueue(
                    self._next_event(
                        parent,
                        "runtime.warning",
                        {
                            "reason": "runtime subagent finished",
                            "child_session_id": notification.payload.get("childSessionId"),
                            "status": notification.payload.get("status"),
                        },
                        "subagent.finished",
                    )
                )

    def _enqueue(self, event: RuntimeEvent) -> None:
        """Deliver an event to the waiting turn and track completion state."""
        session_id = event.session_id
        if event.type == "message.completed":
            self._completed_turns[session_id] = True
        queue = self._pending.get(session_id)
        if queue is None:
            # Events for a session with no waiting consumer are dropped rather
            # than buffered: DSH's durable log remains the record of truth.
            return
        queue.put_nowait(event)

    def _next_event(
        self,
        session_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        native: str | None,
    ) -> RuntimeEvent:
        """Build the next sequenced event for ``session_id``."""
        sequence = self._sequences.get(session_id, 0) + 1
        self._sequences[session_id] = sequence
        return RuntimeEvent(
            type=event_type,
            session_id=session_id,
            runtime_id="dsh",
            payload=dict(payload),
            sequence=sequence,
            native_event_type=native,
        )

    def _translate_session_event(self, payload: Mapping[str, Any]) -> list[RuntimeEvent]:
        """Map one DSH ``session.event`` payload onto normalized events."""
        session_id = payload.get("sessionId")
        envelope = payload.get("event")
        if not isinstance(session_id, str) or not isinstance(envelope, Mapping):
            return []
        native_type = envelope.get("type")
        data = envelope.get("data")
        if not isinstance(native_type, str) or not isinstance(data, Mapping):
            return []
        if session_id not in self._sessions:
            # Events for sessions AgentOS did not create (a resumed runtime, or a
            # subagent child) are still mapped, keyed by their own session id.
            self._sessions[session_id] = str(data.get("cwd") or "")
            self._session_workspaces.setdefault(session_id, str(data.get("cwd") or ""))
            self._sequences.setdefault(session_id, 0)
            self._completed_turns.setdefault(session_id, True)

        if native_type == "assistant/message":
            message = data.get("message")
            content = message.get("content") if isinstance(message, Mapping) else None
            blocks = content if isinstance(content, list) else []
            return [
                self._next_event(
                    session_id,
                    "message.completed",
                    {
                        "text": text_from_blocks(blocks),
                        "reasoning": reasoning_from_blocks(blocks),
                        "interrupted": bool(data.get("interrupted")),
                        "turn": data.get("turn"),
                        "step": data.get("step"),
                        "usage": data.get("usage") if isinstance(data.get("usage"), Mapping) else None,
                    },
                    native_type,
                )
            ]
        if native_type in {"assistant/attempt", "developer/message"}:
            stream = data.get("stream")
            return self._delta_events(session_id, native_type, stream if isinstance(stream, list) else [])
        if native_type == "tool/call":
            return [
                self._next_event(
                    session_id,
                    "tool.call.started",
                    {
                        "call_id": data.get("callId"),
                        "name": data.get("name"),
                        "arguments": _parse_raw_arguments(data.get("arguments")),
                        "turn": data.get("turn"),
                        "step": data.get("step"),
                    },
                    native_type,
                )
            ]
        if native_type == "tool/result":
            message = data.get("message")
            content = message.get("content") if isinstance(message, Mapping) else None
            blocks = content if isinstance(content, list) else []
            error = data.get("error")
            return [
                self._next_event(
                    session_id,
                    "tool.call.completed",
                    {
                        "call_id": message.get("toolCallId") if isinstance(message, Mapping) else None,
                        "is_error": bool(message.get("isError")) if isinstance(message, Mapping) else False,
                        "summary": truncate_text(text_from_blocks(blocks), limit=2048),
                        "error_name": error.get("name") if isinstance(error, Mapping) else None,
                        "error_code": error.get("code") if isinstance(error, Mapping) else None,
                        "turn": data.get("turn"),
                        "step": data.get("step"),
                    },
                    native_type,
                )
            ]
        if native_type in {"turn/end", "step/end"}:
            return []
        if native_type in {"session/error", "error"}:
            return [
                self._next_event(
                    session_id,
                    "runtime.error",
                    {"reason": truncate_text(str(data.get("message") or data), limit=1024), "native_type": native_type},
                    native_type,
                )
            ]
        return []

    def _delta_events(self, session_id: str, native_type: str, stream: "list[Any]") -> list[RuntimeEvent]:
        """Convert DSH assistant-stream records into batched delta events.

        DSH compacts a model stream into delta runs; AgentOS republishes each run
        as one event and records that it is a run, not a single token.
        """
        events: list[RuntimeEvent] = []
        for record in stream:
            if not isinstance(record, Mapping):
                continue
            kind = record.get("type")
            if kind in {"text-chunks", "reasoning-chunks"}:
                texts = record.get("texts")
                if not isinstance(texts, list):
                    continue
                joined = "".join(item for item in texts if isinstance(item, str))
                if not joined:
                    continue
                events.append(
                    self._next_event(
                        session_id,
                        "message.delta",
                        {
                            "text": joined,
                            "channel": "reasoning" if kind == "reasoning-chunks" else "text",
                            "batched_run": len(texts),
                            "index": record.get("index"),
                        },
                        native_type,
                    )
                )
            elif kind == "tool-call-chunks":
                arguments = record.get("args")
                joined_args = "".join(item for item in arguments if isinstance(item, str)) if isinstance(arguments, list) else ""
                events.append(
                    self._next_event(
                        session_id,
                        "tool.call.started",
                        {
                            "call_id": record.get("id"),
                            "name": record.get("name"),
                            "arguments": _parse_raw_arguments(joined_args),
                            "batched_run": len(arguments) if isinstance(arguments, list) else 1,
                            "preliminary": True,
                        },
                        native_type,
                    )
                )
        return events


def _parse_raw_arguments(raw: Any) -> Any:
    """Decode a model-produced raw argument string without ever echoing a secret.

    The value is model JSON, so it is untrusted input. A non-object or
    undecodable value is returned as a bounded string under an explicit marker
    instead of being silently coerced.
    """
    if raw is None:
        return {}
    if not isinstance(raw, str):
        return {"unparsed": truncate_text(str(raw), limit=512)}
    text = raw.strip()
    if not text:
        return {}
    import json

    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return {"unparsed": truncate_text(text, limit=512)}
    if isinstance(decoded, Mapping):
        return dict(decoded)
    return {"unparsed": truncate_text(text, limit=512)}


__all__ = [
    "DEFAULT_INITIALIZE_TIMEOUT_SECONDS",
    "DEFAULT_TURN_TIMEOUT_SECONDS",
    "DSH_SDK_PROFILE",
    "DSH_SERVER_NAME",
    "DshAdapter",
    "DshSettings",
    "NodeVersion",
    "node_version_satisfies",
    "parse_node_version",
    "resolve_node_binary",
]
