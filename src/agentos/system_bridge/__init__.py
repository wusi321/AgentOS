"""System Bridge: read-only system facts plus a minimal controlled executor.

The read path (metrics, processes, services, journal) is safe to expose to a
model without an approval gate. The write path is a closed set of two structured
operations, each of which requires an approval bound to its exact target and
parameters.
"""

from __future__ import annotations

from .bridge import SystemBridge, SystemSnapshot
from .executor import EXECUTABLE_OPERATIONS, ControlledExecutor, ExecutionOutcome
from .metrics import (
    CpuSample,
    FilesystemSample,
    MemorySample,
    read_cpu_sample,
    read_filesystems,
    read_load_average,
    read_memory_sample,
    read_uptime,
)
from .processes import ProcessSample, find_process, list_processes
from .services import JournalEntry, ServiceStatus, list_services, read_journal, service_status, validate_unit_name

__all__ = [
    "EXECUTABLE_OPERATIONS",
    "ControlledExecutor",
    "CpuSample",
    "ExecutionOutcome",
    "FilesystemSample",
    "JournalEntry",
    "MemorySample",
    "ProcessSample",
    "ServiceStatus",
    "SystemBridge",
    "SystemSnapshot",
    "find_process",
    "list_processes",
    "list_services",
    "read_cpu_sample",
    "read_filesystems",
    "read_journal",
    "read_load_average",
    "read_memory_sample",
    "read_uptime",
    "service_status",
    "validate_unit_name",
]
