#!/usr/bin/env bash
# Unified DavyJones entry point: extract credentials, build, start.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "=== DavyJones Start ==="
echo ""

# Step 1: Extract fresh credentials from Keychain
echo "Extracting credentials..."
if ! bash "$SCRIPT_DIR/extract_credentials.sh"; then
    echo ""
    echo "ERROR: Could not extract credentials."
    echo "Run 'claude login' first, then try again."
    exit 1
fi

# Step 2: Merge vault-specific env (Slack/GitLab tokens)
ENV_FILE="$PROJECT_ROOT/.env"
if [ -f "$ENV_FILE" ]; then
    VAULT_DIR=$(grep "^VAULT_DIR=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)
    if [ -n "$VAULT_DIR" ]; then
        source "$SCRIPT_DIR/_merge_env.sh"
        merge_vault_env "$VAULT_DIR" "$ENV_FILE"
    fi
fi

# Step 3: Build images and bring everything up
echo ""
cd "$PROJECT_ROOT"
docker compose build
docker compose up -d

echo ""
echo "DavyJones is running."
