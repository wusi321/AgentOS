"""Desktop subsystem: the GTK4/libadwaita AgentOS application.

Importing this package never imports GTK. The application is built inside
:func:`main` after :func:`require_gtk` succeeds, so a machine without GTK4 or
libadwaita still gets a clear diagnostic instead of an import traceback.
"""

from __future__ import annotations

from .app import (
    APPLICATION_ID,
    STATUS_INTERVAL_SECONDS,
    DesktopApplication,
    MissingDependency,
    main,
    require_gtk,
    status_label,
)
from .ipc_async import AsyncIpc

__all__ = [
    "APPLICATION_ID",
    "STATUS_INTERVAL_SECONDS",
    "AsyncIpc",
    "DesktopApplication",
    "MissingDependency",
    "main",
    "require_gtk",
    "status_label",
]
