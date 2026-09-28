#!/usr/bin/env bash
# Bump the top-level VERSION file and print the new version.
#
#   scripts/bump-version.sh patch   # 0.1.0 -> 0.1.1
#   scripts/bump-version.sh minor   # 0.1.1 -> 0.2.0
#   scripts/bump-version.sh major   # 0.2.0 -> 1.0.0
#
# Typical release flow:
#   make bump-minor            # VERSION now 0.2.0
#   git commit -am "release 0.2.0"
#   git tag v0.2.0 && git push --follow-tags
set -euo pipefail

part="${1:-patch}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
file="$root/VERSION"

current="$(tr -d '[:space:]' < "$file")"
current="${current#v}"
IFS=. read -r major minor patch <<< "${current:-0.0.0}"
major="${major:-0}"; minor="${minor:-0}"; patch="${patch:-0}"

case "$part" in
  major) major=$((major + 1)); minor=0; patch=0 ;;
  minor) minor=$((minor + 1)); patch=0 ;;
  patch) patch=$((patch + 1)) ;;
  *) echo "usage: $0 {patch|minor|major}" >&2; exit 2 ;;
esac

new="${major}.${minor}.${patch}"
printf '%s\n' "$new" > "$file"
printf '%s\n' "$new"
