"""Policy engine: the single authority for whether an operation may run.

Enforcement happens in :meth:`PolicyEngine.evaluate` and again, immediately
before execution, in :meth:`PolicyEngine.authorize_execution`. The second check
is what makes approvals safe: it re-reads the approval record, re-derives the
parameter digest, and re-applies the rules, so a decision made minutes earlier
cannot authorize a different request now.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from datetime import timedelta
from collections.abc import Iterable, Mapping
from typing import Any, Final

from ..errors import ApprovalInvalid, ApprovalRequired, ConfigurationError, PolicyDenied, ValidationError
from ..events import format_timestamp, utc_now
from ..toml import load_toml_file
from .approvals import MAX_APPROVAL_TTL_SECONDS, Approval, ApprovalRequest
from .capabilities import PrivilegeLevel, ToolManifest, builtin_manifest_index, find_manifest
from .decision import Decision
from .rules import PolicyDocument, parse_policy

#: Directory scanned for policy documents, in lexical order.
POLICY_DIRECTORY: Final = "/etc/agentos/policies"
#: Maximum policy documents loaded from the directory, a bound against directory abuse.
MAX_POLICY_DOCUMENTS: Final = 16


@dataclass(frozen=True, slots=True)
class PolicyContext:
    """Facts about the caller that a decision may depend on.

    :param uid: Local user id that initiated the task.
    :param user_name: Display name, when known.
    :param task_level_cap: Highest level this task may ever reach.
    :param approved_units: Additional service units this deployment permits restarting.
    :param task_id: Task the operation belongs to, when one is known.
    """

    uid: int
    user_name: str = ""
    task_level_cap: PrivilegeLevel = PrivilegeLevel.READ_ONLY
    approved_units: tuple[str, ...] = ()
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyEvaluation:
    """The result of evaluating one proposed tool call.

    :param decision: What policy says.
    :param tool: Tool name evaluated.
    :param level: Privilege level the tool requires.
    :param target: Action target, when the tool declares one.
    :param reason: Operator-facing explanation, always populated.
    :param rule_name: Rule that decided the outcome, when a rule applied.
    :param requires_approval: Whether an approval gate applies.
    :param parameters: The normalized, validated parameters.
    """

    decision: Decision
    tool: str
    level: PrivilegeLevel
    target: str | None
    reason: str
    rule_name: str | None = None
    requires_approval: bool = False
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable description of this evaluation."""
        return {
            "decision": self.decision.value,
            "tool": self.tool,
            "level": int(self.level),
            "level_label": self.level.label,
            "target": self.target,
            "reason": self.reason,
            "rule_name": self.rule_name,
            "requires_approval": self.requires_approval,
        }


@dataclass(frozen=True, slots=True)
class ExecutionAuthorization:
    """Proof that an execution was authorized at the moment it started.

    :param evaluation: The re-applied evaluation.
    :param approval: The consumed approval, when one was required.
    :param parameter_digest: Digest of the exact parameters executed.
    """

    evaluation: PolicyEvaluation
    approval: Approval | None
    parameter_digest: str


class PolicyEngine:
    """Evaluate tool calls against capability manifests, rules, and approvals.

    :param policies: Loaded policy documents. An empty sequence installs the
        shipped read-only default, so a missing configuration directory fails
        closed rather than open.
    :param manifests: Capability manifests; defaults to the shipped inventory.
    :param service_unit_allowlist: Units ``system.service.restart`` may target.
    """

    def __init__(
        self,
        *,
        policies: Iterable[PolicyDocument] = (),
        manifests: Iterable[ToolManifest] | None = None,
        service_unit_allowlist: Iterable[str] = (),
    ) -> None:
        self._policies = tuple(policies)
        self._manifests = {manifest.name: manifest for manifest in (manifests or builtin_manifest_index().values())}
        self._service_unit_allowlist = frozenset(service_unit_allowlist)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_configuration(
        cls,
        *,
        directory: str = POLICY_DIRECTORY,
        service_unit_allowlist: Iterable[str] = (),
        manifest_path: str | None = None,
    ) -> "PolicyEngine":
        """Load policies from ``directory`` and build an engine.

        :param directory: Directory holding policy documents.
        :param service_unit_allowlist: Units restarts may target.
        :param manifest_path: Optional administrator-supplied manifest override file.
        :raises ConfigurationError: When a document is unreadable or invalid.
        """
        documents = load_policy_directory(directory)
        manifests = builtin_manifest_index()
        if manifest_path and os.path.isfile(manifest_path):
            manifests.update(_load_manifest_override(manifest_path))
        engine = cls(
            policies=documents,
            manifests=manifests.values(),
            service_unit_allowlist=service_unit_allowlist,
        )
        return engine

    # -- introspection -----------------------------------------------------

    @property
    def policies(self) -> tuple[PolicyDocument, ...]:
        """Return the loaded policy documents."""
        return self._policies

    @property
    def manifests(self) -> Mapping[str, ToolManifest]:
        """Return the manifest index keyed by tool name."""
        return dict(self._manifests)

    def manifest(self, tool_name: str) -> ToolManifest | None:
        """Return the manifest for ``tool_name``, or ``None`` when undeclared."""
        return self._manifests.get(tool_name)

    def inventory(self) -> list[dict[str, Any]]:
        """Return every declared tool with its level and current rule outcome."""
        inventory: list[dict[str, Any]] = []
        for name in sorted(self._manifests):
            manifest = self._manifests[name]
            rule = self._first_rule(name)
            inventory.append(
                {
                    **manifest.to_dict(),
                    "rule_name": rule.name if rule else None,
                    "rule_decision": rule.decision.value if rule else None,
                    "effective_level_cap": int(self._level_ceiling()),
                }
            )
        return inventory

    def _level_ceiling(self) -> PrivilegeLevel:
        """Return the highest privilege level any loaded policy permits."""
        if not self._policies:
            return PrivilegeLevel.READ_ONLY
        return max((policy.max_level for policy in self._policies), default=PrivilegeLevel.READ_ONLY)

    def _first_rule(self, tool_name: str):
        """Return the most authoritative rule covering ``tool_name``."""
        candidates: list[Any] = []
        for policy in self._policies:
            candidates.extend(policy.matching_rules(tool_name))
        candidates.sort(key=lambda rule: (rule.priority, rule.specificity()), reverse=True)
        return candidates[0] if candidates else None

    def service_unit_allowlist(self) -> frozenset[str]:
        """Return the effective set of units a restart may target."""
        from_configuration = set(self._service_unit_allowlist)
        for policy in self._policies:
            from_configuration.update(policy.allowlisted_service_units)
        return frozenset(from_configuration)

    # -- evaluation --------------------------------------------------------

    def evaluate(
        self,
        tool_name: str,
        parameters: Mapping[str, Any],
        context: PolicyContext,
    ) -> PolicyEvaluation:
        """Decide whether one proposed tool call may proceed.

        :param tool_name: Tool the runtime wants to call.
        :param parameters: Raw parameters, validated here before any decision.
        :param context: Caller facts.
        :returns: An evaluation; never raises for an ordinary denial.
        """
        manifest = self._manifests.get(tool_name)
        if manifest is None:
            return PolicyEvaluation(
                decision=Decision.DENY,
                tool=str(tool_name),
                level=PrivilegeLevel.ADMINISTRATIVE,
                target=None,
                reason=(
                    f"tool {tool_name!r} has no capability manifest, so its authority is undeclared; "
                    "AgentOS denies undeclared tools"
                ),
            )
        try:
            normalized = manifest.validate_parameters(parameters)
        except ValidationError as error:
            return PolicyEvaluation(
                decision=Decision.DENY,
                tool=manifest.name,
                level=manifest.level,
                target=None,
                reason=f"parameters rejected: {error.message}",
            )
        target = manifest.target_of(normalized)
        if manifest.denied:
            return PolicyEvaluation(
                decision=Decision.DENY,
                tool=manifest.name,
                level=manifest.level,
                target=target,
                reason=f"tool is denied by policy: {manifest.summary}",
                parameters=normalized,
            )
        if manifest.level > self._level_ceiling():
            return PolicyEvaluation(
                decision=Decision.DENY,
                tool=manifest.name,
                level=manifest.level,
                target=target,
                reason=(
                    f"{manifest.level.label} exceeds the maximum level this deployment permits "
                    f"({self._level_ceiling().label})"
                ),
                parameters=normalized,
            )
        if manifest.level > context.task_level_cap:
            return PolicyEvaluation(
                decision=Decision.DENY,
                tool=manifest.name,
                level=manifest.level,
                target=target,
                reason=(
                    f"{manifest.level.label} exceeds this task's cap ({context.task_level_cap.label}); "
                    "the task was not submitted with that authority"
                ),
                parameters=normalized,
            )
        target_problem = self._check_target(manifest, normalized, context)
        if target_problem is not None:
            return PolicyEvaluation(
                decision=Decision.DENY,
                tool=manifest.name,
                level=manifest.level,
                target=target,
                reason=target_problem,
                parameters=normalized,
            )
        rule = self._first_rule(manifest.name)
        if rule is None:
            if manifest.level >= PrivilegeLevel.CONTROLLED:
                return PolicyEvaluation(
                    decision=Decision.DENY,
                    tool=manifest.name,
                    level=manifest.level,
                    target=target,
                    reason=(
                        f"no policy rule permits {manifest.name!r} at {manifest.level.label}; "
                        "the shipped default policy is read-only"
                    ),
                    parameters=normalized,
                )
            return PolicyEvaluation(
                decision=Decision.ALLOW,
                tool=manifest.name,
                level=manifest.level,
                target=target,
                reason=f"{manifest.level.label} read-only capability with no restrictive rule",
                parameters=normalized,
            )
        if manifest.level > rule.max_level:
            return PolicyEvaluation(
                decision=Decision.DENY,
                tool=manifest.name,
                level=manifest.level,
                target=target,
                reason=(
                    f"rule {rule.name!r} covers at most {rule.max_level.label}, but {manifest.name!r} "
                    f"requires {manifest.level.label}"
                ),
                rule_name=rule.name,
                parameters=normalized,
            )
        if rule.decision is Decision.DENY:
            return PolicyEvaluation(
                decision=Decision.DENY,
                tool=manifest.name,
                level=manifest.level,
                target=target,
                reason=rule.description or f"denied by rule {rule.name!r}",
                rule_name=rule.name,
                parameters=normalized,
            )
        approval_required = rule.decision is Decision.REQUIRE_APPROVAL or rule.requires_approval or manifest.requires_approval
        if approval_required and manifest.level < PrivilegeLevel.CONTROLLED:
            approval_required = True
        return PolicyEvaluation(
            decision=Decision.REQUIRE_APPROVAL if approval_required else Decision.ALLOW,
            tool=manifest.name,
            level=manifest.level,
            target=target,
            reason=rule.description
            or (f"permitted by rule {rule.name!r}" if not approval_required else f"rule {rule.name!r} requires approval"),
            rule_name=rule.name,
            requires_approval=approval_required,
            parameters=normalized,
        )

    def _check_target(
        self,
        manifest: ToolManifest,
        parameters: Mapping[str, Any],
        context: PolicyContext,
    ) -> str | None:
        """Return a denial reason when the action target is not acceptable."""
        if manifest.allowlist_parameter is None:
            return None
        value = parameters.get(manifest.allowlist_parameter)
        if value is None:
            return f"{manifest.name!r} requires an explicit {manifest.allowlist_parameter!r}"
        allowlist = set(self.service_unit_allowlist())
        allowlist.update(context.approved_units)
        for rule in self._policies:
            for candidate in rule.rules:
                if candidate.matches(manifest.name):
                    allowlist.update(candidate.allowed_targets)
        if not allowlist:
            return (
                f"no allowlist is configured for {manifest.name!r}; "
                "add the target to allowlisted_service_units in the policy before enabling the tool"
            )
        if str(value) not in allowlist:
            return (
                f"target {value!r} is not in the configured allowlist for {manifest.name!r} "
                f"({len(allowlist)} permitted value(s))"
            )
        return None

    def require_permitted(self, evaluation: PolicyEvaluation) -> None:
        """Raise the appropriate error when an evaluation does not permit execution.

        :param evaluation: Result of :meth:`evaluate`.
        :raises PolicyDenied: When policy denies the call.
        :raises ApprovalRequired: When an approval gate applies.
        """
        if evaluation.decision is Decision.DENY:
            raise PolicyDenied(
                f"policy denied {evaluation.tool!r}: {evaluation.reason}",
                details={"tool": evaluation.tool, "reason": evaluation.reason, "level": int(evaluation.level)},
            )
        if evaluation.decision is Decision.REQUIRE_APPROVAL:
            raise ApprovalRequired(
                f"{evaluation.tool!r} requires approval: {evaluation.reason}",
                details={"tool": evaluation.tool, "target": evaluation.target, "reason": evaluation.reason},
            )

    # -- execution-time revalidation --------------------------------------

    def authorize_execution(
        self,
        tool_name: str,
        parameters: Mapping[str, Any],
        context: PolicyContext,
        *,
        approval: Approval | None,
        nonce: str | None,
    ) -> ExecutionAuthorization:
        """Re-validate immediately before execution and consume the approval.

        :param tool_name: Tool about to run.
        :param parameters: Exact parameters about to be used.
        :param context: Caller facts.
        :param approval: Approval presented by the caller, when one is required.
        :param nonce: Single-use nonce presented with the approval.
        :returns: Proof of authorization.
        :raises PolicyDenied: When policy denies at execution time.
        :raises ApprovalInvalid: When an approval is needed but unusable.
        """
        evaluation = self.evaluate(tool_name, parameters, context)
        if evaluation.decision is Decision.DENY:
            raise PolicyDenied(
                f"policy denied {tool_name!r} at execution time: {evaluation.reason}",
                details={"tool": tool_name, "reason": evaluation.reason},
            )
        if evaluation.decision is Decision.ALLOW:
            return ExecutionAuthorization(evaluation=evaluation, approval=None, parameter_digest="")
        if approval is None:
            raise ApprovalRequired(
                f"{tool_name!r} requires an approval bound to this exact target and parameter set",
                details={"tool": tool_name, "target": evaluation.target},
            )
        request = ApprovalRequest(
            task_id=approval.request.task_id,
            operation=tool_name,
            target=evaluation.target,
            parameters=evaluation.parameters,
            level=int(evaluation.level),
            requested_by_uid=context.uid,
            requested_by_name=context.user_name,
        )
        approval.check_binding(request, nonce=nonce or "")
        return ExecutionAuthorization(
            evaluation=evaluation,
            approval=approval,
            parameter_digest=request.parameter_digest,
        )

    def approval_ttl_seconds(self, requested: int | None) -> int:
        """Clamp a requested approval lifetime to the configured bounds.

        :param requested: Lifetime the caller asked for; ``None`` selects the maximum.
        :raises ValidationError: When the requested value is not a positive integer.
        """
        if requested is not None and (isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0):
            raise ValidationError("approval ttl must be a positive integer number of seconds")
        floor = min(
            (policy.minimum_approval_ttl_seconds for policy in self._policies),
            default=30,
        )
        ceiling = min(
            [policy.maximum_approval_ttl_seconds for policy in self._policies] + [MAX_APPROVAL_TTL_SECONDS]
        )
        if ceiling < floor:
            ceiling = floor
        if requested is None:
            return ceiling
        return max(floor, min(ceiling, requested))

    def approval_expiry(self, ttl_seconds: int | None = None) -> str:
        """Return the expiry timestamp for a new approval."""
        ttl = self.approval_ttl_seconds(ttl_seconds)
        return format_timestamp(utc_now() + timedelta(seconds=ttl))


def _load_manifest_override(path: str) -> dict[str, ToolManifest]:
    """Load administrator-supplied manifest overrides.

    The override file may only tighten the shipped inventory: it can add a
    manifest for a tool AgentOS already implements, but a manifest that lowers a
    level is rejected, because relaxing authority is a policy change rather than
    a capability description.
    """
    document = load_toml_file(path)
    raw_tools = document.get("tools")
    if raw_tools is None:
        return {}
    if not isinstance(raw_tools, Mapping):
        raise ConfigurationError(f"{path}: 'tools' must be a table")
    builtin = builtin_manifest_index()
    overrides: dict[str, ToolManifest] = {}
    for tool_name, raw in raw_tools.items():
        if not isinstance(raw, Mapping):
            raise ConfigurationError(f"{path}: tools.{tool_name} must be a table")
        known = {"summary", "level", "requires_approval", "read_only_guarantee", "idempotent"}
        unknown = sorted(set(str(key) for key in raw) - known)
        if unknown:
            raise ConfigurationError(f"{path}: tools.{tool_name} has unknown key(s): " + ", ".join(unknown))
        existing = builtin.get(str(tool_name))
        if existing is None:
            raise ConfigurationError(
                f"{path}: tools.{tool_name} does not correspond to a tool AgentOS implements; "
                "manifests describe implemented tools and cannot invent authority"
            )
        level_text = str(raw.get("level", int(existing.level))).upper()
        level_table = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        if level_text not in level_table:
            raise ConfigurationError(f"{path}: tools.{tool_name}.level must be P0..P3")
        level = PrivilegeLevel(level_table[level_text])
        if level < existing.level:
            raise ConfigurationError(
                f"{path}: tools.{tool_name}.level may not be lowered below {existing.level.label}"
            )
        overrides[str(tool_name)] = ToolManifest(
            name=existing.name,
            summary=str(raw.get("summary", existing.summary)),
            level=level,
            parameters=existing.parameters,
            target_parameter=existing.target_parameter,
            requires_approval=bool(raw.get("requires_approval", existing.requires_approval)),
            denied=existing.denied,
            allowlist_parameter=existing.allowlist_parameter,
            read_only_guarantee=str(raw.get("read_only_guarantee", existing.read_only_guarantee)),
            idempotent=bool(raw.get("idempotent", existing.idempotent)),
        )
    return overrides


def load_policy_directory(directory: str = POLICY_DIRECTORY) -> tuple[PolicyDocument, ...]:
    """Load every policy document from ``directory``.

    The loader understands the YAML subset used by the shipped policy: nested
    mappings, lists of scalars, and lists of mappings. A document that needs
    more than that is a signal to migrate it, not a reason to guess.

    :param directory: Directory to scan; a missing directory yields no documents.
    :raises ConfigurationError: When a document is unreadable or invalid.
    """
    if not os.path.isdir(directory):
        return ()
    names = sorted(
        name for name in os.listdir(directory) if name.endswith((".yaml", ".yml", ".json", ".toml"))
    )[:MAX_POLICY_DOCUMENTS]
    documents: list[PolicyDocument] = []
    for name in names:
        path = os.path.join(directory, name)
        if name.endswith(".json"):
            import json

            try:
                with open(path, "r", encoding="utf-8") as handle:
                    decoded = json.load(handle)
            except (OSError, ValueError) as error:
                raise ConfigurationError(f"cannot load policy {path}: {error}", details={"path": path}) from error
        else:
            from ..yaml_subset import load_yaml_file

            decoded = load_yaml_file(path) if name.endswith((".yaml", ".yml")) else load_toml_file(path)
        documents.append(parse_policy(decoded, name=os.path.splitext(name)[0], source=path))
    return tuple(documents)


def describe_allowlisted_units() -> list[str]:
    """Return the allowlisted units that exist on this machine, for diagnostics.

    Read-only: the helper reports which configured units are present so that an
    administrator can see an allowlist entry that can never match.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager", "--plain"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    units: list[str] = []
    for line in completed.stdout.splitlines():
        parts = line.split()
        if parts:
            units.append(parts[0])
    return units


__all__ = [
    "MAX_POLICY_DOCUMENTS",
    "POLICY_DIRECTORY",
    "ExecutionAuthorization",
    "PolicyContext",
    "PolicyEngine",
    "PolicyEvaluation",
    "describe_allowlisted_units",
    "load_policy_directory",
]
