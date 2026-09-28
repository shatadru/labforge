#!/usr/bin/env bash
# Download an untouched official cloud image. No sudo, libvirt, or VM creation.
set -euo pipefail

if (( EUID == 0 )); then
    echo "Run this script as your normal user, not root." >&2
    exit 1
fi
if (( $# > 1 )); then
    echo "Usage: $0 [template-directory]" >&2
    exit 2
fi
for command in curl sha256sum awk mktemp; do
    command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; exit 1; }
done

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
destination=${1:-"$repo_dir/backend/cloud_init_templates/centos-stream10"}
image=CentOS-Stream-GenericCloud-10-20260922.0.x86_64.qcow2
base_url=https://cloud.centos.org/centos/10-stream/x86_64/images
pinned_sha256=cc788312a1f5d86f2557de38b4ca57ff9f9a6b7f812f7a7fc91a5009fb68f76b

mkdir -p -- "$destination"
destination=$(cd -- "$destination" && pwd)
# Keep the large image on the destination filesystem, not a RAM-backed /tmp.
# The hidden staging directory is never discoverable as a template image.
staging=$(mktemp -d "$destination/.download.XXXXXXXX")
trap 'rm -rf -- "$staging"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
curl_options=(--fail --location --silent --show-error --proto '=https' --proto-redir '=https' --connect-timeout 30 --retry 3)
curl "${curl_options[@]}" --max-time 120 \
    "$base_url/$image.SHA256SUM" -o "$staging/official.SHA256SUM"
expected=$(awk -v prefix="SHA256 ($image) = " \
    'index($0, prefix) == 1 {print substr($0, length(prefix) + 1)}' "$staging/official.SHA256SUM")
if [[ ! "$expected" =~ ^[a-fA-F0-9]{64}$ ]] || [[ "$expected" != "$pinned_sha256" ]]; then
    echo "Official SHA-256 is missing, ambiguous, or differs from the reviewed pin; refusing download." >&2
    exit 1
fi
verify() {
    printf '%s  %s\n' "$expected" "$1" | sha256sum --check --status
}
if [[ -e "$destination/$image" || -L "$destination/$image" ]]; then
    if [[ -L "$destination/$image" || ! -f "$destination/$image" ]] || ! verify "$destination/$image"; then
        echo "Existing destination is not the verified official image; refusing to replace it." >&2
        exit 1
    fi
    echo "Already verified: $destination/$image"
else
    echo "Downloading $base_url/$image (about 1 GiB)..."
    curl "${curl_options[@]}" --max-time 1800 "$base_url/$image" -o "$staging/$image"
    verify "$staging/$image" || { echo "SHA-256 mismatch; refusing installation." >&2; exit 1; }
    chmod 0644 "$staging/$image"
    # Hard link atomically publishes only the verified file, without overwriting.
    ln -- "$staging/$image" "$destination/$image"
    echo "Downloaded and verified: $destination/$image"
fi
printf 'SHA256: %s\n' "$expected"
