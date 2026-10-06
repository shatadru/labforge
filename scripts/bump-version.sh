#!/usr/bin/env bash
# Deprecated: version bumping moved to bump-my-version (.bumpversion.toml),
# which updates VERSION *and* charts/labforge/Chart.yaml in one commit.
#
# Kept as a thin wrapper so existing habits keep working:
#   scripts/bump-version.sh patch|minor|major
# is equivalent to `make release-patch|release-minor|release-major`.
set -euo pipefail

part="${1:-patch}"
case "$part" in
  patch|minor|major) ;;
  *) echo "usage: $0 {patch|minor|major}" >&2; exit 2 ;;
esac

command -v bump-my-version >/dev/null 2>&1 || {
  echo "bump-my-version not found: pipx install bump-my-version" >&2
  exit 2
}

exec bump-my-version bump "$part"
