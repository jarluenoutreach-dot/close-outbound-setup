#!/usr/bin/env bash
# Pull the latest code and restart. Usage: sudo ./deploy/update.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
git -c safe.directory="$APP_DIR" -C "$APP_DIR" pull --ff-only
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
chown -R outbound:outbound "$APP_DIR"
systemctl restart close-outbound
systemctl --no-pager status close-outbound | head -5
