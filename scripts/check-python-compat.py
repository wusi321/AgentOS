#!/usr/bin/env python3
"""Check that the shipped code parses and imports under the target Python version.

Ubuntu 22.04 ships Python 3.10, while a development host may run 3.12. A syntax
feature or standard-library addition newer than 3.10 would pass every test here and
fail on the target platform, so the minimum supported version is checked
separately, by compiling every module with a 3.10 grammar and by rejecting
stdlib additions introduced after 3.10.

Usage: check-python-compat.py [--target 3.10] [repo-root]
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

#: Standard-library names, and the version that introduced them, that must not be
#: used in shipped code because the target interpreter predates them.
POST_310_STDLIB: dict[str, str] = {
    "tomllib": "3.11 (use agentos.toml, which reads TOML itself on 3.10)",
    "ExceptionGroup": "3.11",
    "BaseExceptionGroup": "3.11",
    "TaskGroup": "3.11 (asyncio.TaskGroup)",
    "add_note": "3.11 (BaseException.add_note)",
    "typing.Self": "3.11",
    "typing.LiteralString": "3.11",
    "typing.Never": "3.11",
    "typing.assert_never": "3.11",
    "typing.assert_type": "3.11",
    "typing.dataclass_transform": "3.11",
    "typing.override": "3.12",
    "itertools.batched": "3.12",
    "asyncio.timeout": "3.11",
    "asyncio.timeout_at": "3.11",
    "asyncio.Runner": "3.11",
    "pathlib.Path.walk": "3.12",
    "shutil.rmtree.onexc": "3.12",
    "sqlite3.Connection.autocommit": "3.12",
    "enum.StrEnum": "3.11",
    "hashlib.file_digest": "3.11",
    "datetime.UTC": "3.11",
    "math.exp2": "3.11",
    "contextlib.chdir": "3.11",
    "sys.exception": "3.11",
    "re.NOFLAG": "3.11",
}

#: Syntax constructs parsed by 3.10 but not available as runtime behaviour.
SOFT_KEYWORDS_311 = ("except*",)


def feature_version(target: str) -> tuple[int, int]:
    """Return the ``(major, minor)`` pair for a version string."""
    major, _, minor = target.partition(".")
    return int(major), int(minor)


def module_files(root: Path) -> list[Path]:
    """Return every shipped module."""
    return sorted((root / "src" / "agentos").rglob("*.py"))


def check_syntax(paths: list[Path], target: tuple[int, int]) -> list[str]:
    """Return the files that do not parse under the target grammar."""
    problems: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        try:
            ast.parse(text, filename=str(path), feature_version=target)
        except SyntaxError as error:
            problems.append(f"{path}: does not parse as Python {target[0]}.{target[1]}: {error}")
    return problems


def guarded_names(text: str) -> set[str]:
    """Return the standard-library names a module may use on this interpreter.

    A name such as ``tomllib`` is legitimate on the target interpreter when the
    module imports it inside a ``try`` whose handler catches its absence, and then
    refers to it only through a local alias. Both the aliased name and the module
    name are accepted in that case, because the module name may still appear in a
    version check such as ``if _tomllib is not None``.

    The check therefore accepts that shape and rejects an unguarded import, which
    is the difference between a deliberate fallback and an accidental dependency.
    """
    tree = ast.parse(text)
    guarded: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        handles_missing = False
        for handler in node.handlers:
            handler_text = ast.dump(handler.type) if handler.type is not None else ""
            if "ModuleNotFoundError" in handler_text or "ImportError" in handler_text:
                handles_missing = True
        if not handles_missing:
            continue
        for statement in ast.walk(node):
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    guarded.add((alias.asname or alias.name).split(".")[0])
                    guarded.add(alias.name.split(".")[0])
            elif isinstance(statement, ast.ImportFrom) and statement.module:
                for alias in statement.names:
                    guarded.add(alias.asname or alias.name)
                guarded.add(statement.module.split(".")[0])
    return guarded


def check_stdlib(paths: list[Path]) -> list[str]:
    """Return the uses of a standard-library feature newer than the target."""
    problems: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        allowed = guarded_names(text)
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"'):
                continue
            for name, introduced in POST_310_STDLIB.items():
                if name.split(".")[0] in allowed:
                    continue
                # Match a whole dotted name, so `datetime.UTC` does not match
                # `datetime.utcnow` and `typing.Self` does not match `typing.SelfRef`.
                if re.search(rf"(?<![\w.]){re.escape(name)}(?![\w])", stripped):
                    problems.append(f"{path}:{line_number}: {name} requires Python {introduced}")
        for construct in SOFT_KEYWORDS_311:
            if construct in text:
                problems.append(f"{path}: uses the `{construct}` syntax, which is Python 3.11 and later")
    return problems


def check_tomllib_fallback(paths: list[Path]) -> list[str]:
    """Verify that the TOML reader has a fallback for Python 3.10.

    The shipped configuration and policy readers must work on an interpreter with
    no ``tomllib``, and the module that needs it must import it defensively.
    """
    problems: list[str] = []
    toml_module = Path()
    for path in paths:
        if path.name == "toml.py" and path.parent.name == "agentos":
            toml_module = path
            break
    if not toml_module:
        return ["the TOML reader was not found"]
    text = toml_module.read_text(encoding="utf-8")
    if "ModuleNotFoundError" not in text or "_tomllib is not None" not in text:
        problems.append(f"{toml_module}: the tomllib import is not guarded for Python 3.10")
    return problems


def main(argv: list[str]) -> int:
    """Run the compatibility checks."""
    target_text = "3.10"
    root = Path(__file__).resolve().parent.parent
    arguments = argv[1:]
    while arguments:
        item = arguments.pop(0)
        if item == "--target" and arguments:
            target_text = arguments.pop(0)
        else:
            root = Path(item)
    target = feature_version(target_text)
    paths = module_files(root)
    if not paths:
        print(f"no modules found under {root / 'src' / 'agentos'}", file=sys.stderr)
        return 2

    failures: list[str] = []
    for label, problems in (
        (f"grammar parses as Python {target_text}", check_syntax(paths, target)),
        (f"no standard-library feature newer than Python {target_text}", check_stdlib(paths)),
        ("the TOML reader falls back without tomllib", check_tomllib_fallback(paths)),
    ):
        if problems:
            printf = print
            printf(f"[FAIL] {label}")
            for problem in problems:
                printf(f"       {problem}")
            failures.extend(problems)
        else:
            print(f"[ok  ] {label}")

    print("")
    print(f"modules checked: {len(paths)}")
    if failures:
        print(f"{len(failures)} compatibility problem(s)")
        return 1
    print(f"the shipped package is compatible with Python {target_text} and later")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
