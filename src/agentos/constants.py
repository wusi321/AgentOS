"""AgentOS wide constants: identity, versions, paths, and protocol numbers.

Every other module reads identity and path facts from here so that packaging,
services, CLI, and tests cannot drift apart.
"""

from __future__ import annotations

from typing import Final

# --- Project identity -------------------------------------------------------

PROJECT_NAME: Final = "AgentOS"
PROJECT_SLUG: Final = "agentos"
PROJECT_TAGLINE: Final = "An Agent-Native Operating Environment for Linux"
PROJECT_VERSION: Final = "0.1.0"

#: Version of the AgentOS event envelope. Bump only with a reader-compatibility note.
EVENT_SCHEMA_VERSION: Final = 1
#: Version of the Core IPC protocol.
IPC_PROTOCOL_VERSION: Final = 1
#: Version of the persisted SQLite schema. Monotonic; migrations never rewrite in place.
STORAGE_SCHEMA_VERSION: Final = 1
#: Version of the tool capability manifest format shipped by the policy package.
CAPABILITY_MANIFEST_VERSION: Final = 1

#: DSH revision this adapter was verified against. Packaging and the adapter both read this.
DSH_LOCKED_VERSION: Final = "0.2.1-alpha.2"
DSH_LOCKED_COMMIT: Final = "d743267388641bc76f17c45ce8b4c231aed1d32c"
#: Node engines range declared by the locked DSH revision.
DSH_NODE_ENGINES: Final = "^22.19.0 || >=24.0.0"

#: First Ubuntu release in the support baseline.
TARGET_UBUNTU_MIN: Final = "22.04"

# --- Filesystem paths -------------------------------------------------------

#: Core service program files.
PROGRAM_LIB_DIR: Final = "/usr/lib/agentos"
PROGRAM_LIBEXEC_DIR: Final = "/usr/libexec/agentos"

#: Administrator-owned system configuration.
SYSTEM_CONFIG_DIR: Final = "/etc/agentos"
SYSTEM_CONFIG_FILE: Final = SYSTEM_CONFIG_DIR + "/agentos.toml"
SYSTEM_CONFIG_LOCAL_FILE: Final = SYSTEM_CONFIG_DIR + "/agentos.local.toml"

#: Service-owned state.
SYSTEM_STATE_DIR: Final = "/var/lib/agentos"
SYSTEM_STATE_DB: Final = SYSTEM_STATE_DIR + "/agentos.sqlite3"
SYSTEM_CACHE_DIR: Final = "/var/cache/agentos"
SYSTEM_LOG_DIR: Final = "/var/log/agentos"

#: Runtime TCP-free IPC endpoints.
SYSTEM_RUN_DIR: Final = "/run/agentos"
SYSTEM_SOCKET: Final = SYSTEM_RUN_DIR + "/core.sock"

#: DSH runtime home owned by the service account.
DSH_HOME_DIR: Final = SYSTEM_STATE_DIR + "/dsh"
#: Node runtime shipped by the agentos-runtime-dsh package.
DSH_NODE_BINARY: Final = PROGRAM_LIB_DIR + "/node/bin/node"
#: dsh CLI entry point shipped by the agentos-runtime-dsh package.
DSH_CLI_ENTRY: Final = PROGRAM_LIB_DIR + "/dsh/bin/dsh.js"

#: GNOME Shell extension UUID. Stable: changing it orphans enabled state.
GNOME_EXTENSION_UUID: Final = "agentos@agentos.dev"
GNOME_EXTENSION_DIR: Final = "/usr/share/gnome-shell/extensions/" + GNOME_EXTENSION_UUID

#: Human-readable descriptions reused by packaging metadata.
SHORT_DESCRIPTION: Final = "Agent-native operating environment for Linux"

#: Service and session identities.
CORE_SYSTEM_USER: Final = "agentos"
CORE_SYSTEM_GROUP: Final = "agentos"
CORE_SESSION_SERVICE_NAME: Final = "org.agentos.Session1"
CORE_SESSION_OBJECT_PATH: Final = "/org/agentos/Session1"
