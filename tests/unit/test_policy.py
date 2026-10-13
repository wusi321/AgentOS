"""Policy engine tests: capability manifests, rules, levels, and fail-closed behaviour."""

from __future__ import annotations

import pytest

from agentos.errors import ApprovalInvalid, ApprovalRequired, PolicyDenied, ValidationError
from agentos.policy import (
    Decision,
    PolicyContext,
    PolicyEngine,
    PolicyRule,
    PrivilegeLevel,
    ToolManifest,
    builtin_manifest_index,
    parse_policy,
)
from agentos.policy.capabilities import ParameterSchema, ParameterSpec
from agentos.policy.rules import rule_from_mapping
from agentos.schema import FORBIDDEN_PARAMETER_NAMES


def read_context(**overrides: object) -> PolicyContext:
    """Return a P0 caller context."""
    defaults: dict[str, object] = {"uid": 1000, "user_name": "tester", "task_level_cap": PrivilegeLevel.READ_ONLY}
    defaults.update(overrides)
    return PolicyContext(**defaults)  # type: ignore[arg-type]


def test_manifest_inventory_is_complete() -> None:
    """Every shipped manifest declares a level, a summary, and no shell parameter."""
    index = builtin_manifest_index()
    assert index, "the shipped manifest inventory must not be empty"
    for name, manifest in index.items():
        assert manifest.name == name
        assert manifest.summary
        assert manifest.level in set(PrivilegeLevel)
        assert not (manifest.parameters.names() & FORBIDDEN_PARAMETER_NAMES)


def test_readonly_manifests_do_not_require_approval() -> None:
    """A P0 tool is inspection only and needs no human in the loop."""
    for manifest in builtin_manifest_index().values():
        if manifest.level is PrivilegeLevel.READ_ONLY:
            assert manifest.requires_approval is False
            assert manifest.denied is False


def test_controlled_manifests_require_approval_or_are_denied() -> None:
    """The manifest validator refuses a P2+ tool with no gate at all."""
    with pytest.raises(ValidationError):
        ToolManifest(
            name="system.service.restart",
            summary="no gate",
            level=PrivilegeLevel.CONTROLLED,
            parameters=ParameterSchema(parameters=(ParameterSpec("unit", "string"),)),
            requires_approval=False,
        )


def test_manifest_rejects_shell_shaped_parameters() -> None:
    """A manifest cannot even declare a command parameter."""
    with pytest.raises(ValidationError):
        ToolManifest(
            name="system.x.y",
            summary="bad",
            level=PrivilegeLevel.READ_ONLY,
            parameters=ParameterSchema(parameters=(ParameterSpec("command", "string"),)),
        )


def test_manifest_rejects_unknown_target_parameter() -> None:
    """A declared target must exist in the parameter set."""
    with pytest.raises(ValidationError):
        ToolManifest(
            name="system.x.y",
            summary="bad",
            level=PrivilegeLevel.READ_ONLY,
            parameters=ParameterSchema(parameters=(ParameterSpec("pid", "integer"),)),
            target_parameter="unit",
        )


def test_undeclared_tool_is_denied() -> None:
    """A tool with no manifest has no declared authority, so it is refused."""
    engine = PolicyEngine(policies=[])
    evaluation = engine.evaluate("system.made.up", {}, read_context())
    assert evaluation.decision is Decision.DENY
    assert "no capability manifest" in evaluation.reason


def test_readonly_tool_allowed_without_any_policy() -> None:
    """Inspection keeps working even with no policy documents loaded."""
    engine = PolicyEngine(policies=[])
    evaluation = engine.evaluate("system.metrics.read", {}, read_context())
    assert evaluation.decision is Decision.ALLOW


def test_unknown_parameter_is_denied() -> None:
    """The parameter set is closed."""
    engine = PolicyEngine(policies=[])
    evaluation = engine.evaluate("system.processes.list", {"limit": 5, "evil": 1}, read_context())
    assert evaluation.decision is Decision.DENY
    assert "unknown parameter" in evaluation.reason


def test_out_of_range_parameter_is_denied() -> None:
    """A declared bound is enforced before any decision is made."""
    engine = PolicyEngine(policies=[])
    above = engine.evaluate("system.processes.list", {"limit": 100_000}, read_context())
    below = engine.evaluate("system.processes.list", {"limit": 0}, read_context())
    assert above.decision is Decision.DENY
    assert below.decision is Decision.DENY


def test_enum_parameter_is_enforced() -> None:
    """A closed value set rejects anything outside it."""
    engine = PolicyEngine(policies=[])
    evaluation = engine.evaluate("system.processes.list", {"sort_by": "secrets"}, read_context())
    assert evaluation.decision is Decision.DENY


def test_denied_manifest_is_denied_at_any_level() -> None:
    """A tool marked denied stays denied even for root-level authority."""
    engine = PolicyEngine(policies=[])
    context = read_context(task_level_cap=PrivilegeLevel.ADMINISTRATIVE, uid=0)
    evaluation = engine.evaluate("system.packages.install", {}, context)
    assert evaluation.decision is Decision.DENY


def test_controlled_tool_exceeds_a_p0_ceiling(default_policy_engine: PolicyEngine) -> None:
    """The shipped default policy refuses P2 work."""
    context = read_context(task_level_cap=PrivilegeLevel.CONTROLLED)
    evaluation = default_policy_engine.evaluate("system.process.terminate", {"pid": 4242}, context)
    assert evaluation.decision is Decision.DENY
    assert "exceeds the maximum level" in evaluation.reason


def test_task_cap_denies_before_policy_rules(site_policy_engine: PolicyEngine) -> None:
    """A task created at P0 cannot reach a P2 operation even when policy allows it."""
    evaluation = site_policy_engine.evaluate("system.process.terminate", {"pid": 4242}, read_context())
    assert evaluation.decision is Decision.DENY
    assert "task's cap" in evaluation.reason


def test_controlled_tool_requires_approval_when_permitted(site_policy_engine: PolicyEngine) -> None:
    """With P2 authority in both policy and task, the gate moves to approval."""
    context = read_context(task_level_cap=PrivilegeLevel.CONTROLLED)
    evaluation = site_policy_engine.evaluate("system.process.terminate", {"pid": 4242}, context)
    assert evaluation.decision is Decision.REQUIRE_APPROVAL
    assert evaluation.requires_approval is True
    assert evaluation.target == "4242"


def test_allowlisted_target_is_required(site_policy_engine: PolicyEngine) -> None:
    """A restart outside the allowlist is refused, and the allowlist is reported."""
    context = read_context(task_level_cap=PrivilegeLevel.CONTROLLED)
    allowed = site_policy_engine.evaluate("system.service.restart", {"unit": "cups.service"}, context)
    refused = site_policy_engine.evaluate("system.service.restart", {"unit": "systemd-journald.service"}, context)
    assert allowed.decision is Decision.REQUIRE_APPROVAL
    assert refused.decision is Decision.DENY
    assert "not in the configured allowlist" in refused.reason
    assert "cups.service" in site_policy_engine.service_unit_allowlist()


def test_no_allowlist_configured_refuses_every_target() -> None:
    """An empty allowlist means the operation cannot run at all."""
    policy = parse_policy(
        {
            "name": "no-targets",
            "max_level": "P2",
            "rules": [
                {
                    "name": "restart-open",
                    "action": "require_approval",
                    "tool": "system.service.restart",
                    "max_level": "P2",
                }
            ],
        },
        name="no-targets",
    )
    engine = PolicyEngine(policies=[policy])
    context = read_context(task_level_cap=PrivilegeLevel.CONTROLLED)
    evaluation = engine.evaluate("system.service.restart", {"unit": "cups.service"}, context)
    assert evaluation.decision is Decision.DENY
    assert "no allowlist is configured" in evaluation.reason


def test_higher_priority_rule_wins() -> None:
    """Priority beats a more specific pattern."""
    policy = parse_policy(
        {
            "name": "priority",
            "max_level": "P0",
            "rules": [
                {"name": "specific", "action": "allow", "tool": "system.processes.list", "max_level": "P0"},
                {"name": "general", "action": "deny", "tool": "system.*", "max_level": "P1", "priority": 5},
            ],
        },
        name="priority",
    )
    engine = PolicyEngine(policies=[policy])
    evaluation = engine.evaluate("system.processes.list", {}, read_context())
    assert evaluation.decision is Decision.DENY
    assert evaluation.rule_name == "general"


def test_rule_level_ceiling_is_enforced() -> None:
    """A rule that covers only P0 cannot permit a P2 tool."""
    policy = parse_policy(
        {
            "name": "low-ceiling",
            "max_level": "P2",
            "allowlisted_service_units": ["cups.service"],
            "rules": [
                {"name": "restart", "action": "require_approval", "tool": "system.service.restart", "max_level": "P0"}
            ],
        },
        name="low-ceiling",
    )
    engine = PolicyEngine(policies=[policy])
    context = read_context(task_level_cap=PrivilegeLevel.CONTROLLED)
    evaluation = engine.evaluate("system.service.restart", {"unit": "cups.service"}, context)
    assert evaluation.decision is Decision.DENY
    assert "covers at most" in evaluation.reason


def test_allow_rule_may_not_cover_administrative_tools() -> None:
    """A blanket allow at P3 is rejected at rule construction."""
    with pytest.raises(ValidationError):
        PolicyRule(name="bad", decision=Decision.ALLOW, tool_pattern="system.*", max_level=PrivilegeLevel.ADMINISTRATIVE)


def test_unknown_policy_key_is_rejected() -> None:
    """A typo in a policy file fails loudly."""
    from agentos.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        parse_policy({"name": "x", "mystery": 1}, name="x")


def test_unknown_rule_key_is_rejected() -> None:
    """A typo inside a rule fails loudly."""
    from agentos.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        parse_policy({"name": "x", "rules": [{"action": "allow", "tool": "a.b", "mystery": 1}]}, name="x")


def test_unknown_rule_action_is_rejected() -> None:
    """Only the three documented actions are accepted."""
    from agentos.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        parse_policy({"name": "x", "rules": [{"action": "permit", "tool": "a.b"}]}, name="x")


def test_minimum_ttl_above_maximum_is_rejected() -> None:
    """An inverted approval window is caught at load time."""
    from agentos.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        parse_policy(
            {"name": "x", "minimum_approval_ttl_seconds": 900, "maximum_approval_ttl_seconds": 60},
            name="x",
        )


def test_approval_ttl_is_clamped() -> None:
    """A client cannot ask for a longer window than policy allows."""
    policy = parse_policy(
        {"name": "ttl", "minimum_approval_ttl_seconds": 60, "maximum_approval_ttl_seconds": 300},
        name="ttl",
    )
    engine = PolicyEngine(policies=[policy])
    assert engine.approval_ttl_seconds(None) == 300
    assert engine.approval_ttl_seconds(10) == 60
    assert engine.approval_ttl_seconds(10_000) == 300
    assert engine.approval_ttl_seconds(200) == 200
    with pytest.raises(ValidationError):
        engine.approval_ttl_seconds(0)


def test_manifest_override_cannot_lower_a_level() -> None:
    """A capability description may tighten authority, never relax it."""
    from agentos.errors import ConfigurationError

    from agentos.policy.engine import _load_manifest_override

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "override.toml"
        path.write_text('[tools."system.process.terminate"]\nlevel = "P0"\n', encoding="utf-8")
        with pytest.raises(ConfigurationError):
            _load_manifest_override(str(path))


def test_manifest_override_cannot_invent_authority() -> None:
    """An override cannot describe a tool AgentOS does not implement."""
    from agentos.errors import ConfigurationError

    from agentos.policy.engine import _load_manifest_override

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "override.toml"
        path.write_text('[tools."system.made.up"]\nlevel = "P0"\nsummary = "invented"\n', encoding="utf-8")
        with pytest.raises(ConfigurationError):
            _load_manifest_override(str(path))


def test_rule_from_mapping_rejects_empty_pattern() -> None:
    """A rule must name something."""
    from agentos.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        rule_from_mapping({"action": "allow", "tool": ""})


def test_inventory_exposes_levels_and_rules(site_policy_engine: PolicyEngine) -> None:
    """The operator inventory reports every tool with its governing rule."""
    inventory = site_policy_engine.inventory()
    by_name = {entry["name"]: entry for entry in inventory}
    assert by_name["system.process.terminate"]["rule_name"] == "require-approval-for-process-terminate"
    assert by_name["system.processes.list"]["rule_name"] == "allow-list-read-tools"
    assert by_name["system.metrics.read"]["rule_decision"] in {None, "allow"}


def test_require_permitted_raises_the_right_error(site_policy_engine: PolicyEngine) -> None:
    """Callers get a denial or an approval requirement, never a silent pass."""
    context = read_context(task_level_cap=PrivilegeLevel.CONTROLLED)
    approval_needed = site_policy_engine.evaluate("system.process.terminate", {"pid": 42}, context)
    with pytest.raises(ApprovalRequired):
        site_policy_engine.require_permitted(approval_needed)
    denied = site_policy_engine.evaluate("system.service.restart", {"unit": "nope.service"}, context)
    with pytest.raises(PolicyDenied):
        site_policy_engine.require_permitted(denied)


def test_authorize_execution_requires_approval_for_controlled_tool(site_policy_engine: PolicyEngine) -> None:
    """Execution-time revalidation refuses a controlled tool with no approval."""
    context = read_context(task_level_cap=PrivilegeLevel.CONTROLLED)
    with pytest.raises(ApprovalRequired):
        site_policy_engine.authorize_execution(
            "system.process.terminate", {"pid": 4242}, context, approval=None, nonce=None
        )


def test_authorize_execution_rejects_a_foreign_approval(site_policy_engine: PolicyEngine) -> None:
    """An approval for another target cannot authorize this call."""
    from agentos.policy import Approval, ApprovalRequest

    context = read_context(task_level_cap=PrivilegeLevel.CONTROLLED)
    approval = Approval(
        approval_id="a1",
        request=ApprovalRequest(
            task_id="t1",
            operation="system.process.terminate",
            target="4242",
            parameters={"pid": 4242},
            level=2,
            requested_by_uid=1000,
        ),
        state="granted",
    )
    with pytest.raises(ApprovalInvalid):
        site_policy_engine.authorize_execution(
            "system.process.terminate",
            {"pid": 9999},
            context,
            approval=approval,
            nonce=approval.nonce,
        )


def test_authorize_execution_rejects_a_bad_nonce(site_policy_engine: PolicyEngine) -> None:
    """The single-use nonce is part of the binding."""
    from agentos.policy import Approval, ApprovalRequest

    context = read_context(task_level_cap=PrivilegeLevel.CONTROLLED)
    approval = Approval(
        approval_id="a2",
        request=ApprovalRequest(
            task_id="t1",
            operation="system.process.terminate",
            target="4242",
            parameters={"pid": 4242},
            level=2,
            requested_by_uid=1000,
        ),
        state="granted",
    )
    with pytest.raises(ApprovalInvalid):
        site_policy_engine.authorize_execution(
            "system.process.terminate", {"pid": 4242}, context, approval=approval, nonce="wrong"
        )


def test_authorize_execution_accepts_a_correctly_bound_approval(site_policy_engine: PolicyEngine) -> None:
    """A matching approval authorizes exactly one parameter set."""
    from agentos.policy import Approval, ApprovalRequest

    context = read_context(task_level_cap=PrivilegeLevel.CONTROLLED)
    approval = Approval(
        approval_id="a3",
        request=ApprovalRequest(
            task_id="t1",
            operation="system.process.terminate",
            target="4242",
            parameters={"pid": 4242},
            level=2,
            requested_by_uid=1000,
        ),
        state="granted",
    )
    authorization = site_policy_engine.authorize_execution(
        "system.process.terminate", {"pid": 4242}, context, approval=approval, nonce=approval.nonce
    )
    assert authorization.approval is approval
    assert authorization.parameter_digest == approval.request.parameter_digest


def test_authorize_execution_revalidates_policy_at_execution_time() -> None:
    """A policy that changed after the approval was granted still applies."""
    from agentos.policy import Approval, ApprovalRequest

    policy = parse_policy(
        {
            "name": "then-allow",
            "max_level": "P2",
            "allowlisted_service_units": ["cups.service"],
            "rules": [
                {
                    "name": "restart",
                    "action": "require_approval",
                    "tool": "system.service.restart",
                    "max_level": "P2",
                }
            ],
        },
        name="then-allow",
    )
    engine = PolicyEngine(policies=[policy])
    context = read_context(task_level_cap=PrivilegeLevel.CONTROLLED)
    approval = Approval(
        approval_id="a4",
        request=ApprovalRequest(
            task_id="t1",
            operation="system.service.restart",
            target="cups.service",
            parameters={"unit": "cups.service"},
            level=2,
            requested_by_uid=1000,
        ),
        state="granted",
    )
    tightened = PolicyEngine(policies=[parse_policy({"name": "now-deny", "max_level": "P0"}, name="now-deny")])
    with pytest.raises(PolicyDenied):
        tightened.authorize_execution(
            "system.service.restart",
            {"unit": "cups.service"},
            context,
            approval=approval,
            nonce=approval.nonce,
        )
