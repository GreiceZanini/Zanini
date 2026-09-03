#!/usr/bin/env bash
# Idempotent Cloud Agent setup for the Zanini NFS-e automation project.
# Safe to run repeatedly and against cached/snapshotted state.
set -euo pipefail

cd "$(dirname "$0")/.."

# python3-venv is required to create virtual environments and is not present in
# the base image. Install it only when missing.
if ! dpkg -s python3-venv >/dev/null 2>&1; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv
fi

# Create the virtual environment only if it does not already exist.
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Install Chromium and its OS-level dependencies for Playwright. Both commands
# are idempotent: they skip work that is already complete.
sudo .venv/bin/python -m playwright install-deps chromium
.venv/bin/python -m playwright install chromium

echo "Cloud Agent environment ready. Activate with: source .venv/bin/activate"
