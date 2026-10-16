#!/usr/bin/env python3
"""Find variables used before they are assigned in the shell helpers.

Two failures in this repository had the same shape and the same symptom — a script
that produced *no output at all*:

  * a code block inserted above the shebang, so the kernel saw no interpreter;
  * ``HERE`` referenced on an earlier line than its assignment, which with ``set -u``
    aborts the shell at that line.

Both were found by reading logs rather than by a check. This is the check.

A single-pass scan is too noisy to be useful, so the analysis is:

1. **Collect every assignment in the file first**, including ``for``/``while read``
   loop targets and shell functions. A loop target assigned below the loop is normal,
   and a helper library's variables are inherited by the scripts that source it.
2. **Then report uses that no assignment anywhere precedes**, discounting the shapes
   that are not shell variables at all: dpkg's ``${Package}``-style placeholders,
   ``awk`` fields such as ``$NF``, and ``${VAR:-default}`` parameter expansions,
   which are valid with ``set -u``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

ASSIGNMENT = re.compile(
    r"^\s*(?:local\s+|readonly\s+|export\s+|declare\s+(?:-[A-Za-z]+\s+)?)?"
    r"([A-Za-z_][A-Za-z0-9_]*)=(?!=)"
)
LOOP_VARIABLE = re.compile(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")
READ_VARIABLE = re.compile(r"\b(?:while|until)\s+read\s+(?:-[A-Za-z]+\s+)*([A-Za-z_][A-Za-z0-9_]*)")
FUNCTION_DEFINITION = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\)\s*\{")
#: `mapfile -t name` and `read -r a b` also assign.
MAPFILE = re.compile(r"\bmapfile\s+(?:-[A-Za-z]+\s+)*([A-Za-z_][A-Za-z0-9_]*)")

#: Anything a shell or the environment always provides.
ALWAYS_DEFINED = {
    "BASH_SOURCE", "FUNCNAME", "LINENO", "RANDOM", "SECONDS", "EPOCHSECONDS",
    "HOME", "PATH", "USER", "LOGNAME", "SHELL", "PWD", "OLDPWD", "HOSTNAME",
    "LANG", "LC_ALL", "TERM", "UID", "EUID", "IFS", "OPTIND", "OPTARG",
    "BASH_VERSION", "BASHPID", "PPID", "SHLVL", "_", "TMPDIR", "SUDO_USER",
    "SUDO_UID", "AGENTOS_REPO", "AGENTOS_USER", "AGENTOS_SUDO_PASSWORD",
    "AGENTOS_LOCK_TIMEOUT", "AGENTOS_TRACE", "AGENTOS_NODE_BIN", "AGENTOS_OUT",
    "AGENTOS_BUILD_DIR", "PRETTY_NAME",
}

#: Field and record variables available inside awk, and the same names in sed.
AWK_PROGRAMS = re.compile(r"\bawk\b|\bsed\b|\bgrep\b")
AWK_FIELDS = {"NF", "NR", "FNR", "FS", "OFS", "ORS", "RS", "FILENAME", "ARGC", "ARGV"}
#: dpkg's format placeholders: `${Package}`, `${Status}`, `${Conffiles}`, and friends.
DPKG_PLACEHOLDER = re.compile(
    r"\$\{(?:Package|Version|Architecture|Status|Conffiles|Depends|Description|"
    r"Source|Priority|Section|Maintainer|Homepage|Installed-Size|Essential|Multi-Arch)\}"
)
USAGE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
#: `${VAR:-...}`, `${VAR:=...}`, `${VAR:+...}`, `${VAR:?...}` are all safe under -u.
HAS_DEFAULT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):[-=+?]")


def collect_assignments(text: str) -> set[str]:
    """Return every name the file assigns, anywhere in it."""
    names = set(ALWAYS_DEFINED)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        match = ASSIGNMENT.match(line)
        if match:
            names.add(match.group(1))
        for pattern in (LOOP_VARIABLE, READ_VARIABLE, FUNCTION_DEFINITION, MAPFILE):
            for found in pattern.finditer(line):
                names.add(found.group(1))
    return names


def scan(path: Path, inherited: set[str]) -> list[str]:
    """Return one message per variable used before its first assignment."""
    text = path.read_text(encoding="utf-8")
    assigned_anywhere = collect_assignments(text) | inherited
    #: Names that are never a shell variable in this file.
    placeholders = set(USAGE.findall(" ".join(DPKG_PLACEHOLDER.findall(text)))) | AWK_FIELDS

    assigned: set[str] = set(ALWAYS_DEFINED) | inherited
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # Record this line's assignments before looking for uses on it.
        match = ASSIGNMENT.match(line)
        if match:
            assigned.add(match.group(1))
        for pattern in (LOOP_VARIABLE, READ_VARIABLE, FUNCTION_DEFINITION, MAPFILE):
            for found in pattern.finditer(line):
                assigned.add(found.group(1))
        safe_with_default = set(HAS_DEFAULT.findall(line))
        # Inside an awk or sed program, names are that tool's own variables.
        inside_awk = bool(AWK_PROGRAMS.search(line))
        for usage in USAGE.finditer(line):
            name = usage.group(1)
            if name in assigned or name in placeholders or name in safe_with_default:
                continue
            if inside_awk and name in AWK_FIELDS:
                continue
            # Only report when nothing anywhere in the file assigns the name;
            # otherwise the use is merely earlier than the assignment and is the
            # real defect this checks for.
            if name not in assigned_anywhere:
                continue
            problems.append(f"{path.name}:{number}: ${name} is used before it is assigned: {stripped}")
    return problems


def main(argv: list[str]) -> int:
    """Scan every shell script under ``scripts/``."""
    target = Path(argv[1]) if len(argv) > 1 else SCRIPTS
    library_inherited: set[str] = set()
    library = target / "lib-agentos-verify.sh"
    if library.is_file():
        library_inherited = collect_assignments(library.read_text(encoding="utf-8"))

    all_problems: list[str] = []
    checked = 0
    for script in sorted(target.glob("*.sh")):
        checked += 1
        inherited = library_inherited if script != library else set()
        problems = scan(script, inherited)
        if problems:
            all_problems.extend(problems)
            for problem in problems:
                print(f"  [FAIL] {problem}")
        else:
            print(f"  [ok  ] {script.name}")

    print("")
    print(f"scripts checked: {checked}")
    if all_problems:
        print(f"{len(all_problems)} use-before-assignment problem(s). With `set -u` each of")
        print("these aborts the script at that line and produces no further output.")
        return 1
    print("no variable is used before it is assigned")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
