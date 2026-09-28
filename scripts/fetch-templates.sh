#!/usr/bin/env bash
# Download official cloud images for extra LabForge templates and verify checksums.
#
# Usage: scripts/fetch-templates.sh [templates_dir]
set -euo pipefail

TPL_DIR="${1:-$(cd "$(dirname "$0")/../backend" && pwd)/cloud_init_templates}"

# name|url|sha256sums_url
IMAGES=(
  "ubuntu-24.04|https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img|https://cloud-images.ubuntu.com/noble/current/SHA256SUMS"
  "debian-12|https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2|https://cloud.debian.org/images/cloud/bookworm/latest/SHA256SUMS"
)

for entry in "${IMAGES[@]}"; do
  IFS='|' read -r name url sums_url <<< "$entry"
  dir="$TPL_DIR/$name"
  mkdir -p "$dir"
  file="$dir/$(basename "$url")"

  if [[ -f "$file" ]]; then
    echo "[$name] already present: $file"
  else
    echo "[$name] downloading $(basename "$url") ..."
    curl -fL --retry 3 --retry-delay 2 --proto '=https' -o "$file.part" "$url"
    mv "$file.part" "$file"
  fi

  echo "[$name] verifying checksum ..."
  sums="$(mktemp)"
  trap 'rm -f "$sums"' EXIT
  if curl -fsSL --proto '=https' -o "$sums" "$sums_url"; then
    base="$(basename "$url")"
    expected="$(grep -E "[ *]$base$" "$sums" | head -1 | awk '{print $1}' || true)"
    actual="$(sha256sum "$file" | awk '{print $1}')"
    if [[ -n "$expected" && "$expected" == "$actual" ]]; then
      echo "[$name] sha256 OK ($actual)"
    else
      echo "[$name] ERROR: checksum mismatch (expected=$expected actual=$actual)" >&2
      echo "[$name] refusing to keep a corrupt or unexpected image" >&2
      rm -f "$file"
      exit 1
    fi
  else
    echo "[$name] ERROR: could not fetch upstream checksums; refusing to trust $file" >&2
    rm -f "$file"
    exit 1
  fi
  rm -f "$sums"
  trap - EXIT
done

echo "done."
