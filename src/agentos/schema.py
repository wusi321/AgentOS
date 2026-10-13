"""Minimal schema validation and coercion for values crossing trust boundaries.

AgentOS validates at its real boundaries only: the IPC wire, configuration
files, model/tool JSON produced by a runtime, and durable rows read back from
SQLite. This module supplies the small vocabulary those checks need without
adding a runtime dependency to a package that must install on a bare Ubuntu
system Python.

:func:`object_schema` and :func:`string_schema` are deliberately tiny: they
describe a flat object of scalar or list-of-scalar members, which is all the
capability manifests and IPC parameters need.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping, Sequence
from typing import Any, Final

from .errors import ValidationError
from .jsonutil import looks_sensitive

SCALAR_TYPES: Final[tuple[str, ...]] = ("string", "integer", "number", "boolean")

#: Parameter names that may never appear in a tool manifest, whatever the tool does.
FORBIDDEN_PARAMETER_NAMES: Final[frozenset[str]] = frozenset(
    {"shell", "command", "cmd", "script", "argv", "eval", "exec", "sudo", "sh", "bash"}
)


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """One declared tool parameter.

    :param name: Parameter name as it appears in the request object.
    :param type: One of :data:`SCALAR_TYPES`, or ``array`` of a scalar type.
    :param required: Whether the parameter must be present.
    :param description: Short operator-facing explanation.
    :param maximum: Inclusive upper bound for numeric parameters.
    :param minimum: Inclusive lower bound for numeric parameters.
    :param max_length: Inclusive upper bound for string parameters.
    :param choices: Closed set of accepted values, when the parameter is enumerable.
    :param pattern_free_form: Set when a string legitimately accepts arbitrary
        text (for example a service name). Such parameters are additionally
        checked against :data:`FORBIDDEN_PARAMETER_NAMES` at manifest load and
        rejected when they could carry a shell payload.
    """

    name: str
    type: str
    required: bool = True
    description: str = ""
    maximum: float | None = None
    minimum: float | None = None
    max_length: int | None = None
    choices: tuple[Any, ...] = ()
    pattern_free_form: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON Schema fragment describing this parameter."""
        schema: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.type == "array":
            schema = {"type": "array", "items": {"type": "string"}, "description": self.description}
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.max_length is not None:
            schema["maxLength"] = self.max_length
        if self.choices:
            schema["enum"] = list(self.choices)
        return schema


@dataclass(frozen=True, slots=True)
class ParameterSchema:
    """The closed parameter set accepted by one tool."""

    parameters: tuple[ParameterSpec, ...] = ()
    additional: bool = False
    notes: tuple[str, ...] = field(default=())

    def names(self) -> frozenset[str]:
        """Return every declared parameter name."""
        return frozenset(spec.name for spec in self.parameters)

    def to_json_schema(self) -> dict[str, Any]:
        """Return the JSON Schema for this parameter set."""
        properties = {spec.name: spec.to_dict() for spec in self.parameters}
        required = [spec.name for spec in self.parameters if spec.required]
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": self.additional,
        }
        if required:
            schema["required"] = required
        return schema

    def validate(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and normalize ``value``, returning a fresh plain dictionary.

        :param value: Candidate parameter object.
        :returns: A normalized copy containing only declared parameters.
        :raises ValidationError: On an unknown, missing, or mistyped parameter.
        """
        if not isinstance(value, Mapping):
            raise ValidationError("parameters must be a JSON object")
        unknown = sorted(set(str(key) for key in value) - self.names())
        if unknown and not self.additional:
            raise ValidationError(
                "unknown parameter(s): " + ", ".join(unknown),
                details={"unknown": unknown, "accepted": sorted(self.names())},
            )
        normalized: dict[str, Any] = {}
        for spec in self.parameters:
            if spec.name not in value:
                if spec.required:
                    raise ValidationError(
                        f"missing required parameter {spec.name!r}",
                        details={"parameter": spec.name},
                    )
                continue
            normalized[spec.name] = _coerce(spec, value[spec.name])
        return normalized


def _coerce(spec: ParameterSpec, raw: Any) -> Any:
    """Validate one parameter value against ``spec``."""
    if spec.type == "array":
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise ValidationError(
                f"parameter {spec.name!r} must be an array of strings",
                details={"parameter": spec.name},
            )
        items: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise ValidationError(
                    f"parameter {spec.name!r} must contain only strings",
                    details={"parameter": spec.name},
                )
            if spec.max_length is not None and len(item) > spec.max_length:
                raise ValidationError(
                    f"parameter {spec.name!r} item exceeds {spec.max_length} characters",
                    details={"parameter": spec.name},
                )
            items.append(_check_free_form(spec, item))
        if spec.choices and any(item not in spec.choices for item in items):
            raise ValidationError(
                f"parameter {spec.name!r} contains a value outside the allowed set",
                details={"parameter": spec.name, "allowed": list(spec.choices)},
            )
        return items
    if spec.type == "string":
        if not isinstance(raw, str):
            raise ValidationError(
                f"parameter {spec.name!r} must be a string",
                details={"parameter": spec.name},
            )
        if spec.max_length is not None and len(raw) > spec.max_length:
            raise ValidationError(
                f"parameter {spec.name!r} exceeds {spec.max_length} characters",
                details={"parameter": spec.name},
            )
        if spec.choices and raw not in spec.choices:
            raise ValidationError(
                f"parameter {spec.name!r} is outside the allowed value set",
                details={"parameter": spec.name, "allowed": list(spec.choices)},
            )
        return _check_free_form(spec, raw)
    if spec.type == "integer":
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValidationError(
                f"parameter {spec.name!r} must be an integer",
                details={"parameter": spec.name},
            )
        return _check_bounds(spec, raw)
    if spec.type == "number":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValidationError(
                f"parameter {spec.name!r} must be a number",
                details={"parameter": spec.name},
            )
        return _check_bounds(spec, float(raw))
    if spec.type == "boolean":
        if not isinstance(raw, bool):
            raise ValidationError(
                f"parameter {spec.name!r} must be a boolean",
                details={"parameter": spec.name},
            )
        return raw
    raise ValidationError(
        f"parameter {spec.name!r} declares unsupported type {spec.type!r}",
        details={"parameter": spec.name, "type": spec.type},
    )


def _check_bounds(spec: ParameterSpec, value: float | int) -> float | int:
    """Enforce the numeric bounds declared by ``spec``."""
    if spec.minimum is not None and value < spec.minimum:
        raise ValidationError(
            f"parameter {spec.name!r} is below the minimum {spec.minimum}",
            details={"parameter": spec.name},
        )
    if spec.maximum is not None and value > spec.maximum:
        raise ValidationError(
            f"parameter {spec.name!r} exceeds the maximum {spec.maximum}",
            details={"parameter": spec.name},
        )
    return value


def _check_free_form(spec: ParameterSpec, value: str) -> str:
    """Reject free-form strings that look like shell payloads.

    This is defence in depth for the executor, which never runs a shell at all;
    the check exists so that a mistaken future executor cannot be fed a payload
    that already passed schema validation.
    """
    if "\x00" in value:
        raise ValidationError(
            f"parameter {spec.name!r} contains a NUL byte",
            details={"parameter": spec.name},
        )
    if looks_sensitive(spec.name) and value:
        raise ValidationError(
            f"parameter {spec.name!r} must never carry credential material",
            details={"parameter": spec.name},
        )
    if not spec.pattern_free_form:
        if any(character in value for character in "\n\r;&|`$><"):
            raise ValidationError(
                f"parameter {spec.name!r} contains characters that are never valid here",
                details={"parameter": spec.name},
            )
    return value


def require_keys(payload: Mapping[str, Any], required: Sequence[str], *, context: str) -> None:
    """Raise :class:`ValidationError` when ``payload`` lacks any required key."""
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValidationError(
            f"{context} is missing required member(s): " + ", ".join(missing),
            details={"context": context, "missing": missing},
        )


def require_type(value: Any, expected: type | tuple[type, ...], *, context: str) -> Any:
    """Raise :class:`ValidationError` when ``value`` is not of ``expected`` type."""
    if expected is int and isinstance(value, bool):
        raise ValidationError(f"{context} must be an integer, not a boolean", details={"context": context})
    if not isinstance(value, expected):
        names = expected.__name__ if isinstance(expected, type) else "/".join(item.__name__ for item in expected)
        raise ValidationError(f"{context} must be {names}", details={"context": context})
    return value


__all__ = [
    "FORBIDDEN_PARAMETER_NAMES",
    "SCALAR_TYPES",
    "ParameterSchema",
    "ParameterSpec",
    "require_keys",
    "require_type",
]
