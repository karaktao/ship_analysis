#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 1
fi

swap_path="${1:-/swapfile}"
swap_size="${SWAP_SIZE:-4G}"

if swapon --show=NAME --noheadings | awk '{print $1}' | grep -Fxq "${swap_path}"; then
  echo "Swap already active: ${swap_path}"
  exit 0
fi

if [[ -e "${swap_path}" ]]; then
  if [[ "$(stat -c '%a' "${swap_path}")" != "600" ]]; then
    chmod 600 "${swap_path}"
  fi
  if ! file "${swap_path}" | grep -qi 'swap file'; then
    echo "Refusing to overwrite existing non-swap file: ${swap_path}" >&2
    exit 1
  fi
else
  fallocate -l "${swap_size}" "${swap_path}"
  chmod 600 "${swap_path}"
  mkswap "${swap_path}" >/dev/null
fi

swapon "${swap_path}"
if ! grep -qE "^[[:space:]]*${swap_path//\//\\/}[[:space:]]" /etc/fstab; then
  printf '%s none swap sw 0 0\n' "${swap_path}" >> /etc/fstab
fi

echo "Swap enabled: ${swap_path} (${swap_size})"
