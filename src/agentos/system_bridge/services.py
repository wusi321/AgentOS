"""systemd and journal access.

Every call here runs ``systemctl`` or ``journalctl`` with a fixed argument list,
no shell, a timeout, and a bounded output size. Unit names are validated against
the systemd unit-name grammar before they reach argv, and log reads never name a
file: they go through journald only.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from collections.abc import Iterable
from typing import Any, Final

from ..errors import SystemBridgeError, ValidationError

#: Maximum bytes kept from one helper invocation.
MAX_HELPER_OUTPUT_BYTES: Final = 512 * 1024
#: Default helper timeout.
DEFAULT_HELPER_TIMEOUT_SECONDS: Final = 20.0

#: systemd unit-name grammar: an optional instance suffix, a type suffix, and
#: only characters systemd itself permits in a unit name.
_UNIT_NAME_PATTERN: Final = re.compile(r"^[A-Za-z0-9:_.\\@-]{1,256}$")
#: Accepted unit type suffixes for the tools AgentOS exposes.
_UNIT_SUFFIXES: Final = (".service", ".socket", ".target", ".mount", ".timer", ".path", ".slice", ".device")

#: systemd unit states reported by ``systemctl show``.
_UNIT_PROPERTIES: Final = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "Description",
    "MainPID",
    "ExecMainStatus",
    "ActiveEnterTimestamp",
    "NRestarts",
    "MemoryCurrent",
)


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    """Observable state of one systemd unit.

    :param unit: Unit name as requested.
    :param load_state: ``loaded``, ``not-found``, ``masked``, or ``error``.
    :param active_state: ``active``, ``inactive``, ``failed``, or ``activating``.
    :param sub_state: Finer-grained state, for example ``running`` or ``dead``.
    :param unit_file_state: Whether the unit is enabled.
    :param description: Unit description from systemd.
    :param main_pid: Main process id, when the unit has one.
    :param exec_main_status: Exit status of the last main process run.
    :param active_enter_timestamp: When the unit last became active.
    :param restart_count: Times systemd restarted the unit.
    :param memory_current_bytes: Current memory accounting, when available.
    """

    unit: str
    load_state: str = "unknown"
    active_state: str = "unknown"
    sub_state: str = ""
    unit_file_state: str = ""
    description: str = ""
    main_pid: int | None = None
    exec_main_status: int | None = None
    active_enter_timestamp: str = ""
    restart_count: int | None = None
    memory_current_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable description of this unit."""
        return {
            "unit": self.unit,
            "load_state": self.load_state,
            "active_state": self.active_state,
            "sub_state": self.sub_state,
            "unit_file_state": self.unit_file_state,
            "description": self.description,
            "main_pid": self.main_pid,
            "exec_main_status": self.exec_main_status,
            "active_enter_timestamp": self.active_enter_timestamp,
            "restart_count": self.restart_count,
            "memory_current_mib": (
                None if self.memory_current_bytes is None else round(self.memory_current_bytes / (1024**2), 2)
            ),
        }


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One journald record.

    :param timestamp: Journal timestamp string.
    :param priority: Syslog priority number, 0 (emergency) to 7 (debug).
    :param unit: Unit that emitted the record, when known.
    :param identifier: Syslog identifier.
    :param message: Record message, bounded.
    :param pid: Emitting process id.
    """

    timestamp: str
    priority: int
    unit: str
    identifier: str
    message: str
    pid: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable description of this record."""
        return {
            "timestamp": self.timestamp,
            "priority": self.priority,
            "unit": self.unit,
            "identifier": self.identifier,
            "message": self.message,
            "pid": self.pid,
        }


@dataclass(frozen=True, slots=True)
class HelperResult:
    """Outcome of one helper invocation.

    :param argv: The exact argument vector executed.
    :param return_code: Process exit status.
    :param stdout: Bounded standard output.
    :param stderr: Bounded standard error.
    :param timed_out: Whether the helper exceeded its deadline.
    """

    argv: tuple[str, ...]
    return_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        """Return whether the helper exited successfully."""
        return self.return_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable description of this invocation."""
        return {
            "argv": list(self.argv),
            "return_code": self.return_code,
            "timed_out": self.timed_out,
            "stderr": self.stderr[-1024:],
        }


def validate_unit_name(unit: str) -> str:
    """Validate a systemd unit name before it reaches argv.

    :param unit: Candidate unit name.
    :raises ValidationError: When the name cannot be a systemd unit.
    """
    if not isinstance(unit, str) or not unit:
        raise ValidationError("unit name must be a non-empty string")
    if len(unit) > 256:
        raise ValidationError("unit name exceeds 256 characters")
    if not _UNIT_NAME_PATTERN.match(unit):
        raise ValidationError(
            "unit name contains characters systemd does not permit",
            details={"unit": unit[:64]},
        )
    if not unit.endswith(_UNIT_SUFFIXES):
        raise ValidationError(
            "unit name must carry a unit type suffix, for example ssh.service",
            details={"unit": unit},
        )
    if "/" in unit or unit.startswith("-"):
        raise ValidationError("unit name must not contain a path or option prefix", details={"unit": unit})
    return unit


def run_helper(
    argv: Iterable[str],
    *,
    timeout_seconds: float = DEFAULT_HELPER_TIMEOUT_SECONDS,
) -> HelperResult:
    """Run a helper with a fixed argv, no shell, and bounded output.

    :param argv: Executable and arguments.
    :param timeout_seconds: Deadline applied to the helper.
    :raises SystemBridgeError: When the executable is unavailable.
    """
    vector = tuple(str(item) for item in argv)
    if not vector:
        raise ValidationError("helper argument vector must not be empty")
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, shell never used
            vector,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        )
    except FileNotFoundError as error:
        raise SystemBridgeError(
            f"{vector[0]} is not installed on this system",
            details={"executable": vector[0]},
        ) from error
    except subprocess.TimeoutExpired as error:
        return HelperResult(argv=vector, return_code=-1, timed_out=True, stderr=str(error))
    except OSError as error:
        raise SystemBridgeError(f"cannot run {vector[0]}: {error}", details={"executable": vector[0]}) from error
    return HelperResult(
        argv=vector,
        return_code=completed.returncode,
        stdout=completed.stdout[:MAX_HELPER_OUTPUT_BYTES],
        stderr=completed.stderr[:MAX_HELPER_OUTPUT_BYTES],
    )


def service_status(unit: str) -> ServiceStatus:
    """Read one unit's state.

    :param unit: Unit name; validated before use.
    :raises ValidationError: When the unit name is invalid.
    :raises SystemBridgeError: When systemctl is unavailable or fails.
    """
    validate_unit_name(unit)
    argv = ["systemctl", "show", unit, "--no-pager"]
    for prop in _UNIT_PROPERTIES:
        argv.extend(["--property", prop])
    result = run_helper(argv)
    if result.timed_out:
        raise SystemBridgeError(f"systemctl show {unit} timed out", details={"unit": unit})
    if result.return_code != 0:
        if "not found" in result.stderr.lower() or "not-loaded" in result.stderr.lower():
            return ServiceStatus(unit=unit, load_state="not-found")
        raise SystemBridgeError(
            f"systemctl show {unit} exited {result.return_code}",
            details={"unit": unit, "stderr": result.stderr[-512:]},
        )
    properties = _parse_properties(result.stdout)
    return ServiceStatus(
        unit=unit,
        load_state=properties.get("LoadState", "unknown"),
        active_state=properties.get("ActiveState", "unknown"),
        sub_state=properties.get("SubState", ""),
        unit_file_state=properties.get("UnitFileState", ""),
        description=properties.get("Description", ""),
        main_pid=_optional_int(properties.get("MainPID")),
        exec_main_status=_optional_int(properties.get("ExecMainStatus")),
        active_enter_timestamp="" if properties.get("ActiveEnterTimestamp", "").startswith("n/a") else properties.get("ActiveEnterTimestamp", ""),
        restart_count=_optional_int(properties.get("NRestarts")),
        memory_current_bytes=_optional_int(properties.get("MemoryCurrent")),
    )


def list_services(*, state: str = "all", limit: int = 50) -> list[ServiceStatus]:
    """List units filtered by state.

    :param state: ``failed``, ``running``, or ``all``.
    :param limit: Maximum units returned.
    :raises ValidationError: When ``state`` is unknown.
    :raises SystemBridgeError: When systemctl is unavailable or fails.
    """
    if state not in {"failed", "running", "all"}:
        raise ValidationError(f"unsupported service state filter {state!r}")
    if state == "failed":
        argv = ["systemctl", "list-units", "--type=service", "--state=failed", "--no-legend", "--no-pager", "--plain"]
    elif state == "running":
        argv = [
            "systemctl",
            "list-units",
            "--type=service",
            "--state=running",
            "--no-legend",
            "--no-pager",
            "--plain",
        ]
    else:
        argv = [
            "systemctl",
            "list-unit-files",
            "--type=service",
            "--no-legend",
            "--no-pager",
            "--plain",
        ]
    result = run_helper(argv)
    if result.timed_out:
        raise SystemBridgeError(f"systemctl list ({state}) timed out")
    if result.return_code != 0:
        raise SystemBridgeError(
            f"systemctl list ({state}) exited {result.return_code}",
            details={"stderr": result.stderr[-512:]},
        )
    statuses: list[ServiceStatus] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        name = fields[0].lstrip("●").strip()
        if not name.endswith(".service"):
            continue
        if state == "all":
            statuses.append(
                ServiceStatus(
                    unit=name,
                    unit_file_state=fields[1] if len(fields) > 1 else "",
                    load_state="loaded",
                    active_state="unknown",
                    description=" ".join(fields[2:])[:200],
                )
            )
        else:
            statuses.append(
                ServiceStatus(
                    unit=name,
                    load_state=fields[1] if len(fields) > 1 else "",
                    active_state=fields[2] if len(fields) > 2 else "",
                    sub_state=fields[3] if len(fields) > 3 else "",
                    description=" ".join(fields[4:])[:200],
                )
            )
        if len(statuses) >= limit:
            break
    return statuses


def read_journal(
    *,
    unit: str | None = None,
    priority: int | None = None,
    lines: int = 50,
    since: str | None = None,
) -> list[JournalEntry]:
    """Read bounded journald records.

    :param unit: Restrict to one unit; validated before use.
    :param priority: Maximum syslog level, 0 to 7.
    :param lines: Maximum records returned.
    :param since: ``journalctl --since`` expression.
    :raises ValidationError: On an invalid unit, priority, or ``since`` value.
    :raises SystemBridgeError: When journalctl is unavailable or fails.
    """
    if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= 500:
        raise ValidationError("journal line count must be between 1 and 500")
    argv = ["journalctl", "--no-pager", "--output=json", "--utc", f"--lines={lines}"]
    if unit is not None:
        validate_unit_name(unit)
        argv.extend(["--unit", unit])
    if priority is not None:
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 7:
            raise ValidationError("journal priority must be an integer between 0 and 7")
        argv.append(f"--priority={priority}")
    if since is not None:
        if not isinstance(since, str) or len(since) > 64 or any(character in since for character in ";&|`$><\n"):
            raise ValidationError("journal 'since' expression contains unsupported characters")
        argv.extend(["--since", since])
    result = run_helper(argv)
    if result.timed_out:
        raise SystemBridgeError("journalctl timed out")
    if result.return_code != 0:
        raise SystemBridgeError(
            f"journalctl exited {result.return_code}",
            details={"stderr": result.stderr[-512:]},
        )
    entries: list[JournalEntry] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        entries.append(
            JournalEntry(
                timestamp=str(record.get("__REALTIME_TIMESTAMP", "")),
                priority=_optional_int(record.get("PRIORITY")) or 6,
                unit=str(record.get("_SYSTEMD_UNIT") or record.get("UNIT") or ""),
                identifier=str(record.get("SYSLOG_IDENTIFIER") or record.get("_COMM") or ""),
                message=_journal_message(record),
                pid=_optional_int(record.get("_PID")),
            )
        )
    return entries


def _journal_message(record: dict[str, Any]) -> str:
    """Extract a printable message from a journald JSON record.

    ``MESSAGE`` is a byte array when it contains non-UTF-8 data; such a record is
    summarized rather than decoded, because a lossy decode of arbitrary binary is
    exactly the kind of data AgentOS must not present as text.
    """
    message = record.get("MESSAGE")
    if isinstance(message, str):
        return message[:4096]
    if isinstance(message, list):
        try:
            return bytes(int(item) for item in message).decode("utf-8", errors="replace")[:4096]
        except (TypeError, ValueError):
            return "<binary journal message>"
    return ""


def restart_unit(unit: str, *, timeout_seconds: float = 60.0) -> HelperResult:
    """Restart one unit.

    The caller is responsible for having obtained an approval: this function
    performs the action and reports the result, and the executor is what binds
    approval, target, and parameters together.

    :param unit: Unit to restart; validated before use.
    :param timeout_seconds: Deadline for the restart.
    :raises ValidationError: When the unit name is invalid.
    """
    validate_unit_name(unit)
    return run_helper(["systemctl", "restart", unit, "--no-pager"], timeout_seconds=timeout_seconds)


def _parse_properties(output: str) -> dict[str, str]:
    """Parse ``systemctl show`` key=value output."""
    properties: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key.strip()] = value.strip()
    return properties


def _optional_int(value: Any) -> int | None:
    """Return an integer, tolerating systemd's ``[not set]`` markers."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("[") or text in {"n/a", "infinity"}:
        return None
    try:
        return int(text)
    except ValueError:
        return None


__all__ = [
    "DEFAULT_HELPER_TIMEOUT_SECONDS",
    "MAX_HELPER_OUTPUT_BYTES",
    "HelperResult",
    "JournalEntry",
    "ServiceStatus",
    "list_services",
    "read_journal",
    "restart_unit",
    "run_helper",
    "service_status",
    "validate_unit_name",
]
