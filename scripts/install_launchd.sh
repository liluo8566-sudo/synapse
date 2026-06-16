#!/usr/bin/env bash
set -euo pipefail

# install_launchd.sh — install synapse-wx LaunchAgent
# Run from repo root: ./scripts/install_launchd.sh

BRIDGE_HOME="$(pwd)"
BRIDGE_BIN="${BRIDGE_HOME}/.venv/bin/python"
BRIDGE_MODULE="synapse_wx"
USER_HOME="${HOME}"

TEMPLATE="${BRIDGE_HOME}/synapse_wx/deploy/com.synapse-wx.bridge.plist.template"
TARGET="${USER_HOME}/Library/LaunchAgents/com.synapse-wx.bridge.plist"

# --- preflight ---
if [[ ! -x "${BRIDGE_BIN}" ]]; then
    echo "error: python interpreter not found at ${BRIDGE_BIN}" >&2
    echo "       create the venv first: python3 -m venv .venv" >&2
    exit 1
fi

if [[ ! -d "${BRIDGE_HOME}/synapse_wx" ]]; then
    echo "error: ${BRIDGE_HOME} does not contain a synapse_wx/ dir" >&2
    echo "       run this script from the repo root" >&2
    exit 1
fi

if [[ ! -f "${TEMPLATE}" ]]; then
    echo "error: plist template missing at ${TEMPLATE}" >&2
    exit 1
fi

# --- ensure dirs ---
mkdir -p "${USER_HOME}/Library/Logs" "${USER_HOME}/Library/LaunchAgents"

# --- substitute placeholders ---
sed \
    -e "s|__BRIDGE_BIN__|${BRIDGE_BIN}|g" \
    -e "s|__BRIDGE_MODULE__|${BRIDGE_MODULE}|g" \
    -e "s|__BRIDGE_HOME__|${BRIDGE_HOME}|g" \
    -e "s|__USER_HOME__|${USER_HOME}|g" \
    "${TEMPLATE}" > "${TARGET}"

# --- (re)load ---
launchctl unload "${TARGET}" 2>/dev/null || true
launchctl load -w "${TARGET}"

echo "Loaded com.synapse-wx.bridge — logs at ${USER_HOME}/Library/Logs/synapse-wx.{out,err}.log"
