"""CPU, memory, swap, disk, and uptime collection from ``/proc`` and ``statvfs``.

Everything here is read-only. No collector writes a file, signals a process, or
calls a command that can change system state, which is why P0 tools can be
exposed to a model without an approval gate.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Final

from ..errors import SystemBridgeError

#: Kernel clock ticks per second, used to convert ``/proc/<pid>/stat`` jiffies.
CLOCK_TICKS: Final = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
#: Page size in bytes, used to convert ``/proc/<pid>/statm`` pages.
PAGE_SIZE: Final = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


@dataclass(frozen=True, slots=True)
class CpuSample:
    """One CPU utilisation reading.

    :param total_seconds: Cumulative total CPU seconds across all cores.
    :param idle_seconds: Cumulative idle CPU seconds across all cores.
    :param core_count: Number of logical cores.
    :param load_average: One-minute load average.
    :param utilised_percent: Utilisation since the previous sample, when one exists.
    :param per_core: Utilisation percentage per logical core, when a previous sample exists.
    :param core_total_seconds: Cumulative per-core total seconds; the delta baseline.
    :param core_idle_seconds: Cumulative per-core idle seconds; the delta baseline.
    """

    total_seconds: float
    idle_seconds: float
    core_count: int
    load_average: float
    utilised_percent: float | None = None
    per_core: tuple[float, ...] = field(default=())
    core_total_seconds: tuple[float, ...] = field(default=())
    core_idle_seconds: tuple[float, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable description of this sample."""
        return {
            "core_count": self.core_count,
            "load_average_1m": round(self.load_average, 3),
            "utilised_percent": None if self.utilised_percent is None else round(self.utilised_percent, 2),
            "per_core_percent": [round(value, 2) for value in self.per_core],
            "total_cpu_seconds": round(self.total_seconds, 3),
            "idle_cpu_seconds": round(self.idle_seconds, 3),
        }


@dataclass(frozen=True, slots=True)
class MemorySample:
    """Memory and swap utilisation.

    :param total_kib: Total physical memory.
    :param available_kib: Memory available for new work without swapping.
    :param used_kib: Total minus available.
    :param cached_kib: Page cache and reclaimable slabs.
    :param swap_total_kib: Total swap.
    :param swap_used_kib: Swap in use.
    """

    total_kib: int
    available_kib: int
    used_kib: int
    cached_kib: int
    swap_total_kib: int
    swap_used_kib: int

    @property
    def used_percent(self) -> float:
        """Return the used share of physical memory."""
        return 0.0 if self.total_kib <= 0 else 100.0 * self.used_kib / self.total_kib

    @property
    def swap_used_percent(self) -> float:
        """Return the used share of swap."""
        return 0.0 if self.swap_total_kib <= 0 else 100.0 * self.swap_used_kib / self.swap_total_kib

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable description of this sample."""
        return {
            "total_mib": round(self.total_kib / 1024, 1),
            "used_mib": round(self.used_kib / 1024, 1),
            "available_mib": round(self.available_kib / 1024, 1),
            "cached_mib": round(self.cached_kib / 1024, 1),
            "used_percent": round(self.used_percent, 2),
            "swap_total_mib": round(self.swap_total_kib / 1024, 1),
            "swap_used_mib": round(self.swap_used_kib / 1024, 1),
            "swap_used_percent": round(self.swap_used_percent, 2),
        }


@dataclass(frozen=True, slots=True)
class FilesystemSample:
    """Capacity and inode use for one mounted filesystem.

    :param mount_point: Where the filesystem is mounted.
    :param device: Source device or pseudo-device.
    :param filesystem_type: Filesystem type as reported by ``/proc/mounts``.
    :param total_bytes: Total capacity.
    :param used_bytes: Bytes in use.
    :param free_bytes: Bytes free to an unprivileged user.
    :param total_inodes: Total inode count, or ``0`` when the filesystem has none.
    :param free_inodes: Free inode count.
    """

    mount_point: str
    device: str
    filesystem_type: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    total_inodes: int = 0
    free_inodes: int = 0

    @property
    def used_percent(self) -> float:
        """Return the used share of capacity."""
        return 0.0 if self.total_bytes <= 0 else 100.0 * self.used_bytes / self.total_bytes

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable description of this filesystem."""
        return {
            "mount_point": self.mount_point,
            "device": self.device,
            "filesystem_type": self.filesystem_type,
            "total_gib": round(self.total_bytes / (1024**3), 2),
            "used_gib": round(self.used_bytes / (1024**3), 2),
            "free_gib": round(self.free_bytes / (1024**3), 2),
            "used_percent": round(self.used_percent, 2),
            "total_inodes": self.total_inodes,
            "free_inodes": self.free_inodes,
            "inode_used_percent": (
                0.0
                if self.total_inodes <= 0
                else round(100.0 * (self.total_inodes - self.free_inodes) / self.total_inodes, 2)
            ),
        }


def read_cpu_sample(previous: CpuSample | None = None) -> CpuSample:
    """Read CPU counters and, when ``previous`` is given, compute utilisation.

    :param previous: An earlier sample used as the baseline for a delta.
    :raises SystemBridgeError: When ``/proc/stat`` cannot be read or parsed.
    """
    lines = _read_text("/proc/stat").splitlines()
    total = 0.0
    idle = 0.0
    per_core_totals: list[float] = []
    per_core_idles: list[float] = []
    for line in lines:
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        if parts[0] == "cpu":
            total, idle = _parse_cpu_line(parts[1:])
            continue
        core_total, core_idle = _parse_cpu_line(parts[1:])
        per_core_totals.append(core_total)
        per_core_idles.append(core_idle)
    if not per_core_totals:
        raise SystemBridgeError("no per-core CPU counters found in /proc/stat")
    load = read_load_average()[0]
    utilised: float | None = None
    per_core: tuple[float, ...] = ()
    if previous is not None:
        delta_total = total - previous.total_seconds
        delta_idle = idle - previous.idle_seconds
        if delta_total > 0:
            utilised = _percent(delta_total, delta_idle)
        if (
            len(previous.core_total_seconds) == len(per_core_totals)
            and len(previous.core_idle_seconds) == len(per_core_idles)
        ):
            per_core = tuple(
                _percent(
                    per_core_totals[index] - previous.core_total_seconds[index],
                    per_core_idles[index] - previous.core_idle_seconds[index],
                )
                for index in range(len(per_core_totals))
            )
    return CpuSample(
        total_seconds=total,
        idle_seconds=idle,
        core_count=len(per_core_totals),
        load_average=load,
        utilised_percent=utilised,
        per_core=per_core,
        core_total_seconds=tuple(per_core_totals),
        core_idle_seconds=tuple(per_core_idles),
    )


def _percent(delta_total: float, delta_idle: float) -> float:
    """Return clamped busy percentage for one counter delta."""
    if delta_total <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (delta_total - delta_idle) / delta_total))


def _parse_cpu_line(fields: list[str]) -> tuple[float, float]:
    """Return ``(total, idle)`` seconds from one ``/proc/stat`` cpu row."""
    if len(fields) < 4:
        raise SystemBridgeError("/proc/stat cpu row is truncated")
    ticks = [int(value) for value in fields[: max(4, min(len(fields), 8))]]
    total = sum(ticks) / CLOCK_TICKS
    idle = (ticks[3] + (ticks[4] if len(ticks) > 4 else 0)) / CLOCK_TICKS
    return total, idle


def read_memory_sample() -> MemorySample:
    """Read memory and swap counters from ``/proc/meminfo``.

    :raises SystemBridgeError: When the file cannot be read or lacks a required field.
    """
    values: dict[str, int] = {}
    for line in _read_text("/proc/meminfo").splitlines():
        parts = line.split(":")
        if len(parts) != 2:
            continue
        number = parts[1].strip().split()
        if not number:
            continue
        try:
            values[parts[0].strip()] = int(number[0])
        except ValueError:
            continue
    total = values.get("MemTotal", 0)
    if total <= 0:
        raise SystemBridgeError("/proc/meminfo does not report MemTotal")
    available = values.get("MemAvailable")
    if available is None:
        available = values.get("MemFree", 0) + values.get("Buffers", 0) + values.get("Cached", 0)
    cached = values.get("Cached", 0) + values.get("SReclaimable", 0)
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    return MemorySample(
        total_kib=total,
        available_kib=available,
        used_kib=max(0, total - available),
        cached_kib=cached,
        swap_total_kib=swap_total,
        swap_used_kib=max(0, swap_total - swap_free),
    )


def read_load_average() -> tuple[float, float, float]:
    """Return the 1, 5, and 15 minute load averages.

    :raises SystemBridgeError: When ``/proc/loadavg`` cannot be read or parsed.
    """
    fields = _read_text("/proc/loadavg").split()
    if len(fields) < 3:
        raise SystemBridgeError("/proc/loadavg is truncated")
    try:
        return float(fields[0]), float(fields[1]), float(fields[2])
    except ValueError as error:
        raise SystemBridgeError(f"/proc/loadavg is malformed: {fields[:3]}") from error


def read_uptime() -> tuple[float, float]:
    """Return ``(uptime_seconds, idle_seconds)`` from ``/proc/uptime``."""
    fields = _read_text("/proc/uptime").split()
    if len(fields) < 2:
        raise SystemBridgeError("/proc/uptime is truncated")
    try:
        return float(fields[0]), float(fields[1])
    except ValueError as error:
        raise SystemBridgeError("/proc/uptime is malformed") from error


#: Pseudo-filesystem types excluded from capacity reporting.
_SKIPPED_FILESYSTEMS: Final[frozenset[str]] = frozenset(
    {
        "autofs",
        "bpf",
        "cgroup",
        "cgroup2",
        "configfs",
        "debugfs",
        "devpts",
        "devtmpfs",
        "efivarfs",
        "fuse.gvfsd-fuse",
        "fusectl",
        "hugetlbfs",
        "mqueue",
        "nsfs",
        "overlay",
        "proc",
        "pstore",
        "ramfs",
        "securityfs",
        "squashfs",
        "sysfs",
        "tmpfs",
        "tracefs",
    }
)


def read_filesystems(*, include_pseudo: bool = False) -> list[FilesystemSample]:
    """Report capacity for every real mounted filesystem.

    :param include_pseudo: Include tmpfs and other pseudo-filesystems.
    :raises SystemBridgeError: When ``/proc/mounts`` cannot be read.
    """
    samples: list[FilesystemSample] = []
    seen: set[str] = set()
    for line in _read_text("/proc/mounts").splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        device, mount_point, fs_type = fields[0], _unescape_mount(fields[1]), fields[2]
        if not include_pseudo and (fs_type in _SKIPPED_FILESYSTEMS or device in {"tmpfs", "none"}):
            continue
        if mount_point in seen:
            continue
        seen.add(mount_point)
        try:
            stat = os.statvfs(mount_point)
        except OSError:
            continue
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        samples.append(
            FilesystemSample(
                mount_point=mount_point,
                device=device,
                filesystem_type=fs_type,
                total_bytes=total,
                used_bytes=max(0, total - stat.f_bfree * stat.f_frsize),
                free_bytes=free,
                total_inodes=stat.f_files,
                free_inodes=stat.f_ffree,
            )
        )
    samples.sort(key=lambda sample: sample.mount_point)
    return samples


def _unescape_mount(value: str) -> str:
    """Decode the octal escapes ``/proc/mounts`` uses for spaces and tabs."""
    return value.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def _read_text(path: str) -> str:
    """Read a small pseudo-file, converting every OS failure into a bridge error."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except FileNotFoundError as error:
        raise SystemBridgeError(f"{path} is not available on this system", details={"path": path}) from error
    except OSError as error:
        raise SystemBridgeError(f"cannot read {path}: {error}", details={"path": path}) from error


def monotonic_seconds() -> float:
    """Return the monotonic clock, used to bound sampling intervals."""
    return time.monotonic()


__all__ = [
    "CLOCK_TICKS",
    "PAGE_SIZE",
    "CpuSample",
    "FilesystemSample",
    "MemorySample",
    "monotonic_seconds",
    "read_cpu_sample",
    "read_filesystems",
    "read_load_average",
    "read_memory_sample",
    "read_uptime",
]
