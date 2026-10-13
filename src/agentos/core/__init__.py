"""AgentOS Core: configuration, IPC transport, and the service host.

``CoreHost`` is the object the systemd unit runs. ``main`` is the process entry
point.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from .config import (
    DEFAULT_CONFIG_DOCUMENT,
    Config,
    ConfigurationLoader,
    CoreSettings,
    DesktopSettings,
    load_config,
    read_credential_environment,
    render_config,
    write_config_atomically,
)
from .host import CoreHost, configure_logging, load_effective_config, runtime_catalog
from .ipc import IpcRequest, IpcServer, PeerCredentials, new_request_id
from .ipc_client import IpcClient, IpcClientError


def main(argv: list[str] | None = None) -> int:
    """Run the Core service in the foreground.

    The service is designed to be supervised: it logs to stderr, exits non-zero
    only when it cannot start at all, and stops cleanly on SIGTERM.
    """
    from ..errors import AgentOSError

    try:
        config = load_effective_config()
    except AgentOSError as error:
        configure_logging("ERROR").error("cannot load configuration: %s", error.message)
        return 2
    logger = configure_logging(config.core.log_level)
    try:
        host = CoreHost(config, logger=logger)
    except AgentOSError as error:
        logger.error("cannot start agentos-core: %s", error.message)
        return 3
    try:
        asyncio.run(host.run_forever())
    except KeyboardInterrupt:
        return 0
    except AgentOSError as error:
        logger.error("agentos-core failed: %s", error.message)
        return 4
    return 0


__all__ = [
    "DEFAULT_CONFIG_DOCUMENT",
    "Config",
    "ConfigurationLoader",
    "CoreHost",
    "CoreSettings",
    "DesktopSettings",
    "IpcClient",
    "IpcClientError",
    "IpcRequest",
    "IpcServer",
    "PeerCredentials",
    "configure_logging",
    "load_config",
    "load_effective_config",
    "main",
    "new_request_id",
    "read_credential_environment",
    "render_config",
    "runtime_catalog",
    "write_config_atomically",
]
