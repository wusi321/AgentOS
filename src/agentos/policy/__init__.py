"""Policy subsystem: capability manifests, rules, approvals, and enforcement.

The engine is the only module another package needs to import for an
authorization decision. Approvals and rules are exported because Core, the CLI,
and the desktop application all present them to a human.
"""

from __future__ import annotations

from .approvals import (
    APPROVAL_STATES,
    DEFAULT_APPROVAL_TTL_SECONDS,
    MAX_APPROVAL_TTL_SECONDS,
    Approval,
    ApprovalRequest,
)
from .capabilities import (
    BUILTIN_MANIFESTS,
    PrivilegeLevel,
    ToolManifest,
    builtin_manifest_index,
    find_manifest,
)
from .decision import Decision
from .engine import (
    POLICY_DIRECTORY,
    ExecutionAuthorization,
    PolicyContext,
    PolicyEngine,
    PolicyEvaluation,
    load_policy_directory,
)
from .rules import RULE_ACTIONS, PolicyDocument, PolicyRule, parse_policy

__all__ = [
    "APPROVAL_STATES",
    "BUILTIN_MANIFESTS",
    "DEFAULT_APPROVAL_TTL_SECONDS",
    "MAX_APPROVAL_TTL_SECONDS",
    "POLICY_DIRECTORY",
    "RULE_ACTIONS",
    "Approval",
    "ApprovalRequest",
    "Decision",
    "ExecutionAuthorization",
    "PolicyContext",
    "PolicyDocument",
    "PolicyEngine",
    "PolicyEvaluation",
    "PolicyRule",
    "PrivilegeLevel",
    "ToolManifest",
    "builtin_manifest_index",
    "find_manifest",
    "load_policy_directory",
    "parse_policy",
]
