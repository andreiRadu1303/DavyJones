#!/usr/bin/env bash
set -euo pipefail
#
# switch.sh — Start a vault's dispatcher if not already running.
# Called by the Obsidian plugin when the user switches vaults.
# Does NOT stop other vaults — multiple can run simultaneously.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Resolve vault path ─────────────────────────────────────────
if [ -z "${1:-}" ]; then
    echo "Usage: $0 <vault-path>"
    exit 1
fi

if [[ "$1" == /* ]]; then
    VAULT_ABS="$1"
else
    VAULT_ABS="$(cd "$1" 2>/dev/null && pwd)"
fi

if [ ! -d "$VAULT_ABS" ]; then
    echo "Error: $VAULT_ABS does not exist"
    exit 1
fi

# ── Install plugin if missing ───────────────────────────────────
if [ ! -d "$VAULT_ABS/.obsidian/plugins/davyjones" ]; then
    echo "Plugin not found — running setup..."
    "$PROJECT_ROOT/davyjones" setup "$VAULT_ABS"
fi

# ── Start vault if not running ──────────────────────────────────
# Source the helpers from the main davyjones script
source <(grep -A999 '^_vault_slug()' "$PROJECT_ROOT/davyjones" | head -n 5)

# Derive project name
SLUG=$(basename "$VAULT_ABS" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed 's/^-//;s/-$//')
PROJECT="davyjones-${SLUG}"

# Check if already running
RUNNING=$(docker compose --project-name "$PROJECT" ps --status running -q 2>/dev/null | wc -l | tr -d ' ')

if [ "$RUNNING" -gt 0 ]; then
    echo "Already running: $PROJECT"
else
    echo "Starting vault: $(basename "$VAULT_ABS")"
    "$PROJECT_ROOT/davyjones" start --vault "$VAULT_ABS" --here
fi

echo "Done."
