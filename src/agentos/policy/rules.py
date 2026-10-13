"""Policy rules: declarative statements of what a deployment permits.

Rules are loaded from ``/etc/agentos/policies/*.yaml``. The shipped default
policy is read-only; every level above it must be enabled deliberately by an
administrator. A tool with no matching rule and no built-in manifest is denied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Iterable, Mapping
from typing import Any, Final

from ..errors import ConfigurationError, ValidationError
from .capabilities import Decision, PrivilegeLevel

#: Rule vocabulary accepted in a policy document.
RULE_ACTIONS: Final[frozenset[str]] = frozenset({"allow", "require_approval", "deny"})

_ACTION_TO_DECISION: Final[Mapping[str, Decision]] = {
    "allow": Decision.ALLOW,
    "require_approval": Decision.REQUIRE_APPROVAL,
    "deny": Decision.DENY,
}


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """One policy statement.

    :param name: Stable rule name, used in audit records and UI explanations.
    :param decision: What the rule says.
    :param tool_pattern: Exact tool name or a ``prefix.*`` pattern.
    :param max_level: Highest privilege level the rule is willing to cover.
    :param requires_approval: Force an approval even when the level does not.
    :param allowed_targets: Closed target set for this rule; empty means the
        capability manifest's own allowlist applies.
    :param description: Operator-facing rationale, shown with a decision.
    :param priority: Higher wins; the shipped default policy uses 0.
    """

    name: str
    decision: Decision
    tool_pattern: str
    max_level: PrivilegeLevel = PrivilegeLevel.ADMINISTRATIVE
    requires_approval: bool = False
    allowed_targets: tuple[str, ...] = ()
    description: str = ""
    priority: int = 0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValidationError("policy rule requires a name")
        if not self.tool_pattern:
            raise ValidationError(f"policy rule {self.name!r} requires a tool pattern")
        if self.tool_pattern.endswith(".*"):
            if len(self.tool_pattern) <= 2:
                raise ValidationError(f"policy rule {self.name!r} has an empty tool prefix")
        elif not all(segment.isalnum() for segment in self.tool_pattern.split(".") if segment):
            raise ValidationError(
                f"policy rule {self.name!r} has an invalid tool pattern; "
                "use an exact dotted tool name or a trailing '.*' prefix pattern",
                details={"rule": self.name, "tool_pattern": self.tool_pattern},
            )
        if self.decision is Decision.ALLOW and self.max_level >= PrivilegeLevel.ADMINISTRATIVE:
            raise ValidationError(
                f"policy rule {self.name!r} would allow P3 administrative tools; "
                "list explicit tool names instead",
                details={"rule": self.name},
            )

    def specificity(self) -> int:
        """Return how specific this rule is; exact names beat prefix patterns."""
        base = 1000 if not self.tool_pattern.endswith(".*") else 500
        return base + len(self.tool_pattern)

    def matches(self, tool_name: str) -> bool:
        """Return whether this rule covers ``tool_name``."""
        if self.tool_pattern.endswith(".*"):
            return tool_name.startswith(self.tool_pattern[:-1])
        return tool_name == self.tool_pattern

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable description of this rule."""
        return {
            "name": self.name,
            "decision": self.decision.value,
            "tool_pattern": self.tool_pattern,
            "max_level": int(self.max_level),
            "requires_approval": self.requires_approval,
            "allowed_targets": list(self.allowed_targets),
            "description": self.description,
            "priority": self.priority,
        }


@dataclass(frozen=True, slots=True)
class PolicyDocument:
    """A loaded policy: its rules plus deployment-wide bounds.

    :param name: Document name, normally the file stem.
    :param rules: Rules in declaration order.
    :param max_level: Hard ceiling no rule may exceed.
    :param allowlisted_service_units: Units ``system.service.restart`` may target.
    :param minimum_approval_ttl_seconds: Lower bound applied to requested TTLs.
    :param maximum_approval_ttl_seconds: Upper bound applied to requested TTLs.
    :param source: Path the document was loaded from, for diagnostics.
    """

    name: str
    rules: tuple[PolicyRule, ...] = ()
    max_level: PrivilegeLevel = PrivilegeLevel.CONTROLLED
    allowlisted_service_units: tuple[str, ...] = ()
    minimum_approval_ttl_seconds: int = 30
    maximum_approval_ttl_seconds: int = 900
    source: str = ""

    def matching_rules(self, tool_name: str) -> list[PolicyRule]:
        """Return every rule covering ``tool_name``, most authoritative first."""
        matched = [rule for rule in self.rules if rule.matches(tool_name)]
        matched.sort(key=lambda rule: (rule.priority, rule.specificity()), reverse=True)
        return matched

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable description of this policy."""
        return {
            "name": self.name,
            "source": self.source,
            "max_level": int(self.max_level),
            "max_level_label": self.max_level.label,
            "allowlisted_service_units": list(self.allowlisted_service_units),
            "minimum_approval_ttl_seconds": self.minimum_approval_ttl_seconds,
            "maximum_approval_ttl_seconds": self.maximum_approval_ttl_seconds,
            "rules": [rule.to_dict() for rule in self.rules],
        }


def _require_mapping(value: Any, *, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{context} must be a table")
    return value


def parse_policy(document: Mapping[str, Any], *, name: str, source: str = "") -> PolicyDocument:
    """Build a :class:`PolicyDocument` from a decoded YAML/JSON mapping.

    :param document: Decoded policy document.
    :param name: Document name used in diagnostics.
    :param source: Path the document came from.
    :raises ConfigurationError: On an unknown key or invalid value.
    """
    _require_mapping(document, context=f"policy {name}")
    known = {
        "name",
        "max_level",
        "allowlisted_service_units",
        "minimum_approval_ttl_seconds",
        "maximum_approval_ttl_seconds",
        "rules",
    }
    unknown = sorted(set(str(key) for key in document) - known)
    if unknown:
        raise ConfigurationError(
            f"policy {name} has unknown key(s): " + ", ".join(unknown),
            details={"unknown": unknown, "known": sorted(known)},
        )
    max_level = _parse_level(document.get("max_level", "P2"), context=f"policy {name}.max_level")
    units = document.get("allowlisted_service_units", [])
    if not isinstance(units, list) or not all(isinstance(item, str) for item in units):
        raise ConfigurationError(f"policy {name}.allowlisted_service_units must be a list of strings")
    minimum_ttl = document.get("minimum_approval_ttl_seconds", 30)
    maximum_ttl = document.get("maximum_approval_ttl_seconds", 900)
    for label, value in (("minimum", minimum_ttl), ("maximum", maximum_ttl)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigurationError(f"policy {name}.{label}_approval_ttl_seconds must be a positive integer")
    if minimum_ttl > maximum_ttl:
        raise ConfigurationError(f"policy {name}: minimum approval TTL exceeds the maximum")
    raw_rules = document.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ConfigurationError(f"policy {name}.rules must be a list")
    rules: list[PolicyRule] = []
    for index, raw_rule in enumerate(raw_rules):
        rules.append(_parse_rule(raw_rule, index=index, policy_name=name))
    return PolicyDocument(
        name=str(document.get("name", name)),
        rules=tuple(rules),
        max_level=max_level,
        allowlisted_service_units=tuple(units),
        minimum_approval_ttl_seconds=int(minimum_ttl),
        maximum_approval_ttl_seconds=int(maximum_ttl),
        source=source,
    )


def _parse_level(value: Any, *, context: str) -> PrivilegeLevel:
    """Parse ``P0``..``P3`` or an integer level."""
    if isinstance(value, bool):
        raise ConfigurationError(f"{context} must be P0..P3 or an integer")
    if isinstance(value, int):
        try:
            return PrivilegeLevel(value)
        except ValueError as error:
            raise ConfigurationError(f"{context} must be between 0 and 3") from error
    if isinstance(value, str):
        text = value.strip().upper()
        table = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        if text in table:
            return PrivilegeLevel(table[text])
        if text.isdigit() and int(text) in range(4):
            return PrivilegeLevel(int(text))
    raise ConfigurationError(f"{context} must be P0..P3 or an integer 0..3")


def _parse_rule(raw: Any, *, index: int, policy_name: str) -> PolicyRule:
    """Parse one rule entry."""
    context = f"policy {policy_name}.rules[{index}]"
    mapping = _require_mapping(raw, context=context)
    known = {"name", "action", "tool", "max_level", "requires_approval", "allowed_targets", "description", "priority"}
    unknown = sorted(set(str(key) for key in mapping) - known)
    if unknown:
        raise ConfigurationError(f"{context} has unknown key(s): " + ", ".join(unknown))
    action = mapping.get("action")
    if not isinstance(action, str) or action not in RULE_ACTIONS:
        raise ConfigurationError(f"{context}.action must be one of: " + ", ".join(sorted(RULE_ACTIONS)))
    tool = mapping.get("tool")
    if not isinstance(tool, str) or not tool:
        raise ConfigurationError(f"{context}.tool must be a non-empty tool name or prefix pattern")
    targets = mapping.get("allowed_targets", [])
    if not isinstance(targets, list) or not all(isinstance(item, str) for item in targets):
        raise ConfigurationError(f"{context}.allowed_targets must be a list of strings")
    priority = mapping.get("priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ConfigurationError(f"{context}.priority must be an integer")
    return PolicyRule(
        name=str(mapping.get("name") or f"{policy_name}-rule-{index}"),
        decision=_ACTION_TO_DECISION[action],
        tool_pattern=tool,
        max_level=_parse_level(mapping.get("max_level", "P3"), context=f"{context}.max_level"),
        requires_approval=bool(mapping.get("requires_approval", False)),
        allowed_targets=tuple(targets),
        description=str(mapping.get("description", "")),
        priority=priority,
    )


def rule_from_mapping(mapping: Mapping[str, Any], *, index: int = 0, policy_name: str = "custom") -> PolicyRule:
    """Parse one rule from a JSON mapping; used by tests and the CLI."""
    return _parse_rule(mapping, index=index, policy_name=policy_name)


def collect_tool_names(policies: Iterable[PolicyDocument]) -> list[str]:
    """Return every distinct tool pattern mentioned by ``policies``."""
    names: set[str] = set()
    for policy in policies:
        for rule in policy.rules:
            names.add(rule.tool_pattern)
    return sorted(names)


__all__ = [
    "RULE_ACTIONS",
    "PolicyDocument",
    "PolicyRule",
    "collect_tool_names",
    "parse_policy",
    "rule_from_mapping",
]
