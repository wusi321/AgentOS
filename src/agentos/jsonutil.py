"""JSON helpers shared by the event bus, IPC, audit store, and runtime adapters.

The functions here exist to keep three guarantees in one place:

* every value written to durable storage or the wire is JSON-serializable;
* sensitive keys are redacted before they reach a log, an event payload, or the
  audit database;
* approval digests are computed from canonical JSON so that parameter order and
  formatting can never change an approval's identity.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from typing import Any, Final

JsonValue = Any

#: Substrings that mark a key as sensitive. Compared case-insensitively.
SENSITIVE_KEY_MARKERS: Final[tuple[str, ...]] = (
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "auth_token",
    "bearer",
    "client_secret",
    "credential",
    "cookie",
    "dsn",
    "passwd",
    "password",
    "private_key",
    "secret",
    "session_token",
    "token",
)

#: Replacement text written wherever a sensitive value was removed.
REDACTED: Final = "<redacted>"

#: Maximum serialized bytes an event payload or tool result may occupy.
MAX_PAYLOAD_BYTES: Final = 64 * 1024
#: Maximum nesting depth accepted when validating an untrusted JSON value.
MAX_JSON_DEPTH: Final = 32


def looks_sensitive(key: str) -> bool:
    """Return whether ``key`` names a value that must never be persisted raw."""
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def redact(value: JsonValue, *, depth: int = 0) -> JsonValue:
    """Return a copy of ``value`` with sensitive members replaced.

    :param value: Any JSON-compatible value; unknown objects are stringified.
    :param depth: Current recursion depth, guarded by :data:`MAX_JSON_DEPTH`.
    :returns: A JSON-serializable copy safe for logs, events, and audit rows.
    """
    if depth >= MAX_JSON_DEPTH:
        return "<depth-limit>"
    if isinstance(value, Mapping):
        cleaned: dict[str, JsonValue] = {}
        for member_key, member_value in value.items():
            name = str(member_key)
            if looks_sensitive(name):
                cleaned[name] = REDACTED
            else:
                cleaned[name] = redact(member_value, depth=depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [redact(item, depth=depth + 1) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    return str(value)


def is_json_value(value: JsonValue, *, depth: int = 0) -> bool:
    """Return whether ``value`` is representable as JSON within the depth bound."""
    if depth >= MAX_JSON_DEPTH:
        return False
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and is_json_value(item, depth=depth + 1) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return all(is_json_value(item, depth=depth + 1) for item in value)
    return False


def canonical_json(value: JsonValue) -> str:
    """Return a canonical JSON encoding used for digests and equality checks."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: JsonValue) -> str:
    """Return the lowercase SHA-256 hex digest of the canonical encoding of ``value``."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def truncate_text(text: str, *, limit: int = 4096) -> str:
    """Return ``text`` shortened to ``limit`` characters with an explicit marker."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"...<truncated {len(text) - limit} chars>"


#: Maximum bytes a single string member may occupy before it is shortened.
MAX_STRING_BYTES: Final = 4096


def bounded_payload(payload: Mapping[str, JsonValue], *, limit: int = MAX_PAYLOAD_BYTES) -> dict[str, JsonValue]:
    """Return a redacted payload that fits inside ``limit`` serialized bytes.

    Sensitive members are replaced first, then every oversized string is
    shortened. If the result still does not fit, the whole payload is replaced
    by a summary carrying the original digest, so the audit trail keeps a
    verifiable reference to what was dropped rather than silently losing it.

    :param payload: Source mapping; redacted before it is measured.
    :param limit: Inclusive byte bound on the canonical encoding.
    """
    redacted: dict[str, JsonValue] = {}
    for member_key, member_value in payload.items():
        name = str(member_key)
        if looks_sensitive(name):
            redacted[name] = REDACTED
        else:
            redacted[name] = redact(member_value)
    shortened = _shorten_strings(redacted, MAX_STRING_BYTES)
    if not isinstance(shortened, Mapping):
        shortened = redacted
    encoded = canonical_json(shortened)
    if len(encoded.encode("utf-8")) <= limit:
        return dict(shortened)
    shorter = _shorten_strings(redacted, 256)
    if not isinstance(shorter, Mapping):
        shorter = redacted
    encoded = canonical_json(shorter)
    if len(encoded.encode("utf-8")) <= limit:
        return dict(shorter)
    return {
        "truncated": True,
        "original_digest": digest(payload),
        "original_bytes": len(encoded.encode("utf-8")),
        "key_count": len(payload),
        "keys": sorted(str(key) for key in payload)[:MAX_SUMMARY_KEYS],
    }


#: Maximum member names listed in a truncation summary.
MAX_SUMMARY_KEYS: Final = 50


def _shorten_strings(value: JsonValue, limit: int, *, depth: int = 0) -> JsonValue:
    """Return a copy of ``value`` with every string shorter than ``limit`` characters."""
    if depth >= MAX_JSON_DEPTH:
        return "<depth-limit>"
    if isinstance(value, str):
        return truncate_text(value, limit=limit)
    if isinstance(value, Mapping):
        return {str(key): _shorten_strings(item, limit, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_shorten_strings(item, limit, depth=depth + 1) for item in value]
    return value


def text_from_blocks(blocks: Iterable[JsonValue]) -> str:
    """Join the visible text of model content blocks.

    Accepts the DSH content-block vocabulary (``text`` and ``reasoning`` blocks)
    and ignores every other member. Unknown block types are skipped rather than
    guessed at, because the vocabulary is merge-extensible upstream.
    """
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def reasoning_from_blocks(blocks: Iterable[JsonValue]) -> str:
    """Join the reasoning text of model content blocks."""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") != "reasoning":
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


__all__ = [
    "MAX_JSON_DEPTH",
    "MAX_PAYLOAD_BYTES",
    "MAX_STRING_BYTES",
    "REDACTED",
    "SENSITIVE_KEY_MARKERS",
    "bounded_payload",
    "canonical_json",
    "digest",
    "is_json_value",
    "looks_sensitive",
    "reasoning_from_blocks",
    "redact",
    "text_from_blocks",
    "truncate_text",
]
