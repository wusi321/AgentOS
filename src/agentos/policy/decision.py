"""Policy decision vocabulary shared by capabilities, rules, and the engine."""

from __future__ import annotations

from enum import Enum


class Decision(Enum):
    """Outcome of a policy evaluation.

    ``REQUIRE_APPROVAL`` is not a denial: it means the operation is permitted
    once a named human authorizes this exact operation, target, and parameter set.
    """

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"

    @property
    def is_permitted(self) -> bool:
        """Return whether the decision lets the operation proceed immediately."""
        return self is Decision.ALLOW


__all__ = ["Decision"]
