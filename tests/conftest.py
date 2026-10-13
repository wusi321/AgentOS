"""Shared pytest fixtures for the AgentOS test suite.

Tests run against a source checkout: ``src`` is placed on ``sys.path`` so that
``pytest tests`` works without installing the package, and every fixture that
touches the filesystem uses a temporary directory so a test can never write into
``/etc`` or ``/var/lib``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentos.core.config import Config, CoreSettings  # noqa: E402
from agentos.policy import PolicyEngine, parse_policy  # noqa: E402
from agentos.runtime.factory import RuntimeSettings  # noqa: E402
from agentos.tasks import Storage  # noqa: E402
from agentos.yaml_subset import load_yaml_file  # noqa: E402

#: Repository root, exposed so tests can locate shipped configuration.
FIXTURE_REPO_ROOT = REPO_ROOT
#: Path of the shipped read-only default policy.
DEFAULT_POLICY_PATH = REPO_ROOT / "configs" / "policies" / "default-policy.yaml"
#: Path of the example site policy that enables the controlled P2 operations.
SITE_POLICY_PATH = REPO_ROOT / "configs" / "policies-example" / "50-site-policy.yaml"


@pytest.fixture()
def storage(tmp_path: Path) -> Storage:
    """Return a storage instance on a temporary database."""
    store = Storage(str(tmp_path / "state.sqlite3"))
    yield store
    store.close()


@pytest.fixture()
def default_policy_engine() -> PolicyEngine:
    """Return a policy engine loaded from the shipped read-only default policy."""
    document = load_yaml_file(str(DEFAULT_POLICY_PATH))
    policy = parse_policy(document, name="default-read-only", source=str(DEFAULT_POLICY_PATH))
    return PolicyEngine(policies=[policy])


@pytest.fixture()
def site_policy_engine() -> PolicyEngine:
    """Return a policy engine loaded from the example site policy (P2 allowed)."""
    document = load_yaml_file(str(SITE_POLICY_PATH))
    policy = parse_policy(document, name="site-restart-allowlist", source=str(SITE_POLICY_PATH))
    return PolicyEngine(policies=[policy])


@pytest.fixture()
def restrictive_policy_engine() -> PolicyEngine:
    """Return an engine with no policy documents, which fails closed to P0."""
    return PolicyEngine(policies=[])


@pytest.fixture()
def core_config(tmp_path: Path) -> Config:
    """Return a Core configuration rooted entirely in a temporary directory."""
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    return Config(
        core=CoreSettings(
            socket_path=str(tmp_path / "core.sock"),
            database_path=str(state / "state.sqlite3"),
            state_directory=str(state),
            cache_directory=str(tmp_path / "cache"),
            log_directory=str(tmp_path / "log"),
            log_level="WARNING",
            workspace_root=str(state / "workspaces"),
            queue_poll_seconds=0.05,
            max_concurrent_tasks=2,
        ),
        runtime=RuntimeSettings(runtime_id="mock", workspace_root=str(state / "workspaces")),
        policy_directory=str(REPO_ROOT / "configs" / "policies-example"),
    )


@pytest.fixture()
def mock_settings() -> RuntimeSettings:
    """Return runtime settings that select the mock runtime."""
    return RuntimeSettings(
        runtime_id="mock",
        mock_scenario={"replies": ["fixture reply"], "chunk_size": 8},
    )
