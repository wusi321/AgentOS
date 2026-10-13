"""Source-level guards against defect classes that runtime tests cannot see.

These checks read the source tree. They exist because the defects they prevent
are silent: a payload that keeps a credential, or an ``isinstance`` check that
answers ``False`` for a real ``dict``, produces no error at all.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE_ROOT = REPO_ROOT / "src" / "agentos"

#: Names whose ``typing`` alias must never be used in a runtime ``isinstance``.
RUNTIME_ABC_NAMES = frozenset(
    {"Awaitable", "Callable", "Iterable", "Iterator", "Mapping", "MutableMapping", "Sequence", "Set"}
)


def python_sources() -> list[Path]:
    """Return every shipped module."""
    return sorted(SOURCE_ROOT.rglob("*.py"))


def test_source_tree_is_present() -> None:
    """The guard tests only mean something if they can see the sources."""
    assert python_sources(), f"no modules found under {SOURCE_ROOT}"


@pytest.mark.parametrize("path", python_sources(), ids=lambda path: path.name)
def test_no_typing_aliases_in_isinstance(path: Path) -> None:
    """``typing`` collection aliases must not be used for runtime type checks.

    On CPython 3.12 an ``isinstance`` against ``typing.Mapping`` can compare
    against a deprecated cached copy and answer ``False`` for a genuine ``dict``,
    silently disabling the branch the check guards. The canonical ABCs from
    ``collections.abc`` are the supported spelling.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    typing_names: set[str] = set()
    abc_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            typing_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module == "collections.abc":
            abc_names.update(alias.name for alias in node.names)
    suspect = typing_names & RUNTIME_ABC_NAMES
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "isinstance"):
            continue
        for argument in node.args[1:]:
            names = [argument.id] if isinstance(argument, ast.Name) else []
            if isinstance(argument, ast.Tuple):
                names = [element.id for element in argument.elts if isinstance(element, ast.Name)]
            offending = sorted(name for name in names if name in suspect)
            assert not offending, (
                f"{path.relative_to(REPO_ROOT)}: isinstance() uses the typing alias "
                f"{offending}; import it from collections.abc instead"
            )


@pytest.mark.parametrize("path", python_sources(), ids=lambda path: path.name)
def test_no_shell_execution_in_source(path: Path) -> None:
    """AgentOS never shells out with a caller-controlled command string.

    The rule is narrower than "no subprocess": the System Bridge legitimately runs
    ``systemctl`` and ``journalctl`` with a fixed argv. What is forbidden is
    ``shell=True`` and the ``os.system``/``os.popen`` family, because those turn a
    parameter into a command.
    """
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"system", "popen", "spawnvp", "execvp"}:
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "os":
                    raise AssertionError(f"{path.relative_to(REPO_ROOT)}: os.{node.func.attr}() is forbidden")
            for keyword in node.keywords:
                if keyword.arg == "shell":
                    raise AssertionError(f"{path.relative_to(REPO_ROOT)}: subprocess shell= is forbidden")


def test_no_network_listener_in_source() -> None:
    """No module binds a TCP address; the IPC transport is a Unix socket."""
    for path in python_sources():
        text = path.read_text(encoding="utf-8")
        assert "AF_INET" not in text, f"{path.relative_to(REPO_ROOT)}: network sockets are not part of AgentOS"
        assert "0.0.0.0" not in text, f"{path.relative_to(REPO_ROOT)}: binding a wildcard address is forbidden"
        assert "HTTPServer" not in text, f"{path.relative_to(REPO_ROOT)}: no HTTP server is exposed"


def test_every_module_has_a_docstring() -> None:
    """A module without a docstring is a module nobody can review."""
    missing: list[str] = []
    for path in python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if not ast.get_docstring(tree):
            missing.append(str(path.relative_to(REPO_ROOT)))
    assert not missing, "modules without a module docstring: " + ", ".join(missing)


def test_every_public_callable_has_a_docstring() -> None:
    """Public module-level and class-level callables document their contract.

    Nested helpers are exempt: they are private by construction, since they
    cannot be reached from outside the callable that defines them. GObject
    virtual-method overrides (``do_*``) are exempt because they are dispatched by
    the toolkit and documented at the base class.
    """
    missing: list[str] = []
    for path in python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        top_level: list[ast.AST] = list(tree.body)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                top_level.extend(node.body)
        for node in top_level:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith(("_", "do_")):
                continue
            if ast.get_docstring(node) is None:
                missing.append(f"{path.relative_to(REPO_ROOT)}::{node.name}")
    assert not missing, "public callables without a docstring: " + ", ".join(missing)


def test_no_pydantic_dependency_in_shipped_code() -> None:
    """The shipped package must run on a bare system Python."""
    offenders: list[str] = []
    for path in python_sources():
        text = path.read_text(encoding="utf-8")
        if "pydantic" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, "shipped modules must not import pydantic: " + ", ".join(offenders)


def test_no_todo_or_fixme_markers_in_shipped_code() -> None:
    """Shipped modules are complete; unfinished work is tracked outside the source."""
    offenders: list[str] = []
    for path in python_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("#", '"', "'")) and ("TODO" in stripped or "FIXME" in stripped or "XXX" in stripped):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert not offenders, "unfinished markers in shipped code: " + ", ".join(offenders)


def test_files_end_with_one_newline() -> None:
    """A missing or doubled trailing newline breaks diffs and patch tools."""
    offenders: list[str] = []
    for path in python_sources():
        text = path.read_text(encoding="utf-8")
        if not text.endswith("\n") or text.endswith("\n\n"):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, "files without exactly one trailing newline: " + ", ".join(offenders)


def test_no_byte_order_mark_in_shipped_code() -> None:
    """A BOM breaks strict parsers, including the policy and YAML readers."""
    offenders: list[str] = []
    for path in python_sources():
        if path.read_bytes()[:3] == b"\xef\xbb\xbf":
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, "files with a byte order mark: " + ", ".join(offenders)
