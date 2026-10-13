"""Runtime selection: map configuration onto a concrete :class:`AgentRuntime`.

Core asks this module for a runtime by name and never imports an adapter
directly. That keeps the adapter set an explicit, auditable list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, Final

from ..errors import ConfigurationError
from .api import AgentRuntime
from .dsh import DshAdapter, DshSettings
from .mock import MockRuntime, load_scenario

#: Runtime identifiers shipped in the first release.
RUNTIME_IDS: Final[tuple[str, ...]] = ("mock", "dsh")


@dataclass(slots=True)
class RuntimeSettings:
    """Deployment-level runtime selection and its parameters.

    :param runtime_id: One of :data:`RUNTIME_IDS`.
    :param provider: Model provider route for adapters that need one.
    :param model: Model name for adapters that need one.
    :param reasoning_effort: Optional adapter-owned reasoning effort id.
    :param max_tokens: Optional positive output-token cap.
    :param node_binary: Node.js executable for the DSH adapter.
    :param cli_entry: ``dsh`` CLI entry point for the DSH adapter.
    :param dsh_home: Harness home directory.
    :param workspace_root: Root under which per-task workspaces live.
    :param turn_timeout_seconds: Deadline for one prompt turn.
    :param initialize_timeout_seconds: Deadline for the SDK handshake.
    :param mock_scenario: Scripted mock behavior, used only by ``runtime_id = "mock"``.
    """

    runtime_id: str = "mock"
    provider: str = ""
    model: str = ""
    reasoning_effort: str | None = None
    max_tokens: int | None = None
    node_binary: str = ""
    cli_entry: str = ""
    dsh_home: str = ""
    workspace_root: str = ""
    turn_timeout_seconds: float | None = None
    initialize_timeout_seconds: float | None = None
    mock_scenario: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "RuntimeSettings":
        """Build settings from a decoded configuration table.

        :param data: A TOML/JSON object; unknown members are rejected so that a
            typo cannot silently change the selected runtime.
        :raises ConfigurationError: On an unknown member or wrong value type.
        """
        known = {
            "runtime_id",
            "provider",
            "model",
            "reasoning_effort",
            "max_tokens",
            "node_binary",
            "cli_entry",
            "dsh_home",
            "workspace_root",
            "turn_timeout_seconds",
            "initialize_timeout_seconds",
            "mock_scenario",
        }
        unknown = sorted(set(str(key) for key in data) - known)
        if unknown:
            raise ConfigurationError(
                "unknown runtime setting(s): " + ", ".join(unknown),
                details={"unknown": unknown, "known": sorted(known)},
            )
        settings = cls()
        for key, value in data.items():
            if key in {"reasoning_effort", "provider", "model", "node_binary", "cli_entry", "dsh_home", "workspace_root"}:
                if value is not None and not isinstance(value, str):
                    raise ConfigurationError(f"runtime.{key} must be a string", details={"field": key})
                setattr(settings, key, value or "")
            elif key == "runtime_id":
                if not isinstance(value, str):
                    raise ConfigurationError("runtime.runtime_id must be a string", details={"field": key})
                settings.runtime_id = value
            elif key == "max_tokens":
                if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                    raise ConfigurationError("runtime.max_tokens must be an integer", details={"field": key})
                settings.max_tokens = value
            elif key in {"turn_timeout_seconds", "initialize_timeout_seconds"}:
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                    raise ConfigurationError(f"runtime.{key} must be a number", details={"field": key})
                setattr(settings, key, None if value is None else float(value))
            elif key == "mock_scenario":
                if not isinstance(value, Mapping):
                    raise ConfigurationError("runtime.mock_scenario must be a table", details={"field": key})
                settings.mock_scenario = dict(value)
        if settings.reasoning_effort == "":
            settings.reasoning_effort = None
        return settings

    def dsh_settings(self) -> DshSettings:
        """Return the DSH adapter settings implied by this configuration."""
        from ..constants import DSH_CLI_ENTRY, DSH_HOME_DIR, DSH_NODE_BINARY, SYSTEM_STATE_DIR

        return DshSettings(
            node_binary=self.node_binary or DSH_NODE_BINARY,
            cli_entry=self.cli_entry or DSH_CLI_ENTRY,
            dsh_home=self.dsh_home or DSH_HOME_DIR,
            provider=self.provider,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            max_tokens=self.max_tokens,
            turn_timeout_seconds=(
                self.turn_timeout_seconds if self.turn_timeout_seconds is not None else 900.0
            ),
            initialize_timeout_seconds=(
                self.initialize_timeout_seconds if self.initialize_timeout_seconds is not None else 60.0
            ),
            workspace_root=self.workspace_root or SYSTEM_STATE_DIR + "/workspaces",
        )


def create_runtime(settings: RuntimeSettings) -> AgentRuntime:
    """Build the runtime named by ``settings``.

    :param settings: Effective runtime configuration.
    :returns: A started-on-demand :class:`AgentRuntime`.
    :raises ConfigurationError: When the runtime id is unknown or unsupported.
    """
    if settings.runtime_id == "mock":
        return MockRuntime(scenario=load_scenario(settings.mock_scenario) if settings.mock_scenario else None)
    if settings.runtime_id == "dsh":
        return DshAdapter(settings.dsh_settings())
    raise ConfigurationError(
        f"unknown runtime {settings.runtime_id!r}",
        details={"runtime_id": settings.runtime_id, "known": list(RUNTIME_IDS)},
    )


def list_runtimes() -> list[dict[str, Any]]:
    """Describe every shipped runtime for the doctor command and documentation."""
    return [
        {
            "runtime_id": "mock",
            "purpose": "keyless, network-free runtime for tests and offline installs",
            "requires_credentials": False,
        },
        {
            "runtime_id": "dsh",
            "purpose": "DeepSeek Harness SDK profile over JSON-RPC stdio",
            "requires_credentials": True,
            "locked_version": "0.2.1-alpha.2",
            "node_engines": "^22.19.0 || >=24.0.0",
        },
    ]


__all__ = ["RUNTIME_IDS", "RuntimeSettings", "create_runtime", "list_runtimes"]
