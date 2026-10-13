"""Minimal TOML reader for the AgentOS subset, including Python 3.10 hosts.

Ubuntu 22.04 ships Python 3.10, whose standard library has no ``tomllib``. The
AgentOS configuration format only needs tables, dotted keys, strings, numbers,
booleans, and arrays of scalars, so this module implements exactly that subset
and refuses everything else instead of guessing.

When ``tomllib`` exists (Python 3.11 and later) it is used, because a shipped
parser is a better answer than a hand-rolled one wherever it is available.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, Final

from .errors import ConfigurationError, ValidationError

try:  # pragma: no cover - exercised by which interpreter runs the tests
    import tomllib as _tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 path
    _tomllib = None

_BARE_KEY: Final = re.compile(r"^[A-Za-z0-9_-]+$")
_INTEGER: Final = re.compile(r"^[+-]?(0|[1-9][0-9_]*)$")
_FLOAT: Final = re.compile(r"^[+-]?(0|[1-9][0-9_]*)(\.[0-9_]+)?([eE][+-]?[0-9]+)?$")
_BASIC_STRING_ESCAPES: Final[Mapping[str, str]] = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    '"': '"',
    "\\": "\\",
    "b": "\b",
    "f": "\f",
    "/": "/",
}


def load_toml(text: str, *, origin: str = "<memory>") -> dict[str, Any]:
    """Parse TOML text into a dictionary.

    :param text: File contents.
    :param origin: Path used in error messages.
    :raises ConfigurationError: When the document uses unsupported TOML syntax
        or is malformed.
    """
    if _tomllib is not None:
        try:
            return _tomllib.loads(text)
        except Exception as error:  # noqa: BLE001 - tomllib raises its own error type
            raise ConfigurationError(f"{origin}: invalid TOML: {error}") from error
    return _SubsetTomlParser(text, origin).parse()


def load_toml_file(path: str) -> dict[str, Any]:
    """Read and parse a TOML file.

    :param path: Filesystem path to read.
    :raises ConfigurationError: When the file cannot be read or parsed.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise ConfigurationError(f"cannot read {path}: {error}", details={"path": path}) from error
    return load_toml(text, origin=path)


class _SubsetTomlParser:
    """Parse the AgentOS configuration subset of TOML.

    Supported: comments, blank lines, ``[table]`` and ``[table.sub]`` headers,
    dotted keys, basic and literal strings, integers, floats, booleans, and
    arrays of scalars. Multi-line strings, inline tables, array-of-table syntax,
    and datetime values raise :class:`ConfigurationError` naming the line.
    """

    def __init__(self, text: str, origin: str) -> None:
        self._lines = text.splitlines()
        self._origin = origin
        self._root: dict[str, Any] = {}
        self._current: dict[str, Any] = self._root
        self._current_path: tuple[str, ...] = ()

    def parse(self) -> dict[str, Any]:
        """Parse the document and return the resulting mapping."""
        for number, raw in enumerate(self._lines, start=1):
            line = _strip_comment(raw).strip()
            if not line:
                continue
            if line.startswith("["):
                self._enter_table(line, number)
                continue
            self._assign(line, number)
        return self._root

    def _fail(self, number: int, message: str) -> "ConfigurationError":
        raise ConfigurationError(
            f"{self._origin}:{number}: {message}",
            details={"line": number, "origin": self._origin},
        )

    def _enter_table(self, line: str, number: int) -> None:
        if line.startswith("[["):
            self._fail(number, "arrays of tables are not part of the AgentOS configuration format")
        if not line.endswith("]"):
            self._fail(number, "table header is not closed")
        body = line[1:-1].strip()
        if not body:
            self._fail(number, "empty table header")
        path = tuple(part.strip().strip('"') for part in body.split("."))
        for part in path:
            if not part:
                self._fail(number, "empty table name segment")
        table = self._root
        for part in path:
            existing = table.get(part)
            if existing is None:
                created: dict[str, Any] = {}
                table[part] = created
                table = created
            elif isinstance(existing, dict):
                table = existing
            else:
                self._fail(number, f"{part!r} is already a value, not a table")
        self._current = table
        self._current_path = path

    def _assign(self, line: str, number: int) -> None:
        key_text, separator, value_text = _split_assignment(line)
        if not separator:
            self._fail(number, "expected key = value")
        keys = [part.strip().strip('"') for part in _split_dotted(key_text)]
        if not all(keys):
            self._fail(number, "empty key segment")
        target = self._current
        for part in keys[:-1]:
            existing = target.get(part)
            if existing is None:
                created: dict[str, Any] = {}
                target[part] = created
                target = created
            elif isinstance(existing, dict):
                target = existing
            else:
                self._fail(number, f"{part!r} is already a value, not a table")
        leaf = keys[-1]
        if leaf in target:
            self._fail(number, f"duplicate key {leaf!r}")
        target[leaf] = _parse_value(value_text.strip(), lambda message: self._fail(number, message))


def _strip_comment(line: str) -> str:
    """Remove a trailing comment, honouring quoted strings."""
    in_basic = False
    in_literal = False
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if in_basic and character == "\\":
            escaped = True
            continue
        if not in_literal and character == '"':
            in_basic = not in_basic
            continue
        if not in_basic and character == "'":
            in_literal = not in_literal
            continue
        if character == "#" and not in_basic and not in_literal:
            return line[:index]
    return line


def _split_assignment(line: str) -> tuple[str, bool, str]:
    """Split ``key = value`` at the first top-level ``=``."""
    in_basic = False
    in_literal = False
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if in_basic and character == "\\":
            escaped = True
            continue
        if not in_literal and character == '"':
            in_basic = not in_basic
            continue
        if not in_basic and character == "'":
            in_literal = not in_literal
            continue
        if character == "=" and not in_basic and not in_literal:
            return line[:index], True, line[index + 1 :]
    return line, False, ""


def _split_dotted(text: str) -> list[str]:
    """Split a dotted key list, honouring quoted segments."""
    parts: list[str] = []
    buffer: list[str] = []
    in_basic = False
    in_literal = False
    escaped = False
    for character in text:
        if escaped:
            buffer.append(character)
            escaped = False
            continue
        if in_basic:
            if character == "\\":
                escaped = True
                continue
            if character == '"':
                in_basic = False
                continue
            buffer.append(character)
            continue
        if in_literal:
            if character == "'":
                in_literal = False
                continue
            buffer.append(character)
            continue
        if character == '"':
            in_basic = True
            continue
        if character == "'":
            in_literal = True
            continue
        if character == ".":
            parts.append("".join(buffer))
            buffer = []
            continue
        buffer.append(character)
    parts.append("".join(buffer))
    return parts


def _parse_value(text: str, fail: Callable[[str], None]) -> Any:
    """Parse one TOML value from the supported subset."""
    if not text:
        fail("missing value")
    if text.startswith('"""') or text.startswith("'''"):
        fail("multi-line strings are not part of the AgentOS configuration format")
    if text.startswith('"'):
        return _parse_basic_string(text, fail)
    if text.startswith("'"):
        if not text.endswith("'") or len(text) < 2:
            fail("unterminated literal string")
        return text[1:-1]
    if text.startswith("["):
        return _parse_array(text, fail)
    if text.startswith("{"):
        fail("inline tables are not part of the AgentOS configuration format")
    if text in {"true", "false"}:
        return text == "true"
    if _INTEGER.match(text):
        return int(text.replace("_", ""))
    if _FLOAT.match(text):
        return float(text.replace("_", ""))
    fail(f"unsupported value {text!r}")
    raise AssertionError("unreachable")  # pragma: no cover - fail() always raises


def _parse_basic_string(text: str, fail: Callable[[str], None]) -> str:
    """Parse a double-quoted TOML basic string."""
    if len(text) < 2 or not text.endswith('"'):
        fail("unterminated string")
    body = text[1:-1]
    result: list[str] = []
    index = 0
    while index < len(body):
        character = body[index]
        if character != "\\":
            result.append(character)
            index += 1
            continue
        index += 1
        if index >= len(body):
            fail("dangling escape sequence")
        escape = body[index]
        if escape in _BASIC_STRING_ESCAPES:
            result.append(_BASIC_STRING_ESCAPES[escape])
            index += 1
            continue
        if escape in {"u", "U"}:
            width = 4 if escape == "u" else 8
            digits = body[index + 1 : index + 1 + width]
            if len(digits) != width:
                fail("truncated unicode escape")
            try:
                result.append(chr(int(digits, 16)))
            except ValueError:
                fail(f"invalid unicode escape \\{escape}{digits}")
            index += 1 + width
            continue
        fail(f"unsupported escape sequence \\{escape}")
    return "".join(result)


def _parse_array(text: str, fail: Callable[[str], None]) -> list[Any]:
    """Parse an array of scalar values, including nested arrays."""
    if not text.endswith("]"):
        fail("unterminated array")
    body = text[1:-1].strip()
    if not body:
        return []
    items: list[Any] = []
    for chunk in _split_top_level(body):
        stripped = chunk.strip()
        if not stripped:
            continue
        items.append(_parse_value(stripped, fail))
    return items


def _split_top_level(body: str) -> list[str]:
    """Split an array body on commas that are not inside strings or nested values."""
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    in_basic = False
    in_literal = False
    escaped = False
    for character in body:
        if escaped:
            buffer.append(character)
            escaped = False
            continue
        if in_basic:
            buffer.append(character)
            if character == "\\":
                escaped = True
            elif character == '"':
                in_basic = False
            continue
        if in_literal:
            buffer.append(character)
            if character == "'":
                in_literal = False
            continue
        if character == '"':
            in_basic = True
            buffer.append(character)
            continue
        if character == "'":
            in_literal = True
            buffer.append(character)
            continue
        if character in "[{":
            depth += 1
            buffer.append(character)
            continue
        if character in "]}":
            depth -= 1
            buffer.append(character)
            continue
        if character == "," and depth == 0:
            parts.append("".join(buffer))
            buffer = []
            continue
        buffer.append(character)
    parts.append("".join(buffer))
    return parts


def merge_mappings(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively overlay ``overlay`` onto ``base`` without mutating either.

    Used to layer ``agentos.local.toml`` over ``agentos.toml`` so that a package
    upgrade can replace the shipped template without discarding administrator
    overrides.

    :raises ValidationError: When the two documents disagree on a value's kind.
    """
    merged: dict[str, Any] = {key: value for key, value in base.items()}
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = merge_mappings(existing, value)
            continue
        if existing is not None and isinstance(existing, Mapping) != isinstance(value, Mapping):
            raise ValidationError(
                f"configuration member {key!r} is a table in one file and a value in another",
                details={"member": key},
            )
        merged[key] = value
    return merged


__all__ = ["load_toml", "load_toml_file", "merge_mappings"]
