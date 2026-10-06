#!/usr/bin/env bash
# Resolve the build version for packages, charts and images.
#
#   release tag (vX.Y.Z)  -> X.Y.Z
#   CI build on a branch  -> <base>-ci.<short-sha>
#   local / unknown       -> <base>-dev
#
# The base is the contents of the top-level VERSION file, bumped by
# bump-my-version via `make release-patch|release-minor|release-major`.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
base="$(tr -d '[:space:]' < "$root/VERSION")"
base="${base#v}"

if [[ "${GITHUB_REF_TYPE:-}" == "tag" && -n "${GITHUB_REF_NAME:-}" ]]; then
  printf '%s\n' "${GITHUB_REF_NAME#v}"
elif [[ -n "${GITHUB_SHA:-}" ]]; then
  printf '%s\n' "${base}-ci.${GITHUB_SHA:0:7}"
else
  # Local build: prefer an exact tag on HEAD, else a dev version off the file.
  tag="$(git -C "$root" describe --tags --exact-match 2>/dev/null || true)"
  if [[ -n "$tag" ]]; then
    printf '%s\n' "${tag#v}"
  else
    printf '%s\n' "${base}-dev"
  fi
fi
