#!/usr/bin/env python3
"""Report the outcome of a verification run.

Reading a long verification log by hand invites mistakes, and shelling out with a
pattern that contains a pipe is easy to get wrong from another shell. This reads the
log directly and prints the stage results, plus any check that failed and any
diagnostic block a failure produced.

Usage: summarize-verification.py <log> [--failures-only]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

STAGE = re.compile(r"^== (?P<label>.+)$")
RESULT = re.compile(r"^-- (?P<outcome>passed|FAILED): (?P<label>.+)$")
CHECK = re.compile(r"^\s*\[(?P<mark>ok\s*|FAIL|note)\]\s*(?P<label>.*)$")
TOTAL = re.compile(r"^(?P<count>\d+) .*(?:failed|check\(s\) failed)$")


def main(argv: list[str]) -> int:
    """Print the stage results and every failure from a verification log."""
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(f"no such log: {path}", file=sys.stderr)
        return 2
    failures_only = "--failures-only" in argv
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    stages: list[tuple[str, str]] = []
    failures: list[str] = []
    notes: list[str] = []
    totals: list[str] = []
    diagnostics: list[str] = []
    in_diagnostics = False

    for line in lines:
        result = RESULT.match(line)
        if result:
            stages.append((result.group("label"), result.group("outcome")))
            continue
        if STAGE.match(line):
            in_diagnostics = False
            continue
        if "--- why:" in line:
            in_diagnostics = True
            diagnostics.append(line.rstrip())
            continue
        if in_diagnostics:
            if line.startswith("== "):
                in_diagnostics = False
            elif line.strip():
                diagnostics.append(line.rstrip())
                continue
        check = CHECK.match(line)
        if check:
            mark = check.group("mark").strip()
            if mark == "FAIL":
                failures.append(line.rstrip())
            elif mark == "note":
                notes.append(line.rstrip())
            continue
        total = TOTAL.match(line.strip())
        if total:
            totals.append(line.strip())

    print("stage results")
    for label, outcome in stages:
        marker = "ok  " if outcome == "passed" else "FAIL"
        print(f"  [{marker}] {label}")

    if notes and not failures_only:
        print("")
        print("notes")
        for note in notes:
            print(f"  {note}")

    if failures:
        print("")
        print("failures")
        for failure in failures:
            print(f"  {failure}")
    if diagnostics:
        print("")
        print("diagnostics collected at the failure point")
        for line in diagnostics:
            print(f"  {line}")
    if totals:
        print("")
        for total in totals:
            print(f"  {total}")

    failed = sum(1 for _, outcome in stages if outcome != "passed")
    print("")
    print(f"stages: {len(stages)}, failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
