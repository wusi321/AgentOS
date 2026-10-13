"""Agent Runtime abstraction: protocol, events, capabilities, and adapters.

The package root exports the protocol surface, the adapter factory, and the
mock runtime that tests and offline installs rely on, so that Core code cannot
accidentally depend on one runtime's private details.
"""

from __future__ import annotations

from .api import (
    RUNTIME_EVENT_TYPES,
    AgentRuntime,
    RuntimeCapabilities,
    RuntimeEvent,
    RuntimeHealth,
    RuntimeRequest,
)
from .factory import RUNTIME_IDS, RuntimeSettings, create_runtime, list_runtimes
from .mock import MockRuntime, MockScenario

__all__ = [
    "RUNTIME_EVENT_TYPES",
    "RUNTIME_IDS",
    "AgentRuntime",
    "MockRuntime",
    "MockScenario",
    "RuntimeCapabilities",
    "RuntimeEvent",
    "RuntimeHealth",
    "RuntimeRequest",
    "RuntimeSettings",
    "create_runtime",
    "list_runtimes",
]
