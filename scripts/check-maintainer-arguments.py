#!/usr/bin/env python3
"""Check that every maintainer script handles the arguments dpkg actually passes.

dpkg calls maintainer scripts with a documented, finite set of arguments, and it
does so at moments the package author does not control: during an upgrade of the
*old* package, after a failed upgrade, and while cleaning up an aborted one. A
script that refuses an argument it will really receive makes every upgrade of the
package fail, which is exactly the failure this check exists to prevent.

The rules encoded here are from Debian Policy §6.5:

* ``postinst`` receives ``configure``, ``abort-upgrade``, ``abort-remove``,
  ``abort-deconfigure`` and, on a newer dpkg, a second argument.
* ``prerm`` receives ``remove``, ``upgrade``, ``deconfigure``,
  ``failed-upgrade`` and ``abort-install``.
* ``postrm`` receives ``remove``, ``purge``, ``upgrade``, ``disappear``,
  ``failed-upgrade``, ``abort-install`` and ``abort-upgrade``.

Usage: check-maintainer-arguments.py [repo-root]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Arguments each maintainer script must be able to receive without failing.
REQUIRED_ARGUMENTS: dict[str, frozenset[str]] = {
    "postinst": frozenset({"configure", "abort-upgrade", "abort-remove", "abort-deconfigure"}),
    "prerm": frozenset({"remove", "upgrade", "deconfigure", "failed-upgrade", "abort-install"}),
    "postrm": frozenset(
        {"remove", "purge", "upgrade", "disappear", "failed-upgrade", "abort-install", "abort-upgrade"}
    ),
}

#: Delegated whole-case bodies, which are correct and must not be flagged.
DELEGATED = re.compile(r"case\s+\"?\$\{?DEBHELPER\}?\"?", re.IGNORECASE)


def case_arms(text: str) -> set[str]:
    """Return every argument label the script's case statement accepts.

    Only the top-level dispatch is considered: a nested ``case`` on a different
    variable is a different decision and must not be read as covering ``$1``.
    """
    arms: set[str] = set()
    # Find each `case "$1" in ... esac` block, tolerating the $1 spelling dpkg
    # scripts use in practice.
    for match in re.finditer(r'case\s+"?\$1"?\s+in(.*?)\n\s*esac', text, re.DOTALL):
        body = match.group(1)
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.endswith(")"):
                label = stripped[:-1].strip()
                for part in label.split("|"):
                    part = part.strip().strip('"').strip("'")
                    if part:
                        arms.add(part)
    return arms


def check_script(path: Path) -> list[str]:
    """Return the problems found in one maintainer script."""
    problems: list[str] = []
    kind = path.name.rsplit(".", 1)[-1]
    required = REQUIRED_ARGUMENTS.get(kind)
    if required is None:
        return problems
    text = path.read_text(encoding="utf-8")

    if DELEGATED.search(text):
        # The script hands the whole dispatch to a generated helper; the helper
        # handles the full argument set, so this file is not at fault.
        return problems

    arms = case_arms(text)
    if not arms:
        problems.append(f"{path.name}: no 'case \"$1\" in' dispatch block was found")
        return problems

    if "*" in arms or "*)":
        # A wildcard arm swallows everything, but if it exits non-zero the script
        # still fails on the arguments dpkg really passes.
        wildcard_body = _wildcard_body(text)
        if "exit 1" in wildcard_body or "exit 2" in wildcard_body:
            missing = sorted(required - arms - {"*"})
            if missing:
                problems.append(
                    f"{path.name}: the wildcard arm exits non-zero but these arguments are not "
                    "handled explicitly: " + ", ".join(missing)
                )
        return problems

    missing = sorted(required - arms)
    if missing:
        problems.append(f"{path.name}: does not handle " + ", ".join(missing))
    return problems


def _wildcard_body(text: str) -> str:
    """Return the text of the wildcard arm of the top-level dispatch."""
    match = re.search(r'case\s+"?\$1"?\s+in(.*?)\n\s*esac', text, re.DOTALL)
    if match is None:
        return ""
    body = match.group(1)
    marker = body.find("*)")
    return body[marker:] if marker >= 0 else ""


def main(argv: list[str]) -> int:
    """Check every maintainer script under ``debian/``."""
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    debian = root / "debian"
    if not debian.is_dir():
        print(f"no debian directory under {root}", file=sys.stderr)
        return 2
    scripts = sorted(
        path
        for path in debian.iterdir()
        if path.is_file() and re.fullmatch(r"agentos-[a-z-]+\.(preinst|postinst|prerm|postrm)", path.name)
    )
    if not scripts:
        print("no maintainer scripts found", file=sys.stderr)
        return 2

    failures: list[str] = []
    for script in scripts:
        problems = check_script(script)
        if problems:
            failures.extend(problems)
            print(f"[FAIL] {script.name}")
            for problem in problems:
                print(f"       {problem}")
        else:
            arms = sorted(case_arms(script.read_text(encoding="utf-8")))
            print(f"[ok  ] {script.name} handles {len(arms)} argument label(s)")

    print("")
    if failures:
        print(f"{len(failures)} maintainer-script problem(s)")
        return 1
    print("every maintainer script handles the arguments dpkg passes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
