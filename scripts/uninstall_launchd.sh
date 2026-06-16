#!/usr/bin/env bash
set -euo pipefail

# uninstall_launchd.sh — remove synapse-wx LaunchAgent

TARGET="${HOME}/Library/LaunchAgents/com.synapse-wx.bridge.plist"

if [[ -f "${TARGET}" ]]; then
    launchctl unload "${TARGET}" 2>/dev/null || true
    rm -f "${TARGET}"
fi

echo "Unloaded com.synapse-wx.bridge"
