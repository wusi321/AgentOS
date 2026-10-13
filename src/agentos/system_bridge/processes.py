"""Process inventory from ``/proc``.

Reading ``/proc`` directly avoids a runtime dependency on ``psutil`` while
keeping the collector honest: every field reported here is one the kernel
exposed, and nothing is inferred from a helper process's formatted output.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, replace
from collections.abc import Mapping
from typing import Any, Final

from ..errors import SystemBridgeError
from .metrics import CLOCK_TICKS, PAGE_SIZE

#: Process states reported by ``/proc/<pid>/stat``, with plain-language names.
PROCESS_STATE_NAMES: Final[Mapping[str, str]] = {
    "R": "running",
    "S": "sleeping",
    "D": "uninterruptible",
    "Z": "zombie",
    "T": "stopped",
    "t": "tracing-stop",
    "X": "dead",
    "I": "idle",
    "P": "parked",
}

#: Processes whose command line is never reported, as a defence against leaking
#: credential material from a short-lived privileged helper.
_NEVER_REPORTED_NAMES: Final[frozenset[str]] = frozenset({"agentos-core", "dsh"})


@dataclass(frozen=True, slots=True)
class ProcessSample:
    """One process's resource use and identity.

    :param pid: Process id.
    :param name: Executable name from ``/proc/<pid>/stat``.
    :param command_line: Full command line, bounded and never including a raw environment.
    :param executable: Resolved executable path, when readable.
    :param uid: Owning user id.
    :param state: Plain-language process state.
    :param parent_pid: Parent process id.
    :param thread_count: Number of threads.
    :param rss_kib: Resident set size.
    :param virtual_kib: Virtual memory size.
    :param cpu_percent: CPU share since the previous sample, when one exists.
    :param cpu_seconds: Cumulative CPU seconds.
    :param started_epoch: Process start time as a Unix epoch, when derivable.
    """

    pid: int
    name: str
    command_line: str = ""
    executable: str = ""
    uid: int = -1
    state: str = ""
    parent_pid: int = 0
    thread_count: int = 1
    rss_kib: int = 0
    virtual_kib: int = 0
    cpu_percent: float | None = None
    cpu_seconds: float = 0.0
    started_epoch: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable description of this process."""
        return {
            "pid": self.pid,
            "name": self.name,
            "command_line": self.command_line,
            "executable": self.executable,
            "uid": self.uid,
            "state": self.state,
            "parent_pid": self.parent_pid,
            "thread_count": self.thread_count,
            "rss_mib": round(self.rss_kib / 1024, 2),
            "virtual_mib": round(self.virtual_kib / 1024, 2),
            "cpu_percent": None if self.cpu_percent is None else round(self.cpu_percent, 2),
            "cpu_seconds": round(self.cpu_seconds, 3),
            "started_epoch": self.started_epoch,
        }


def _read(path: str) -> str | None:
    """Read a pseudo-file, returning ``None`` when the process vanished."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    except OSError:
        return None


def _read_bytes(path: str) -> bytes | None:
    """Read a pseudo-file as bytes; command lines are NUL-separated bytes."""
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    except OSError:
        return None


def _parse_stat(content: str) -> tuple[str, list[str]] | None:
    """Split ``/proc/<pid>/stat`` into the executable name and remaining fields.

    The executable name is wrapped in parentheses and may itself contain spaces or
    parentheses, so the split happens at the last ``)`` rather than by whitespace.
    """
    open_index = content.find("(")
    close_index = content.rfind(")")
    if open_index < 0 or close_index <= open_index:
        return None
    name = content[open_index + 1 : close_index]
    remainder = content[close_index + 2 :].split()
    if len(remainder) < 22:
        return None
    return name, remainder


#: Index of each field in the post-``comm`` remainder of ``/proc/<pid>/stat``.
_FIELD_STATE = 0
_FIELD_PARENT_PID = 1
_FIELD_UTIME = 11
_FIELD_STIME = 12
_FIELD_NUM_THREADS = 17
_FIELD_STARTTIME = 19
_FIELD_VSIZE = 20
_FIELD_RSS = 21


def read_process(pid: int, *, previous_cpu: Mapping[int, float] | None = None) -> ProcessSample | None:
    """Read one process, or ``None`` when it no longer exists.

    :param pid: Process id to read.
    :param previous_cpu: Mapping of pid to cumulative CPU seconds from an earlier
        sample, used to compute ``cpu_percent``.
    """
    stat_content = _read(f"/proc/{pid}/stat")
    if stat_content is None:
        return None
    parsed = _parse_stat(stat_content)
    if parsed is None:
        return None
    name, fields = parsed
    uid = -1
    status_content = _read(f"/proc/{pid}/status")
    if status_content is not None:
        for line in status_content.splitlines():
            if line.startswith("Uid:"):
                parts = line.split()
                if len(parts) > 1:
                    try:
                        uid = int(parts[1])
                    except ValueError:
                        uid = -1
                break
    executable = ""
    try:
        executable = os.readlink(f"/proc/{pid}/exe")
    except (OSError, ValueError):
        executable = ""
    command_line = ""
    if name not in _NEVER_REPORTED_NAMES:
        raw = _read_bytes(f"/proc/{pid}/cmdline") or b""
        command_line = " ".join(
            part.decode("utf-8", errors="replace") for part in raw.split(b"\x00") if part
        )[:1024]
    cpu_seconds = (int(fields[_FIELD_UTIME]) + int(fields[_FIELD_STIME])) / CLOCK_TICKS
    cpu_percent: float | None = None
    if previous_cpu is not None and pid in previous_cpu:
        cpu_percent = None  # filled by the caller, which owns the wall-clock delta
    started_epoch = _start_time_epoch(fields[_FIELD_STARTTIME])
    return ProcessSample(
        pid=pid,
        name=name,
        command_line=command_line,
        executable=executable,
        uid=uid,
        state=PROCESS_STATE_NAMES.get(fields[_FIELD_STATE], fields[_FIELD_STATE]),
        parent_pid=int(fields[_FIELD_PARENT_PID]),
        thread_count=int(fields[_FIELD_NUM_THREADS]),
        rss_kib=int(fields[_FIELD_RSS]) * PAGE_SIZE // 1024,
        virtual_kib=int(fields[_FIELD_VSIZE]) // 1024,
        cpu_percent=cpu_percent,
        cpu_seconds=cpu_seconds,
        started_epoch=started_epoch,
    )


def _start_time_epoch(starttime_ticks: str) -> float | None:
    """Convert a ``starttime`` jiffy count into a Unix epoch second."""
    try:
        ticks = int(starttime_ticks)
    except ValueError:
        return None
    uptime = None
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            uptime = float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    boot_time = time.time() - uptime
    return round(boot_time + ticks / CLOCK_TICKS, 3)


def list_processes(
    *,
    limit: int = 25,
    sort_by: str = "cpu",
    min_cpu_percent: float = 0.0,
    previous_cpu: Mapping[int, float] | None = None,
    elapsed_seconds: float | None = None,
) -> list[ProcessSample]:
    """Return processes ordered by the requested key.

    :param limit: Maximum rows returned, capped at 200 by the tool schema.
    :param sort_by: ``cpu``, ``memory``, or ``pid``.
    :param min_cpu_percent: Skip processes below this CPU share.
    :param previous_cpu: Mapping of pid to cumulative CPU seconds from an earlier sample.
    :param elapsed_seconds: Wall-clock seconds between the two samples.
    :raises SystemBridgeError: When no process could be read at all.
    """
    if sort_by not in {"cpu", "memory", "pid"}:
        raise SystemBridgeError(f"unsupported sort key {sort_by!r}")
    try:
        entries = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError as error:
        raise SystemBridgeError(f"cannot list /proc: {error}") from error
    samples: list[ProcessSample] = []
    for pid in entries:
        sample = read_process(pid, previous_cpu=previous_cpu)
        if sample is None:
            continue
        if previous_cpu is not None and elapsed_seconds and elapsed_seconds > 0 and pid in previous_cpu:
            delta = max(0.0, sample.cpu_seconds - previous_cpu[pid])
            percent = 100.0 * delta / elapsed_seconds
            sample = replace(sample, cpu_percent=percent)
        samples.append(sample)
    if min_cpu_percent > 0:
        samples = [sample for sample in samples if (sample.cpu_percent or 0.0) >= min_cpu_percent]
    if sort_by == "cpu":
        samples.sort(key=lambda sample: (sample.cpu_percent or 0.0, sample.cpu_seconds), reverse=True)
    elif sort_by == "memory":
        samples.sort(key=lambda sample: sample.rss_kib, reverse=True)
    else:
        samples.sort(key=lambda sample: sample.pid)
    return samples[:limit]


def cpu_counter_snapshot(processes: list[ProcessSample]) -> dict[int, float]:
    """Return a pid-to-cumulative-CPU-seconds mapping usable as a delta baseline."""
    return {sample.pid: sample.cpu_seconds for sample in processes}


def find_process(pid: int) -> ProcessSample | None:
    """Return one process or ``None`` when it does not exist."""
    return read_process(pid)


__all__ = [
    "PROCESS_STATE_NAMES",
    "ProcessSample",
    "cpu_counter_snapshot",
    "find_process",
    "list_processes",
    "read_process",
]
