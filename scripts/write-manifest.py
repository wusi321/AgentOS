#!/usr/bin/env python3
"""Write the release manifest for a build.

The manifest is what makes a build traceable: the artifacts and their checksums,
the source revision they came from, the locked runtime revision, and the results of
the verification stages. It is the artifact an operator reads when they need to
know exactly what they are installing.

Usage: write-manifest.py <artifact-directory> [--revision <git-describe>]
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agentos.constants import (  # noqa: E402
    DSH_LOCKED_COMMIT,
    DSH_LOCKED_VERSION,
    DSH_NODE_ENGINES,
    PROJECT_NAME,
    PROJECT_VERSION,
    STORAGE_SCHEMA_VERSION,
    TARGET_UBUNTU_MIN,
)


def sha256(path: Path) -> str:
    """Return the lowercase SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_output(argv: list[str]) -> str:
    """Return a command's stdout, or an explanation when it cannot run."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            cwd=str(ROOT),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"unavailable: {error}"
    if completed.returncode != 0:
        return f"unavailable: exit {completed.returncode}"
    return completed.stdout.strip()


def collect_artifacts(directory: Path) -> list[dict[str, object]]:
    """Return one manifest entry per package."""
    entries: list[dict[str, object]] = []
    for deb in sorted(directory.glob("agentos-*.deb")):
        package = command_output(["dpkg-deb", "-f", str(deb), "Package"])
        version = command_output(["dpkg-deb", "-f", str(deb), "Version"])
        architecture = command_output(["dpkg-deb", "-f", str(deb), "Architecture"])
        depends = command_output(["dpkg-deb", "-f", str(deb), "Depends"])
        contents = command_output(["dpkg-deb", "--contents", str(deb)])
        file_count = sum(1 for line in contents.splitlines() if not line.rstrip().endswith("/"))
        entries.append(
            {
                "package": package,
                "version": version,
                "architecture": architecture,
                "depends": depends,
                "file": deb.name,
                "size_bytes": deb.stat().st_size,
                "sha256": sha256(deb),
                "installed_files": file_count,
            }
        )
    return entries


def main(argv: list[str]) -> int:
    """Write ``MANIFEST.json`` next to the artifacts."""
    arguments = argv[1:]
    if not arguments:
        print(__doc__, file=sys.stderr)
        return 2
    directory = Path(arguments[0])
    revision = "unknown"
    if "--revision" in arguments:
        revision = arguments[arguments.index("--revision") + 1]
    else:
        revision = command_output(["git", "describe", "--tags", "--always", "--dirty"])

    if not directory.is_dir():
        print(f"no such directory: {directory}", file=sys.stderr)
        return 2
    artifacts = collect_artifacts(directory)
    if not artifacts:
        print(f"no agentos-*.deb in {directory}", file=sys.stderr)
        return 1

    manifest = {
        "project": PROJECT_NAME,
        "project_version": PROJECT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source_revision": revision,
        "target_platform": {
            "distribution": f"Ubuntu {TARGET_UBUNTU_MIN} LTS Desktop or later",
            "architecture": "x86_64",
            "python": ">= 3.10 (the distribution system interpreter)",
            "gnome_shell": "42 and later (the extension declares 42 through 44)",
        },
        "storage_schema_version": STORAGE_SCHEMA_VERSION,
        "runtime": {
            "first_adapter": "dsh",
            "locked_version": DSH_LOCKED_VERSION,
            "locked_commit": DSH_LOCKED_COMMIT,
            "node_engines": DSH_NODE_ENGINES,
            "redistributed": False,
        },
        "artifacts": artifacts,
        "install": "sudo dpkg -i agentos-core_*.deb agentos-runtime-dsh_*.deb agentos-desktop_*.deb agentos-gnome-extension_*.deb agentos-meta_*.deb",
        "verification": [
            "python3 -m pytest -q tests",
            "python3 scripts/check-python-compat.py --target 3.10",
            "python3 scripts/check-packaging.py",
            "python3 scripts/check-maintainer-arguments.py",
            "bash scripts/check-packaged-scripts.sh build/debs",
            "bash scripts/verify-debs.sh build/debs",
            "python3 scripts/smoke-core.py",
            "bash scripts/verify-installed.sh build/debs   (needs root)",
            "bash scripts/verify-data-retention.sh build/debs   (needs root)",
            "bash scripts/verify-live.sh build/debs   (needs root)",
        ],
        "known_limitations": [
            "the DSH SDK profile has no cancel method; a cancelled turn is stopped by restarting the runtime process, and the task is marked CANCELLED",
            "the DSH SDK profile sends no approval notification; AgentOS policy is the only approval authority",
            "no privileged executor: P3 administrative operations are declared and denied",
            "the ISO deliverable is not part of this release",
            "the GNOME extension has not been exercised on a real GNOME Shell 42 session",
        ],
    }

    target = directory / "MANIFEST.json"
    target.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {target}")
    print(f"  packages: {len(artifacts)}")
    for entry in artifacts:
        print(f"    {entry['package']:<26} {entry['sha256'][:16]}…  {entry['installed_files']} files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
