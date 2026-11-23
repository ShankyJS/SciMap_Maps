#!/bin/bash
# Install pinned Tippecanoe, AWS CLI, and rclone into ROOT/bin.
# Targets GitHub-hosted ubuntu-latest (linux amd64). A second run skips a tool
# whose version already matches. Output is captured to a file so pipefail does
# not turn SIGPIPE into a false mismatch.
set -euo pipefail

root=${1:?usage: install_daily_toolchain.sh ROOT}
bindir="$root/bin"
mkdir -p "$bindir"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
export DEBIAN_FRONTEND=noninteractive

version_matches() {
  local pattern="$1"
  shift
  local out="$tmp/version.txt"
  : >"$out"
  if [ -x "$1" ]; then
    "$@" >"$out" 2>&1 || true
  fi
  grep -q -- "$pattern" "$out" || return 1
}

if ! version_matches '^rclone v1\.75\.1$' "$bindir/rclone" version; then
  curl -fsSL -o "$tmp/rclone.zip" https://downloads.rclone.org/v1.75.1/rclone-v1.75.1-linux-amd64.zip
  unzip -q "$tmp/rclone.zip" -d "$tmp/rclone"
  install -m 0755 "$tmp"/rclone/rclone-*/rclone "$bindir/rclone"
fi

if ! version_matches '^aws-cli/2\.40\.0 ' "$bindir/aws" --version; then
  curl -fsSL -o "$tmp/aws.zip" https://awscli.amazonaws.com/awscli-exe-linux-x86_64-2.40.0.zip
  unzip -q "$tmp/aws.zip" -d "$tmp/aws"
  "$tmp/aws/aws/install" -i "$root/aws-cli" -b "$bindir" --update
fi

if ! version_matches '^tippecanoe v2\.79\.0$' "$bindir/tippecanoe" --version; then
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends build-essential libsqlite3-dev zlib1g-dev
  git clone --depth 1 --branch 2.79.0 https://github.com/felt/tippecanoe.git "$tmp/tippecanoe"
  make -C "$tmp/tippecanoe" -j"$(nproc)"
  install -m 0755 "$tmp/tippecanoe/tippecanoe" "$bindir/tippecanoe"
fi

version_matches '^rclone v1\.75\.1$' "$bindir/rclone" version
version_matches '^aws-cli/2\.40\.0 ' "$bindir/aws" --version
version_matches '^tippecanoe v2\.79\.0$' "$bindir/tippecanoe" --version
