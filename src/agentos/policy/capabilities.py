"""Tool capability manifests and the privilege levels AgentOS enforces.

A capability manifest is the only place a tool's authority is declared. The
policy engine refuses to evaluate a tool that has no manifest, so adding a tool
without declaring its blast radius is a hard failure rather than a silent grant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from collections.abc import Iterable, Mapping
from typing import Any, Final

from ..constants import CAPABILITY_MANIFEST_VERSION
from ..errors import ValidationError
from ..schema import FORBIDDEN_PARAMETER_NAMES, ParameterSchema, ParameterSpec
from .decision import Decision


class PrivilegeLevel(IntEnum):
    """Authority a tool requires.

    The numeric order is meaningful: a policy rule may cap a task at a level, and
    every higher level is a strict superset of risk.
    """

    #: Read-only inspection of explicitly allowed system facts.
    READ_ONLY = 0
    #: Writes confined to AgentOS-owned state and the task's own workspace.
    RESTRICTED_WRITE = 1
    #: Action with a real system side effect on a fixed, allowlisted target.
    CONTROLLED = 2
    #: Administrative action: packages, network, users, power, system config.
    ADMINISTRATIVE = 3

    @property
    def label(self) -> str:
        """Return the short operator-facing level name."""
        return _LEVEL_LABELS[self]


_LEVEL_LABELS: Final[Mapping[PrivilegeLevel, str]] = {
    PrivilegeLevel.READ_ONLY: "P0 read-only",
    PrivilegeLevel.RESTRICTED_WRITE: "P1 restricted write",
    PrivilegeLevel.CONTROLLED: "P2 controlled operation",
    PrivilegeLevel.ADMINISTRATIVE: "P3 administrative operation",
}


@dataclass(frozen=True, slots=True)
class ToolManifest:
    """Declared authority and parameter set for one tool.

    :param name: Dotted tool name, for example ``system.processes.list``.
    :param summary: One-line operator-facing description.
    :param level: Privilege level the tool requires.
    :param parameters: Closed parameter set; unknown parameters are rejected.
    :param target_parameter: Parameter naming the single object the tool acts on,
        when the tool acts on one object. Approval binds to its value.
    :param requires_approval: Whether an approval is mandatory at this level.
    :param denied: When true the tool exists only to be refused with a reason.
    :param allowlist_parameter: Parameter whose value must come from a fixed set
        enforced by the executor rather than by the caller.
    :param read_only_guarantee: Short statement of what the tool cannot change.
    :param idempotent: Whether repeating the call is safe during crash recovery.
    """

    name: str
    summary: str
    level: PrivilegeLevel
    parameters: ParameterSchema = field(default_factory=ParameterSchema)
    target_parameter: str | None = None
    requires_approval: bool = False
    denied: bool = False
    allowlist_parameter: str | None = None
    read_only_guarantee: str = ""
    idempotent: bool = True

    def __post_init__(self) -> None:
        if not self.name or not all(part.isidentifier() or part.islower() for part in self.name.split(".")):
            raise ValidationError(f"invalid tool name {self.name!r}")
        if self.denied and not self.summary:
            raise ValidationError(f"denied tool {self.name!r} must explain why it is unavailable")
        if self.target_parameter is not None and self.target_parameter not in self.parameters.names():
            raise ValidationError(
                f"tool {self.name!r} declares target parameter {self.target_parameter!r}, which it does not accept",
                details={"tool": self.name, "parameter": self.target_parameter},
            )
        if self.allowlist_parameter is not None and self.allowlist_parameter not in self.parameters.names():
            raise ValidationError(
                f"tool {self.name!r} declares allowlist parameter {self.allowlist_parameter!r}, which it does not accept",
                details={"tool": self.name, "parameter": self.allowlist_parameter},
            )
        if self.level >= PrivilegeLevel.CONTROLLED and not self.requires_approval and not self.denied:
            raise ValidationError(
                f"tool {self.name!r} at {self.level.label} must require approval or be denied",
                details={"tool": self.name, "level": int(self.level)},
            )
        risky = self.parameters.names() & FORBIDDEN_PARAMETER_NAMES
        if risky:
            raise ValidationError(
                f"tool {self.name!r} declares shell-shaped parameter(s): " + ", ".join(sorted(risky)),
                details={"tool": self.name, "parameters": sorted(risky)},
            )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON Schema description of this manifest."""
        return {
            "manifest_version": CAPABILITY_MANIFEST_VERSION,
            "name": self.name,
            "summary": self.summary,
            "level": int(self.level),
            "level_label": self.level.label,
            "denied": self.denied,
            "requires_approval": self.requires_approval,
            "target_parameter": self.target_parameter,
            "allowlist_parameter": self.allowlist_parameter,
            "read_only_guarantee": self.read_only_guarantee,
            "idempotent": self.idempotent,
            "parameters": self.parameters.to_json_schema(),
            "notes": list(self.parameters.notes),
        }

    def validate_parameters(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and normalize a call's parameters against this manifest."""
        if self.denied:
            raise ValidationError(
                f"tool {self.name!r} is denied by policy: {self.summary}",
                details={"tool": self.name, "denied": True},
            )
        return self.parameters.validate(value)

    def target_of(self, parameters: Mapping[str, Any]) -> str | None:
        """Return the action target named by ``parameters``, when one is declared."""
        if self.target_parameter is None:
            return None
        value = parameters.get(self.target_parameter)
        return None if value is None else str(value)


def _read_only(name: str, summary: str, parameters: Iterable[ParameterSpec], guarantee: str) -> ToolManifest:
    """Build a P0 manifest, which never needs an approval."""
    return ToolManifest(
        name=name,
        summary=summary,
        level=PrivilegeLevel.READ_ONLY,
        parameters=ParameterSchema(parameters=tuple(parameters)),
        read_only_guarantee=guarantee,
    )


#: The shipped tool inventory. Every tool the Core can execute appears here.
BUILTIN_MANIFESTS: Final[tuple[ToolManifest, ...]] = (
    _read_only(
        "system.metrics.read",
        "Read CPU, memory, swap, and filesystem utilisation",
        (),
        "reads /proc and statvfs only; no state is written",
    ),
    _read_only(
        "system.processes.list",
        "List processes ordered by CPU or memory use",
        (
            ParameterSpec("limit", "integer", required=False, minimum=1, maximum=200, description="Rows to return"),
            ParameterSpec(
                "sort_by",
                "string",
                required=False,
                choices=("cpu", "memory", "pid"),
                description="Ordering key",
            ),
            ParameterSpec(
                "min_cpu_percent",
                "number",
                required=False,
                minimum=0.0,
                maximum=100.0,
                description="Skip processes below this CPU share",
            ),
        ),
        "reads /proc only; never signals or renicers a process",
    ),
    _read_only(
        "system.process.inspect",
        "Read one process's command line, state, and resource use",
        (
            ParameterSpec("pid", "integer", minimum=1, maximum=4194304, description="Process id to inspect"),
        ),
        "reads /proc/<pid> only",
    ),
    _read_only(
        "system.disks.read",
        "Report per-filesystem capacity and inode use",
        (),
        "calls statvfs and reads /proc/mounts only",
    ),
    _read_only(
        "system.uptime.read",
        "Report uptime, load averages, and boot time",
        (),
        "reads /proc/uptime and /proc/loadavg only",
    ),
    _read_only(
        "system.services.list",
        "List failed or filtered systemd units visible to the service",
        (
            ParameterSpec(
                "state",
                "string",
                required=False,
                choices=("failed", "running", "all"),
                description="Unit state filter",
            ),
            ParameterSpec("limit", "integer", required=False, minimum=1, maximum=500, description="Rows to return"),
        ),
        "calls systemctl show/list-units in read-only mode",
    ),
    _read_only(
        "system.service.status",
        "Read one systemd unit's load, active, and sub state",
        (
            ParameterSpec(
                "unit",
                "string",
                max_length=256,
                description="Unit name, for example ssh.service",
            ),
        ),
        "calls systemctl show for exactly one unit name",
    ),
    _read_only(
        "system.logs.read",
        "Read a bounded number of journal entries",
        (
            ParameterSpec("unit", "string", required=False, max_length=256, description="Restrict to one unit"),
            ParameterSpec("priority", "integer", required=False, minimum=0, maximum=7, description="Maximum syslog level"),
            ParameterSpec("lines", "integer", required=False, minimum=1, maximum=500, description="Entry count"),
            ParameterSpec("since", "string", required=False, max_length=64, description="journalctl --since expression"),
        ),
        "calls journalctl with --no-pager and a fixed line bound; never reads arbitrary files",
    ),
    ToolManifest(
        name="system.service.restart",
        summary="Restart exactly one allowlisted systemd unit",
        level=PrivilegeLevel.CONTROLLED,
        parameters=ParameterSchema(
            parameters=(
                ParameterSpec("unit", "string", max_length=256, description="Unit name from the allowlist"),
            )
        ),
        target_parameter="unit",
        requires_approval=True,
        allowlist_parameter="unit",
        read_only_guarantee="cannot be used for any unit outside the configured allowlist",
        idempotent=False,
    ),
    ToolManifest(
        name="system.process.terminate",
        summary="Send SIGTERM to one explicitly identified process",
        level=PrivilegeLevel.CONTROLLED,
        parameters=ParameterSchema(
            parameters=(
                ParameterSpec("pid", "integer", minimum=2, maximum=4194304, description="Process id to signal"),
                ParameterSpec("reason", "string", max_length=280, required=False, description="Why"),
            )
        ),
        target_parameter="pid",
        requires_approval=True,
        read_only_guarantee="sends SIGTERM only; never SIGKILL, never a process group, never pid 1",
        idempotent=False,
    ),
    ToolManifest(
        name="system.packages.install",
        summary="Install or remove Debian packages",
        level=PrivilegeLevel.ADMINISTRATIVE,
        parameters=ParameterSchema(additional=False),
        denied=True,
        read_only_guarantee="unavailable in this release",
    ),
    ToolManifest(
        name="system.network.configure",
        summary="Change network configuration",
        level=PrivilegeLevel.ADMINISTRATIVE,
        parameters=ParameterSchema(additional=False),
        denied=True,
        read_only_guarantee="unavailable in this release",
    ),
    ToolManifest(
        name="system.shell.execute",
        summary="Run a shell command string",
        level=PrivilegeLevel.ADMINISTRATIVE,
        parameters=ParameterSchema(additional=False),
        denied=True,
        read_only_guarantee="AgentOS never exposes a shell to a model",
    ),
    ToolManifest(
        name="system.power.manage",
        summary="Reboot, shut down, or suspend the machine",
        level=PrivilegeLevel.ADMINISTRATIVE,
        parameters=ParameterSchema(additional=False),
        denied=True,
        read_only_guarantee="unavailable in this release",
    ),
    ToolManifest(
        name="system.files.write",
        summary="Write to an arbitrary filesystem path",
        level=PrivilegeLevel.ADMINISTRATIVE,
        parameters=ParameterSchema(additional=False),
        denied=True,
        read_only_guarantee="writes are confined to the task workspace by the runtime account",
    ),
)


def builtin_manifest_index() -> dict[str, ToolManifest]:
    """Return the shipped manifests keyed by tool name."""
    return {manifest.name: manifest for manifest in BUILTIN_MANIFESTS}


def find_manifest(name: str, extra: Iterable[ToolManifest] = ()) -> ToolManifest | None:
    """Return the manifest for ``name``, consulting ``extra`` after the built-ins."""
    for manifest in (*BUILTIN_MANIFESTS, *extra):
        if manifest.name == name:
            return manifest
    return None


__all__ = [
    "BUILTIN_MANIFESTS",
    "Decision",
    "PrivilegeLevel",
    "ToolManifest",
    "builtin_manifest_index",
    "find_manifest",
]
