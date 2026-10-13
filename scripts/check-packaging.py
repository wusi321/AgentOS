#!/usr/bin/env python3
"""Verify the Debian packaging metadata before a build.

A missing source file in an ``.install`` list produces a confusing failure deep
inside ``dh_install``. This check reports the real problem first: every declared
source path, every maintainer script, every service and schema file, and the
internal consistency of ``debian/control``.

Run it from the repository root, or let ``scripts/build-debs.sh`` run it.
"""

from __future__ import annotations

import configparser
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEBIAN = ROOT / "debian"
#: Package names the build must produce, in the order control declares them.
EXPECTED_PACKAGES = (
    "agentos-core",
    "agentos-runtime-dsh",
    "agentos-desktop",
    "agentos-gnome-extension",
    "agentos-meta",
)


def check_control() -> list[str]:
    """Validate debian/control and return the package names it declares."""
    problems: list[str] = []
    path = DEBIAN / "control"
    if not path.is_file():
        return [f"{path} is missing"]
    text = path.read_text(encoding="utf-8")
    binaries = re.findall(r"^Package:\s*(\S+)\s*$", text, re.MULTILINE)
    if not text.lstrip().startswith("Source:"):
        problems.append("debian/control must start with a Source stanza")
    for section in ("Section:", "Priority:", "Maintainer:", "Build-Depends:", "Standards-Version:"):
        if section not in text:
            problems.append(f"debian/control is missing {section}")
    for name in EXPECTED_PACKAGES:
        if name not in binaries:
            problems.append(f"debian/control does not declare {name}")
    for name in binaries:
        if name not in EXPECTED_PACKAGES:
            problems.append(f"debian/control declares unexpected package {name}")
    # Every binary stanza needs the mandatory fields.
    for stanza in re.split(r"\n(?=Package:)", text):
        match = re.search(r"^Package:\s*(\S+)", stanza, re.MULTILINE)
        if match is None:
            continue
        for field in ("Architecture:", "Depends:", "Description:"):
            if not re.search(rf"^{field}", stanza, re.MULTILINE):
                problems.append(f"{match.group(1)} is missing {field}")
    return problems


def check_install_lists() -> list[str]:
    """Verify that every source path named by an .install file exists.

    A ``debian/<package>.install`` line is ``<source> [<destination>]``, where
    ``<source>`` is relative to the source tree root and ``<destination>`` is the
    directory inside the binary package. Both halves are checked here: a typo in
    either produces a failure deep inside ``dh_install`` that is hard to read.
    """
    problems: list[str] = []
    lists = sorted(DEBIAN.glob("*.install"))
    if not lists:
        return ["debian/*.install: no install lists found"]
    for install_list in lists:
        package = install_list.name[: -len(".install")]
        if package not in EXPECTED_PACKAGES:
            problems.append(f"{install_list.name}: no such package is declared in debian/control")
        for number, raw in enumerate(install_list.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) > 2:
                problems.append(
                    f"{install_list.name}:{number}: expected '<source> [<destination>]', got {line!r}"
                )
                continue
            source = parts[0]
            destination = parts[1] if len(parts) == 2 else ""
            if not _source_matches(source):
                problems.append(f"{install_list.name}:{number}: source {source} matches nothing")
                continue
            if destination and not destination.startswith(("usr/", "etc/", "lib/", "var/", "opt/")):
                problems.append(
                    f"{install_list.name}:{number}: destination {destination} is not an absolute "
                    "system location"
                )
                continue
            target = _resolve_target(source, destination)
            if not _is_expected_destination(package, target):
                problems.append(
                    f"{install_list.name}:{number}: {source} installs to {target}, which is not an "
                    f"expected location for {package}"
                )
    return problems


def _source_matches(source: str) -> bool:
    """Return whether an install-list source names at least one real path.

    A source may be a plain path or a glob. dh_install expands globs itself, so a
    pattern that matches nothing would be a silent omission from the package; the
    check therefore insists on at least one match rather than on the literal path
    existing.
    """
    if "*" not in source and "?" not in source and "[" not in source:
        return (ROOT / source).exists()
    candidate = ROOT / source
    try:
        return any(candidate.parent.glob(candidate.name))
    except (ValueError, OSError):
        return False


def _resolve_target(source: str, destination: str) -> str:
    """Return the full in-package path an install line produces.

    ``dh_install`` treats a destination ending in ``/`` as a directory, so
    ``bin/agentosctl usr/bin/`` lands at ``usr/bin/agentosctl``. Resolving that
    here means the location rules can be written as exact path fragments.
    """
    if not destination:
        return source
    if destination.endswith("/"):
        return destination + Path(source).name
    return destination


#: Leading destination fragments each package is allowed to install to.
ALLOWED_PREFIXES: dict[str, tuple[str, ...]] = {
    "agentos-core": (
        "usr/bin/agentosctl",
        "usr/lib/python3/dist-packages/agentos/",
        "usr/libexec/agentos/",
        "etc/agentos/",
        "lib/systemd/system/agentos-core",
        "usr/share/dbus-1/system.d/",
        "usr/share/doc/agentos-core/",
        "usr/share/man/man1/",
    ),
    "agentos-runtime-dsh": (
        "usr/lib/python3/dist-packages/agentos/runtime/",
        "usr/share/agentos/",
        "usr/share/doc/agentos-runtime-dsh/",
    ),
    "agentos-desktop": (
        "usr/bin/agentos-",
        "usr/lib/python3/dist-packages/agentos/desktop/",
        "usr/lib/python3/dist-packages/agentos/session/",
        "usr/share/applications/",
        "usr/share/icons/",
        "usr/share/dbus-1/services/",
        "usr/share/glib-2.0/schemas/",
        "lib/systemd/user/",
    ),
    "agentos-gnome-extension": (
        "usr/share/gnome-shell/extensions/",
        "usr/share/glib-2.0/schemas/",
        "usr/share/doc/agentos-gnome-extension/",
    ),
    "agentos-meta": ("usr/share/doc/agentos-meta/",),
}


def _is_expected_destination(package: str, source: str) -> bool:
    """Return whether ``source`` belongs in ``package``."""
    prefixes = ALLOWED_PREFIXES.get(package)
    if prefixes is None:
        return True
    return any(source.startswith(prefix) for prefix in prefixes)


def check_maintainer_scripts() -> list[str]:
    """Verify every maintainer script is well formed."""
    problems: list[str] = []
    scripts = sorted(
        path
        for path in DEBIAN.iterdir()
        if path.is_file() and re.fullmatch(r"agentos-[a-z-]+\.(preinst|postinst|prerm|postrm)", path.name)
    )
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        if not text.startswith("#!/bin/sh"):
            problems.append(f"{script.name} must use #!/bin/sh")
        if not script.stat().st_mode & 0o111:
            problems.append(f"{script.name} is not executable")
        if "#DEBHELPER#" not in text:
            problems.append(f"{script.name} is missing the #DEBHELPER# substitution")
        if "apt-get update" in text or "apt install" in text:
            problems.append(f"{script.name} must not run a package manager")
        if "curl " in text or "wget " in text:
            problems.append(f"{script.name} must not fetch anything from the network")
        match = re.search(r'case "\$1" in(.*?)esac', text, re.DOTALL)
        if match is None:
            problems.append(f"{script.name} has no 'case \"$1\" in' dispatch block")
            continue
        body = match.group(1)
        if "configure" not in body and "remove" not in body:
            problems.append(f"{script.name} dispatches on neither configure nor remove")
    expected = {
        "agentos-core.postinst",
        "agentos-core.prerm",
        "agentos-core.postrm",
        "agentos-desktop.postinst",
        "agentos-desktop.postrm",
        "agentos-gnome-extension.postinst",
    }
    found = {path.name for path in scripts}
    missing = sorted(expected - found)
    if missing:
        problems.append("missing maintainer script(s): " + ", ".join(missing))
    return problems


def check_changelog() -> list[str]:
    """Verify the changelog names the source package and a valid version."""
    problems: list[str] = []
    path = DEBIAN / "changelog"
    if not path.is_file():
        return ["debian/changelog is missing"]
    first = path.read_text(encoding="utf-8").splitlines()[0]
    match = re.match(r"^(\S+) \(([^)]+)\) (\S+); urgency=(\S+)$", first)
    if match is None:
        problems.append(f"debian/changelog first line is malformed: {first!r}")
        return problems
    if match.group(1) != "agentos":
        problems.append(f"changelog names {match.group(1)} but the source package is agentos")
    if re.search(r"-\d+$", match.group(2)):
        problems.append(
            "a 3.0 (native) package must not carry a Debian revision in its version: " + match.group(2)
        )
    return problems


def check_systemd_units() -> list[str]:
    """Verify the unit files declare what the packages promise."""
    problems: list[str] = []
    system_units = sorted((ROOT / "packaging" / "systemd").glob("*.service"))
    if not system_units:
        return ["packaging/systemd: no unit files found"]
    core = ROOT / "packaging" / "systemd" / "agentos-core.service"
    text = core.read_text(encoding="utf-8")
    for directive in ("User=agentos", "Group=agentos", "NoNewPrivileges=yes", "ProtectSystem=strict"):
        if directive not in text:
            problems.append(f"agentos-core.service is missing {directive}")
    if "root" in re.findall(r"^User=(\S+)", text, re.MULTILINE):
        problems.append("agentos-core.service must not run as root")
    session = ROOT / "packaging" / "systemd" / "agentos-session.service"
    session_text = session.read_text(encoding="utf-8")
    if "NoNewPrivileges=yes" not in session_text:
        problems.append("agentos-session.service is missing NoNewPrivileges=yes")
    if "WantedBy=graphical-session.target" not in session_text:
        problems.append("agentos-session.service must belong to the graphical session")
    return problems


def check_policy_files() -> list[str]:
    """Verify the shipped default policy parses and stays read-only."""
    problems: list[str] = []
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from agentos.errors import AgentOSError
        from agentos.policy import PolicyEngine
        from agentos.policy.engine import load_policy_directory
    except ImportError as error:
        return [f"cannot import the policy package: {error}"]
    directory = ROOT / "configs" / "policies"
    try:
        documents = load_policy_directory(str(directory))
    except AgentOSError as error:
        return [f"the shipped default policy does not load: {error}"]
    if not documents:
        problems.append(f"{directory} contains no loadable policy document")
        return problems
    engine = PolicyEngine(policies=documents)
    for tool in ("system.service.restart", "system.process.terminate"):
        evaluation = engine.evaluate(tool, {"unit": "sshd.service"} if "service" in tool else {"pid": 4242},
                                     _read_only_context())
        if evaluation.decision.value == "allow":
            problems.append(f"the shipped default policy allows {tool} without an approval")
    return problems


def _read_only_context():
    """Return a read-only policy context for the packaging check."""
    from agentos.policy import PolicyContext, PrivilegeLevel

    return PolicyContext(uid=1000, task_level_cap=PrivilegeLevel.CONTROLLED)


def check_gnome_extension() -> list[str]:
    """Verify the extension declares the UUID the packages install it under."""
    problems: list[str] = []
    metadata = ROOT / "gnome-extension" / "metadata.json"
    if not metadata.is_file():
        return ["gnome-extension/metadata.json is missing"]
    import json

    try:
        document = json.loads(metadata.read_text(encoding="utf-8"))
    except ValueError as error:
        return [f"gnome-extension/metadata.json is not valid JSON: {error}"]
    if document.get("uuid") != "agentos@agentos.dev":
        problems.append(
            f"the extension uuid is {document.get('uuid')!r}, but the package installs it as "
            "agentos@agentos.dev"
        )
    versions = document.get("shell-version", [])
    if not isinstance(versions, list) or not any(str(item).startswith("42") for item in versions):
        problems.append("the extension does not declare GNOME Shell 42 support")
    schema = ROOT / "gnome-extension" / "schemas" / "org.gnome.shell.extensions.agentos.gschema.xml"
    if not schema.is_file():
        problems.append("the extension schema is missing")
    elif document.get("settings-schema") not in schema.read_text(encoding="utf-8"):
        problems.append("the extension's settings-schema id does not appear in its schema file")
    return problems


def check_every_module_is_shipped() -> list[str]:
    """Verify that no module in the package tree is left uninstalled.

    A module that exists in the source tree but appears in no install list is a
    silent omission: the service starts and then fails at import time on the
    target machine, which is exactly the class of packaging defect that is worst
    to debug after release.
    """
    problems: list[str] = []
    sources: list[str] = []
    for install_list in sorted(DEBIAN.glob("*.install")):
        for raw in install_list.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            sources.append(line.split()[0])
    for module in sorted((ROOT / "src" / "agentos").rglob("*.py")):
        relative = str(module.relative_to(ROOT))
        if relative.endswith("__pycache__") or "/__pycache__/" in relative:
            continue
        if relative in sources:
            continue
        # A glob in an install list covers the module when its directory matches.
        if any(_covers(pattern, relative) for pattern in sources):
            continue
        problems.append(f"{relative} is not installed by any package")
    return problems


def _covers(pattern: str, relative: str) -> bool:
    """Return whether an install-list pattern covers a specific module path.

    A pattern that ends in ``/*.py`` covers every module directly inside the
    matching directory; a pattern naming a directory covers its whole subtree.
    """
    if pattern == relative:
        return True
    if pattern.endswith("/*.py"):
        directory = pattern[: -len("/*.py")]
        return relative.startswith(directory + "/") and "/" not in relative[len(directory) + 1 :]
    return relative.startswith(pattern.rstrip("/") + "/")


def main() -> int:
    """Run every packaging check and report the result."""
    checks = {
        "control": check_control,
        "install lists": check_install_lists,
        "module coverage": check_every_module_is_shipped,
        "maintainer scripts": check_maintainer_scripts,
        "changelog": check_changelog,
        "systemd units": check_systemd_units,
        "policy files": check_policy_files,
        "gnome extension": check_gnome_extension,
    }
    failures: dict[str, list[str]] = {}
    for label, check in checks.items():
        try:
            problems = check()
        except Exception as error:  # noqa: BLE001 - a crashing check is a failing check
            problems = [f"the check itself failed: {type(error).__name__}: {error}"]
        if problems:
            failures[label] = problems
            print(f"[FAIL] {label}")
            for problem in problems:
                print(f"       {problem}")
        else:
            print(f"[ok  ] {label}")
    print("")
    if failures:
        total = sum(len(items) for items in failures.values())
        print(f"{total} packaging problem(s) in {len(failures)} area(s)")
        return 1
    print("packaging metadata is consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
