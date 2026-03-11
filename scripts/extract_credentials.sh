#!/usr/bin/env bash
# Extract Claude CLI OAuth credentials from macOS Keychain
# Run this on the host before `docker compose up`
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CREDS_FILE="$PROJECT_DIR/.claude-credentials.json"

echo "Extracting Claude CLI credentials from macOS Keychain..."

# Try user-specific account entry first (created by `claude auth login`),
# then fall back to the default entry (created by `claude login`).
KEYCHAIN_DATA=""
CURRENT_USER=$(whoami)
if KEYCHAIN_DATA=$(security find-generic-password -s "Claude Code-credentials" -a "$CURRENT_USER" -w 2>/dev/null); then
    echo "Found credentials for account '$CURRENT_USER'"
elif KEYCHAIN_DATA=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null); then
    echo "Found credentials (default account)"
else
    echo "ERROR: Could not find 'Claude Code-credentials' in Keychain."
    echo "Run 'claude login' first to authenticate."
    exit 1
fi

# Validate it's valid JSON with expected fields
echo "$KEYCHAIN_DATA" | python3 -c "
import sys, json
data = json.load(sys.stdin)
oauth = data.get('claudeAiOauth', {})
if not oauth.get('accessToken'):
    print('ERROR: No accessToken found in credentials', file=sys.stderr)
    sys.exit(1)
if not oauth.get('refreshToken'):
    print('ERROR: No refreshToken found in credentials', file=sys.stderr)
    sys.exit(1)
print('Credentials validated OK')
" || exit 1

# Write credentials file
echo "$KEYCHAIN_DATA" > "$CREDS_FILE"
chmod 600 "$CREDS_FILE"

echo "Credentials written to $CREDS_FILE"
echo "Token expires at: $(echo "$KEYCHAIN_DATA" | python3 -c "
import sys, json, datetime
data = json.load(sys.stdin)
ts = data['claudeAiOauth']['expiresAt'] / 1000
print(datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S'))
")"
