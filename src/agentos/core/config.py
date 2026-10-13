"""Configuration loading, validation, defaults, and rendering.

The configuration layers in a fixed order: built-in defaults, then
``/etc/agentos/agentos.toml`` (package-managed), then
``/etc/agentos/agentos.local.toml`` (administrator overrides), then
``/etc/agentos/agentos.toml.d/*.toml`` fragments in lexical order. A later layer
wins. Secrets are never read from these files: model credentials come from the
service environment or a root-owned environment file, so a configuration file
can be committed, diffed, and reviewed without leaking anything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from collections.abc import Iterable, Mapping
from typing import Any, Final

from ..constants import (
    DSH_CLI_ENTRY,
    DSH_HOME_DIR,
    DSH_NODE_BINARY,
    SYSTEM_CACHE_DIR,
    SYSTEM_CONFIG_DIR,
    SYSTEM_CONFIG_FILE,
    SYSTEM_CONFIG_LOCAL_FILE,
    SYSTEM_LOG_DIR,
    SYSTEM_RUN_DIR,
    SYSTEM_SOCKET,
    SYSTEM_STATE_DB,
    SYSTEM_STATE_DIR,
)
from ..errors import ConfigurationError, ValidationError
from ..policy.engine import POLICY_DIRECTORY
from ..runtime.factory import RuntimeSettings
from ..toml import load_toml_file, merge_mappings

#: Directory holding configuration fragments, applied in lexical order.
CONFIG_FRAGMENT_DIRECTORY: Final = SYSTEM_CONFIG_DIR + "/agentos.toml.d"
#: File the packaged service reads model credentials from; mode 0600, root-owned.
CREDENTIAL_ENVIRONMENT_FILE: Final = SYSTEM_CONFIG_DIR + "/credentials.env"
#: Longest accepted fragment chain, a bound against a directory used as an attack.
MAX_CONFIG_FRAGMENTS: Final = 32


@dataclass(frozen=True, slots=True)
class CoreSettings:
    """Settings for the Core service process itself.

    :param socket_path: Unix domain socket the Core listens on.
    :param database_path: SQLite database path.
    :param state_directory: Service-owned state root.
    :param cache_directory: Service cache root.
    :param log_directory: Service log root.
    :param log_level: Minimum level written to the service log.
    :param max_concurrent_tasks: Tasks that may run at once.
    :param queue_poll_seconds: Delay between empty queue polls.
    :param worker_uid: Unix user the service runs as, used for ownership checks.
    :param workspace_root: Root directory for per-task workspaces.
    :param audit_retain_days: Audit retention in days.
    :param audit_max_records: Audit capacity bound.
    :param shutdown_grace_seconds: Time allowed for a clean shutdown.
    """

    socket_path: str = SYSTEM_SOCKET
    database_path: str = SYSTEM_STATE_DB
    state_directory: str = SYSTEM_STATE_DIR
    cache_directory: str = SYSTEM_CACHE_DIR
    log_directory: str = SYSTEM_LOG_DIR
    log_level: str = "INFO"
    max_concurrent_tasks: int = 2
    queue_poll_seconds: float = 0.5
    worker_uid: int | None = None
    workspace_root: str = SYSTEM_STATE_DIR + "/workspaces"
    audit_retain_days: int = 30
    audit_max_records: int = 200_000
    shutdown_grace_seconds: float = 15.0

    @property
    def socket_directory(self) -> str:
        """Return the directory holding the IPC socket."""
        return os.path.dirname(self.socket_path) or SYSTEM_RUN_DIR


@dataclass(frozen=True, slots=True)
class DesktopSettings:
    """Settings for the desktop application and the GNOME extension bridge.

    :param enabled: Whether the desktop application may be launched.
    :param notification_throttle_seconds: Minimum spacing between notifications.
    :param poll_interval_seconds: Status poll interval for the panel.
    """

    enabled: bool = True
    notification_throttle_seconds: int = 30
    poll_interval_seconds: int = 5


@dataclass(frozen=True, slots=True)
class Config:
    """The complete effective configuration.

    :param core: Core service settings.
    :param runtime: Runtime selection and parameters.
    :param desktop: Desktop integration settings.
    :param sources: Configuration files that contributed, in application order.
    :param policy_directory: Directory scanned for policy documents; overrides the
        packaged location so that tests and non-packaged deployments can point at
        their own policy tree.
    :param service_unit_allowlist: Units a restart may target even when no policy
        document names them.
    """

    core: CoreSettings = field(default_factory=CoreSettings)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    desktop: DesktopSettings = field(default_factory=DesktopSettings)
    sources: tuple[str, ...] = ()
    policy_directory: str = POLICY_DIRECTORY
    service_unit_allowlist: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the effective configuration with secret-bearing material excluded.

        This is what ``agentosctl doctor`` prints; it deliberately contains no
        environment values and no credential file contents.
        """
        return {
            "core": {
                "socket_path": self.core.socket_path,
                "database_path": self.core.database_path,
                "state_directory": self.core.state_directory,
                "cache_directory": self.core.cache_directory,
                "log_directory": self.core.log_directory,
                "log_level": self.core.log_level,
                "max_concurrent_tasks": self.core.max_concurrent_tasks,
                "queue_poll_seconds": self.core.queue_poll_seconds,
                "workspace_root": self.core.workspace_root,
                "audit_retain_days": self.core.audit_retain_days,
                "audit_max_records": self.core.audit_max_records,
            },
            "runtime": {
                "runtime_id": self.runtime.runtime_id,
                "provider": self.runtime.provider,
                "model": self.runtime.model,
                "reasoning_effort": self.runtime.reasoning_effort,
                "max_tokens": self.runtime.max_tokens,
                "node_binary": self.runtime.node_binary,
                "cli_entry": self.runtime.cli_entry,
                "dsh_home": self.runtime.dsh_home,
                "turn_timeout_seconds": self.runtime.turn_timeout_seconds,
            },
            "desktop": {
                "enabled": self.desktop.enabled,
                "notification_throttle_seconds": self.desktop.notification_throttle_seconds,
                "poll_interval_seconds": self.desktop.poll_interval_seconds,
            },
            "sources": list(self.sources),
        }


#: The configuration written by ``agentosctl config render`` and shipped as the package default.
DEFAULT_CONFIG_DOCUMENT: Final[str] = f"""# AgentOS Core configuration.
#
# This file is package-managed: an upgrade replaces it. Put local changes in
# {SYSTEM_CONFIG_LOCAL_FILE}, or in a fragment under
# {CONFIG_FRAGMENT_DIRECTORY}/ .
#
# Never put a model credential in this file. The service reads credentials from
# the environment, or from {CREDENTIAL_ENVIRONMENT_FILE} (mode 0600, root-owned).

[core]
# Unix domain socket the Core service listens on.
socket_path = "{SYSTEM_SOCKET}"
database_path = "{SYSTEM_STATE_DB}"
state_directory = "{SYSTEM_STATE_DIR}"
cache_directory = "{SYSTEM_CACHE_DIR}"
log_directory = "{SYSTEM_LOG_DIR}"
log_level = "INFO"
max_concurrent_tasks = 2
queue_poll_seconds = 0.5
workspace_root = "{SYSTEM_STATE_DIR}/workspaces"
audit_retain_days = 30
audit_max_records = 200000
shutdown_grace_seconds = 15.0

[runtime]
# "mock" runs without a model credential and is the safe default after install.
# Switch to "dsh" to drive DeepSeek Harness; the agentos-runtime-dsh package
# provides the Node.js runtime and the CLI entry point it needs.
runtime_id = "mock"
provider = ""
model = ""
reasoning_effort = ""
max_tokens = 4096
node_binary = "{DSH_NODE_BINARY}"
cli_entry = "{DSH_CLI_ENTRY}"
dsh_home = "{DSH_HOME_DIR}"
turn_timeout_seconds = 900.0
initialize_timeout_seconds = 60.0

[runtime.mock_scenario]
# Scripted assistant replies used by the mock runtime.
replies = [
  "Mock runtime reply: no model credential is configured, so this response is scripted. Configure runtime.runtime_id = \\"dsh\\" and provide a credential to enable real analysis.",
]
chunk_size = 32

[desktop]
enabled = true
notification_throttle_seconds = 30
poll_interval_seconds = 5
"""


def _require_table(document: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """Return a named table from a configuration document.

    :raises ConfigurationError: When the member exists but is not a table.
    """
    value = document.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"configuration section [{name}] must be a table")
    return value


#: Configuration members accepted in the [core] table.
_CORE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "socket_path",
        "database_path",
        "state_directory",
        "cache_directory",
        "log_directory",
        "log_level",
        "max_concurrent_tasks",
        "queue_poll_seconds",
        "worker_uid",
        "workspace_root",
        "audit_retain_days",
        "audit_max_records",
        "shutdown_grace_seconds",
    }
)

#: Configuration members accepted in the [desktop] table.
_DESKTOP_KEYS: Final[frozenset[str]] = frozenset(
    {"enabled", "notification_throttle_seconds", "poll_interval_seconds"}
)


def _build_core(document: Mapping[str, Any]) -> CoreSettings:
    """Build :class:`CoreSettings` from an untrusted table."""
    unknown = sorted(set(str(key) for key in document) - _CORE_KEYS)
    if unknown:
        raise ConfigurationError(
            "unknown [core] setting(s): " + ", ".join(unknown),
            details={"unknown": unknown, "known": sorted(_CORE_KEYS)},
        )
    defaults = CoreSettings()
    values: dict[str, Any] = {}
    for key in ("socket_path", "database_path", "state_directory", "cache_directory", "log_directory", "workspace_root"):
        if key in document:
            value = document[key]
            if not isinstance(value, str) or not value:
                raise ConfigurationError(f"core.{key} must be a non-empty string")
            if not os.path.isabs(value):
                raise ConfigurationError(f"core.{key} must be an absolute path", details={"value": value})
            values[key] = value
    if "log_level" in document:
        level = document["log_level"]
        if not isinstance(level, str) or level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("core.log_level must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")
        values["log_level"] = level.upper()
    for key in ("max_concurrent_tasks", "audit_retain_days", "audit_max_records"):
        if key in document:
            value = document[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigurationError(f"core.{key} must be a positive integer")
            values[key] = value
    if "worker_uid" in document:
        value = document["worker_uid"]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ConfigurationError("core.worker_uid must be a non-negative integer or omitted")
        values["worker_uid"] = value
    for key in ("queue_poll_seconds", "shutdown_grace_seconds"):
        if key in document:
            value = document[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ConfigurationError(f"core.{key} must be a positive number")
            values[key] = float(value)
    if values.get("max_concurrent_tasks", defaults.max_concurrent_tasks) > 32:
        raise ConfigurationError("core.max_concurrent_tasks must not exceed 32")
    return replace(defaults, **values) if values else defaults


def _build_desktop(document: Mapping[str, Any]) -> DesktopSettings:
    """Build :class:`DesktopSettings` from an untrusted table."""
    unknown = sorted(set(str(key) for key in document) - _DESKTOP_KEYS)
    if unknown:
        raise ConfigurationError(
            "unknown [desktop] setting(s): " + ", ".join(unknown),
            details={"unknown": unknown, "known": sorted(_DESKTOP_KEYS)},
        )
    values: dict[str, Any] = {}
    if "enabled" in document:
        if not isinstance(document["enabled"], bool):
            raise ConfigurationError("desktop.enabled must be a boolean")
        values["enabled"] = document["enabled"]
    for key in ("notification_throttle_seconds", "poll_interval_seconds"):
        if key in document:
            value = document[key]
            if isinstance(value, bool) or not isinstance(value, int) or not 2 <= value <= 600:
                raise ConfigurationError(f"desktop.{key} must be an integer between 2 and 600")
            values[key] = value
    return replace(DesktopSettings(), **values) if values else DesktopSettings()


def config_document_layers(
    *,
    base_path: str = SYSTEM_CONFIG_FILE,
    local_path: str = SYSTEM_CONFIG_LOCAL_FILE,
    fragment_directory: str = CONFIG_FRAGMENT_DIRECTORY,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Load and merge every configuration layer.

    :returns: ``(merged_document, contributing_paths)``.
    :raises ConfigurationError: When a layer is unreadable or has a type conflict.
    """
    merged: dict[str, Any] = {}
    sources: list[str] = []
    base = load_toml_file(base_path)
    if base:
        merged = merge_mappings(merged, base)
        sources.append(base_path)
    fragments: list[str] = []
    if os.path.isdir(fragment_directory):
        names = sorted(name for name in os.listdir(fragment_directory) if name.endswith(".toml"))
        if len(names) > MAX_CONFIG_FRAGMENTS:
            raise ConfigurationError(
                f"{fragment_directory} contains {len(names)} fragments; at most {MAX_CONFIG_FRAGMENTS} are applied",
                details={"directory": fragment_directory},
            )
        fragments = [os.path.join(fragment_directory, name) for name in names]
    for path in fragments:
        fragment = load_toml_file(path)
        if fragment:
            merged = merge_mappings(merged, fragment)
            sources.append(path)
    local = load_toml_file(local_path)
    if local:
        merged = merge_mappings(merged, local)
        sources.append(local_path)
    return merged, tuple(sources)


def load_config(
    *,
    base_path: str = SYSTEM_CONFIG_FILE,
    local_path: str = SYSTEM_CONFIG_LOCAL_FILE,
    fragment_directory: str = CONFIG_FRAGMENT_DIRECTORY,
) -> Config:
    """Load the effective configuration from disk.

    A missing configuration file is not an error: the built-in defaults are the
    documented fallback, and they select the mock runtime, which needs no
    credential.

    :raises ConfigurationError: When any present layer is invalid.
    """
    document, sources = config_document_layers(
        base_path=base_path,
        local_path=local_path,
        fragment_directory=fragment_directory,
    )
    known = {"core", "runtime", "desktop"}
    unknown = sorted(set(str(key) for key in document) - known)
    if unknown:
        raise ConfigurationError(
            "unknown configuration section(s): " + ", ".join(unknown),
            details={"unknown": unknown, "known": sorted(known)},
        )
    core = _build_core(_require_table(document, "core"))
    runtime = RuntimeSettings.from_mapping(_require_table(document, "runtime"))
    desktop = _build_desktop(_require_table(document, "desktop"))
    return Config(core=core, runtime=runtime, desktop=desktop, sources=sources)


class ConfigurationLoader:
    """Reloadable configuration holder for a long-running service.

    The service keeps its startup configuration and asks this object for a fresh
    one on ``SIGHUP``. A reload never leaves the service without a usable
    configuration: an invalid file is reported and the previous configuration
    stays active.
    """

    def __init__(self, config: Config) -> None:
        self._config = config

    @property
    def current(self) -> Config:
        """Return the active configuration."""
        return self._config

    def reload(
        self,
        *,
        base_path: str = SYSTEM_CONFIG_FILE,
        local_path: str = SYSTEM_CONFIG_LOCAL_FILE,
        fragment_directory: str = CONFIG_FRAGMENT_DIRECTORY,
    ) -> tuple[Config, str | None]:
        """Attempt a reload.

        :returns: ``(config, error)`` where ``error`` is ``None`` on success and
            otherwise explains why the previous configuration was kept.
        """
        try:
            fresh = load_config(
                base_path=base_path,
                local_path=local_path,
                fragment_directory=fragment_directory,
            )
        except (ConfigurationError, ValidationError) as error:
            return self._config, error.message
        self._config = fresh
        return fresh, None


def read_credential_environment() -> dict[str, str]:
    """Read model credentials from the root-owned environment file.

    Only names matching a small allowlist are returned, so a file that
    accumulates unrelated variables cannot inject them into the runtime child.
    The file is never logged, and values are never returned through IPC.

    :returns: Mapping of allowed variable names to values.
    :raises ConfigurationError: When the file exists but is unusable.
    """
    allowed = {
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "DSH_MODEL",
        "DSH_PROVIDER",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
    }
    path = CREDENTIAL_ENVIRONMENT_FILE
    if not os.path.exists(path):
        return {}
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError as error:
        raise ConfigurationError(f"cannot stat {path}: {error}") from error
    if mode & 0o077:
        raise ConfigurationError(
            f"{path} is readable by group or other (mode {mode:03o}); "
            "chmod 600 the file and retry so that model credentials stay private",
            details={"path": path, "mode": f"{mode:03o}"},
        )
    values: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for number, raw in enumerate(handle, start=1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                name, separator, value = line.partition("=")
                if not separator:
                    raise ConfigurationError(f"{path}:{number}: expected NAME=value")
                name = name.strip()
                if name not in allowed:
                    continue
                cleaned = value.strip().strip('"').strip("'")
                if cleaned:
                    values[name] = cleaned
    except OSError as error:
        raise ConfigurationError(f"cannot read {path}: {error}") from error
    return values


def render_config(config: Config, *, header: str = "") -> str:
    """Render a configuration document from an effective :class:`Config`.

    :param config: Effective configuration to render.
    :param header: Optional comment block placed above the document.
    """
    lines: list[str] = []
    if header:
        for line in header.strip().splitlines():
            lines.append(f"# {line}" if line else "#")
        lines.append("")
    lines.append("[core]")
    lines.append(f'socket_path = "{config.core.socket_path}"')
    lines.append(f'database_path = "{config.core.database_path}"')
    lines.append(f'state_directory = "{config.core.state_directory}"')
    lines.append(f'cache_directory = "{config.core.cache_directory}"')
    lines.append(f'log_directory = "{config.core.log_directory}"')
    lines.append(f'log_level = "{config.core.log_level}"')
    lines.append(f"max_concurrent_tasks = {config.core.max_concurrent_tasks}")
    lines.append(f"queue_poll_seconds = {config.core.queue_poll_seconds}")
    lines.append(f'workspace_root = "{config.core.workspace_root}"')
    lines.append(f"audit_retain_days = {config.core.audit_retain_days}")
    lines.append(f"audit_max_records = {config.core.audit_max_records}")
    lines.append(f"shutdown_grace_seconds = {config.core.shutdown_grace_seconds}")
    if config.core.worker_uid is not None:
        lines.append(f"worker_uid = {config.core.worker_uid}")
    lines.append("")
    lines.append("[runtime]")
    lines.append(f'runtime_id = "{config.runtime.runtime_id}"')
    lines.append(f'provider = "{config.runtime.provider}"')
    lines.append(f'model = "{config.runtime.model}"')
    rendered_effort = config.runtime.reasoning_effort or ""
    lines.append(f'reasoning_effort = "{rendered_effort}"')
    if config.runtime.max_tokens is not None:
        lines.append(f"max_tokens = {config.runtime.max_tokens}")
    lines.append(f'node_binary = "{config.runtime.node_binary}"')
    lines.append(f'cli_entry = "{config.runtime.cli_entry}"')
    lines.append(f'dsh_home = "{config.runtime.dsh_home}"')
    if config.runtime.turn_timeout_seconds is not None:
        lines.append(f"turn_timeout_seconds = {config.runtime.turn_timeout_seconds}")
    if config.runtime.initialize_timeout_seconds is not None:
        lines.append(f"initialize_timeout_seconds = {config.runtime.initialize_timeout_seconds}")
    if config.runtime.mock_scenario:
        replies = config.runtime.mock_scenario.get("replies")
        if isinstance(replies, (list, tuple)) and replies:
            lines.append("")
            lines.append("[runtime.mock_scenario]")
            lines.append("replies = [")
            for reply in replies:
                escaped = str(reply).replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'  "{escaped}",')
            lines.append("]")
            chunk_size = config.runtime.mock_scenario.get("chunk_size")
            if isinstance(chunk_size, int) and not isinstance(chunk_size, bool):
                lines.append(f"chunk_size = {chunk_size}")
    lines.append("")
    lines.append("[desktop]")
    lines.append(f"enabled = {'true' if config.desktop.enabled else 'false'}")
    lines.append(f"notification_throttle_seconds = {config.desktop.notification_throttle_seconds}")
    lines.append(f"poll_interval_seconds = {config.desktop.poll_interval_seconds}")
    lines.append("")
    return "\n".join(lines)


def write_config_atomically(path: str, document: str, *, mode: int = 0o644) -> None:
    """Write a configuration document atomically.

    A partial write would leave an unreadable configuration behind, so the new
    content goes to a temporary file in the same directory and is renamed over
    the target.

    :param path: Destination path.
    :param document: Complete document text.
    :param mode: File mode applied before the rename.
    :raises ConfigurationError: When the write fails.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    temporary = os.path.join(directory, f".{os.path.basename(path)}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError as error:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise ConfigurationError(f"cannot write {path}: {error}", details={"path": path}) from error


def iter_config_paths() -> Iterable[str]:
    """Yield every path that contributes to the effective configuration."""
    yield SYSTEM_CONFIG_FILE
    if os.path.isdir(CONFIG_FRAGMENT_DIRECTORY):
        for name in sorted(os.listdir(CONFIG_FRAGMENT_DIRECTORY)):
            if name.endswith(".toml"):
                yield os.path.join(CONFIG_FRAGMENT_DIRECTORY, name)
    yield SYSTEM_CONFIG_LOCAL_FILE


__all__ = [
    "CONFIG_FRAGMENT_DIRECTORY",
    "CREDENTIAL_ENVIRONMENT_FILE",
    "DEFAULT_CONFIG_DOCUMENT",
    "MAX_CONFIG_FRAGMENTS",
    "Config",
    "ConfigurationLoader",
    "CoreSettings",
    "DesktopSettings",
    "config_document_layers",
    "iter_config_paths",
    "load_config",
    "read_credential_environment",
    "render_config",
    "write_config_atomically",
]
