#!/usr/bin/env bash
# Syntax-check every helper script and report the exact failure, if any.
#
# A script that fails to parse is only discovered when a user runs it, usually
# mid-install, so this check runs before anything else in the verification chain.
#
# Usage: check-shell-syntax.sh [repo-root]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${1:-$(cd "$HERE/.." && pwd)}"
status=0
checked=0
# Initialized rather than only set by the loop: with `set -u`, a name nothing has
# assigned is an error at its first use, and finding exactly that mistake in other
# scripts is what check-shell-variables.py does.
script=""
output=""

echo "=== shell scripts ==="
while IFS= read -r script; do
  checked=$((checked + 1))
  relative="${script#"$ROOT"/}"
  if output=$(bash -n "$script" 2>&1); then
    printf '  [ok  ] %s\n' "$relative"
  else
    printf '  [FAIL] %s\n' "$relative"
    printf '         %s\n' "$output"
    status=1
  fi
done < <(find "$ROOT/scripts" "$ROOT/debian" -maxdepth 2 -type f \
           \( -name '*.sh' -o -name '*.postinst' -o -name '*.prerm' -o -name '*.postrm' -o -name '*.preinst' \) \
           -not -path '*/.git/*' | sort)

echo ""
echo "  scripts checked: $checked"

echo ""
echo "=== maintainer scripts also parse as POSIX sh ==="
for script in "$ROOT"/debian/agentos-*.postinst "$ROOT"/debian/agentos-*.prerm "$ROOT"/debian/agentos-*.postrm; do
  [ -e "$script" ] || continue
  relative="debian/$(basename "$script")"
  if output=$(sh -n "$script" 2>&1); then
    printf '  [ok  ] %s\n' "$relative"
  else
    printf '  [FAIL] %s declares /bin/sh but does not parse as POSIX sh\n' "$relative"
    printf '         %s\n' "$output"
    status=1
  fi
done

echo ""
echo "=== python helper scripts ==="
while IFS= read -r script; do
  relative="${script#"$ROOT"/}"
  if output=$(python3 -m py_compile "$script" 2>&1); then
    printf '  [ok  ] %s\n' "$relative"
  else
    printf '  [FAIL] %s does not compile\n' "$relative"
    printf '         %s\n' "$output"
    status=1
  fi
done < <(find "$ROOT/scripts" -maxdepth 1 -name '*.py' | sort)
find "$ROOT/scripts" -maxdepth 1 -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

echo ""
if [ "$status" -eq 0 ]; then
  echo "every helper script parses"
  exit 0
fi
echo "at least one helper script does not parse"
exit 1
