"""Minimal YAML reader for the AgentOS policy subset.

The shipped policy documents and any administrator-supplied policy use a small,
predictable YAML shape: nested mappings, lists of scalars, and lists of
mappings, with comments. Rather than add PyYAML to the runtime dependency set of
a system service, this module implements exactly that subset and raises on
anything else — anchors, tags, multi-document streams, block scalars, flow
collections, and multi-line strings are refused with a line number.

Refusing is the security-relevant behaviour: a policy file the loader does not
fully understand must never be interpreted partially and silently.
"""

from __future__ import annotations

import re
from typing import Any, Final

from .errors import ConfigurationError

_PLAIN_INT: Final = re.compile(r"^[+-]?[0-9]+$")
_PLAIN_FLOAT: Final = re.compile(r"^[+-]?([0-9]+\.[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$")
_UNSUPPORTED_PREFIXES: Final = ("&", "*", "!", "|", ">", "%", "@", "`")


def load_yaml(text: str, *, origin: str = "<memory>") -> Any:
    """Parse the AgentOS YAML subset.

    :param text: Document text.
    :param origin: Path used in error messages.
    :returns: The decoded document: a mapping, a list, or a scalar.
    :raises ConfigurationError: On unsupported or malformed syntax.
    """
    lines: list[tuple[int, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise ConfigurationError(
                f"{origin}:{number}: tabs may not be used for indentation",
                details={"line": number, "origin": origin},
            )
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        if stripped.strip() == "---":
            continue
        if stripped.strip() == "...":
            break
        lines.append((number, stripped.rstrip()))
    if not lines:
        return {}
    parser = _Parser(lines, origin)
    value = parser.parse_block(0)
    parser.ensure_consumed()
    return value


def load_yaml_file(path: str) -> Any:
    """Read and parse a YAML file.

    :param path: Filesystem path.
    :raises ConfigurationError: When the file cannot be read or parsed.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        raise ConfigurationError(f"cannot read {path}: {error}", details={"path": path}) from error
    return load_yaml(text, origin=path)


def _strip_comment(line: str) -> str:
    """Remove a trailing comment, honouring single and double quotes."""
    in_single = False
    in_double = False
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if in_double and character == "\\":
            escaped = True
            continue
        if not in_single and character == '"':
            in_double = not in_double
            continue
        if not in_double and character == "'":
            in_single = not in_single
            continue
        if character == "#" and not in_single and not in_double:
            if index == 0 or line[index - 1].isspace():
                return line[:index]
    return line


class _Parser:
    """Indentation-driven parser over pre-stripped lines."""

    def __init__(self, lines: list[tuple[int, str]], origin: str) -> None:
        self._lines = lines
        self._origin = origin
        self._index = 0

    def ensure_consumed(self) -> None:
        """Raise when lines remain that the block parser did not consume."""
        if self._index < len(self._lines):
            number, _ = self._lines[self._index]
            self._fail(number, "unexpected content after the document root")

    def _fail(self, number: int, message: str) -> "ConfigurationError":
        raise ConfigurationError(
            f"{self._origin}:{number}: {message}",
            details={"line": number, "origin": self._origin},
        )

    def _peek(self) -> tuple[int, str, int] | None:
        """Return ``(line_number, content, indent)`` for the next line."""
        if self._index >= len(self._lines):
            return None
        number, content = self._lines[self._index]
        indent = len(content) - len(content.lstrip(" "))
        return number, content.strip(), indent

    def parse_block(self, indent: int) -> Any:
        """Parse the block whose members sit at ``indent``."""
        head = self._peek()
        if head is None:
            return {}
        number, content, current_indent = head
        if current_indent < indent:
            return {}
        if content.startswith("- "):
            return self.parse_sequence(current_indent)
        if current_indent > indent:
            self._fail(number, "unexpected indentation")
        return self.parse_mapping(current_indent)

    def parse_mapping(self, indent: int) -> dict[str, Any]:
        """Parse a mapping whose keys sit at ``indent``."""
        mapping: dict[str, Any] = {}
        while True:
            head = self._peek()
            if head is None:
                break
            number, content, current_indent = head
            if current_indent < indent:
                break
            if current_indent > indent:
                self._fail(number, "unexpected indentation inside a mapping")
            if content.startswith("- "):
                break
            key, separator, remainder = _split_key(content)
            if not separator:
                self._fail(number, "expected 'key: value'")
            if key in mapping:
                self._fail(number, f"duplicate key {key!r}")
            self._index += 1
            if remainder == "":
                nested = self._peek()
                if nested is None or nested[2] < indent or (nested[2] == indent and not nested[1].startswith("-")):
                    # No deeper member: this key holds an empty mapping. A sibling
                    # mapping key at the same indent or any shallower line ends it.
                    mapping[key] = {}
                    continue
                mapping[key] = self.parse_block(nested[2])
                continue
            mapping[key] = _parse_scalar(remainder, number, self._fail)
        return mapping

    def parse_sequence(self, indent: int) -> list[Any]:
        """Parse a sequence whose items sit at ``indent``."""
        items: list[Any] = []
        while True:
            head = self._peek()
            if head is None:
                break
            number, content, current_indent = head
            if current_indent < indent:
                break
            if current_indent != indent or not content.startswith("-"):
                break
            body = content[1:].strip()
            self._index += 1
            if not body:
                nested = self._peek()
                if nested is None or nested[2] <= indent:
                    items.append(None)
                    continue
                items.append(self.parse_block(nested[2]))
                continue
            if ":" in body and not _looks_like_scalar(body):
                items.append(self._parse_inline_mapping(body, number, indent))
                continue
            if body.startswith(("{",)) or body.startswith(_UNSUPPORTED_PREFIXES):
                self._fail(number, f"unsupported sequence value {body!r}")
            items.append(_parse_scalar(body, number, self._fail))
        return items

    def _parse_inline_mapping(self, body: str, number: int, indent: int) -> dict[str, Any]:
        """Parse ``- key: value`` plus any deeper members of the same item.

        Deeper members may be mappings (``max_level: P2``) or a nested sequence
        (``allowed_targets:`` followed by ``- unit.service`` lines). The deeper
        block is re-parsed by a sub-parser over the collected lines, so both forms
        use the same mapping and sequence code paths.
        """
        key, separator, remainder = _split_key(body)
        if not separator:
            self._fail(number, "expected 'key: value' inside a sequence item")
        item: dict[str, Any] = {}
        if remainder == "":
            nested = self._peek()
            if nested is not None and nested[2] > indent:
                item[key] = self.parse_block(nested[2])
            else:
                item[key] = {}
        else:
            item[key] = _parse_scalar(remainder, number, self._fail)
        deferred: list[tuple[int, str]] = []
        while True:
            head = self._peek()
            if head is None:
                break
            next_number, content, current_indent = head
            if current_indent <= indent:
                break
            deferred.append((next_number, content))
            self._index += 1
        if deferred:
            base_indent = min(len(content) - len(content.lstrip(" ")) for _, content in deferred)
            sub = _Parser(deferred, self._origin)
            sub._index = 0
            extra = sub.parse_block(base_indent)
            sub.ensure_consumed()
            if not isinstance(extra, dict):
                self._fail(number, "nested content inside a sequence item must be a mapping")
            for extra_key, extra_value in extra.items():
                if extra_key in item:
                    self._fail(number, f"duplicate key {extra_key!r} inside a sequence item")
                item[extra_key] = extra_value
        return item


def _looks_like_scalar(body: str) -> bool:
    """Return whether ``body`` is plainly a scalar rather than a mapping start.

    A mapping start is ``key:`` or ``key: value``; a URL or a sentence containing
    a colon is not. The heuristic requires the text before the colon to look like
    a key.
    """
    key, separator, _ = _split_key(body)
    if not separator:
        return True
    return not bool(re.fullmatch(r"[A-Za-z0-9_.-]+", key))


def _split_key(content: str) -> tuple[str, bool, str]:
    """Split ``key: value`` at the first colon outside quotes."""
    in_single = False
    in_double = False
    escaped = False
    for index, character in enumerate(content):
        if escaped:
            escaped = False
            continue
        if in_double and character == "\\":
            escaped = True
            continue
        if not in_single and character == '"':
            in_double = not in_double
            continue
        if not in_double and character == "'":
            in_single = not in_single
            continue
        if character == ":" and not in_single and not in_double:
            key = content[:index].strip()
            remainder = content[index + 1 :].strip()
            if key.startswith(('"', "'")):
                key = key.strip("\"'")
            return key, True, remainder
    return content.strip(), False, ""


def _parse_scalar(text: str, number: int, fail: Any) -> Any:
    """Parse a scalar, or a single-line flow collection of scalars.

    ``[]`` and ``[a, b]`` are accepted because writing an empty policy allowlist
    inline is the natural YAML spelling. Nested flow collections are refused: a
    policy document that needs them should be written in block form so that a
    reader can see its structure.
    """
    if text.startswith("{"):
        fail(number, f"flow mappings are not part of the policy format: {text!r}")
    if text.startswith("["):
        return _parse_flow_sequence(text, number, fail)
    if text.startswith(_UNSUPPORTED_PREFIXES):
        fail(number, f"unsupported YAML feature in value {text!r}")
    if text.startswith('"'):
        return _parse_double_quoted(text, number, fail)
    if text.startswith("'"):
        if len(text) < 2 or not text.endswith("'"):
            fail(number, "unterminated single-quoted string")
        return text[1:-1].replace("''", "'")
    if text in {"null", "~", "Null", "NULL"}:
        return None
    if text in {"true", "True", "TRUE"}:
        return True
    if text in {"false", "False", "FALSE"}:
        return False
    if _PLAIN_INT.match(text):
        return int(text)
    if _PLAIN_FLOAT.match(text):
        return float(text)
    return text


def _parse_flow_sequence(text: str, number: int, fail: Any) -> list[Any]:
    """Parse a single-line ``[a, b]`` sequence of scalars."""
    if not text.endswith("]"):
        fail(number, f"unterminated flow sequence {text!r}")
    body = text[1:-1].strip()
    if not body:
        return []
    items: list[Any] = []
    for chunk in body.split(","):
        candidate = chunk.strip()
        if not candidate:
            continue
        if candidate.startswith(("{", "[")):
            fail(number, f"nested flow collections are not part of the policy format: {candidate!r}")
        items.append(_parse_scalar(candidate, number, fail))
    return items


def _parse_double_quoted(text: str, number: int, fail: Any) -> str:
    """Parse a double-quoted scalar with the common escape sequences."""
    if len(text) < 2 or not text.endswith('"'):
        fail(number, "unterminated double-quoted string")
    body = text[1:-1]
    escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "0": "\0"}
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
            fail(number, "dangling escape sequence")
        escape = body[index]
        if escape in escapes:
            result.append(escapes[escape])
            index += 1
            continue
        fail(number, f"unsupported escape sequence \\{escape}")
    return "".join(result)


__all__ = ["load_yaml", "load_yaml_file"]
