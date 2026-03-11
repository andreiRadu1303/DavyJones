#!/usr/bin/env bash
# Smart credential fix: extract from Keychain, refresh if needed, or re-login.
# Called by the Obsidian plugin's "fix credentials" button.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CREDS_FILE="$PROJECT_DIR/.claude-credentials.json"

# ── Step 1: Extract from Keychain (maybe user logged in elsewhere) ──
if bash "$SCRIPT_DIR/extract_credentials.sh" 2>/dev/null; then
    VALID=$(python3 -c "
import json, time
with open('$CREDS_FILE') as f:
    data = json.load(f)
exp = data.get('claudeAiOauth', {}).get('expiresAt', 0) / 1000
print('yes' if time.time() < exp - 300 else 'no')
" 2>/dev/null || echo "no")

    if [ "$VALID" = "yes" ]; then
        echo "FIX_EXTRACTED"
        exit 0
    fi
    echo "Keychain credentials expired, attempting token refresh..."
fi

# ── Step 2: Try refreshing the token directly ──
if [ -f "$CREDS_FILE" ]; then
    REFRESH_RESULT=$(python3 -c "
import json, time, urllib.request, urllib.error

CREDS_FILE = '$CREDS_FILE'
OAUTH_ENDPOINT = 'https://console.anthropic.com/v1/oauth/token'
CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e'

with open(CREDS_FILE) as f:
    data = json.load(f)

oauth = data.get('claudeAiOauth', {})
refresh_tok = oauth.get('refreshToken')
if not refresh_tok:
    print('NO_REFRESH_TOKEN')
    exit()

payload = json.dumps({
    'grant_type': 'refresh_token',
    'refresh_token': refresh_tok,
    'client_id': CLIENT_ID,
}).encode()

try:
    req = urllib.request.Request(
        OAUTH_ENDPOINT,
        data=payload,
        headers={'Content-Type': 'application/json', 'User-Agent': 'claude-code/2.1.71'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode())

    new_access = result.get('access_token')
    new_refresh = result.get('refresh_token')
    expires_in = result.get('expires_in', 28800)

    if not new_access:
        print('REFRESH_NO_TOKEN')
        exit()

    oauth['accessToken'] = new_access
    if new_refresh:
        oauth['refreshToken'] = new_refresh
    oauth['expiresAt'] = int(time.time() * 1000) + (expires_in * 1000)
    data['claudeAiOauth'] = oauth

    with open(CREDS_FILE, 'w') as f:
        json.dump(data, f, indent=2)

    print('REFRESHED')
except urllib.error.HTTPError as e:
    print(f'HTTP_{e.code}')
except Exception as ex:
    print(f'ERROR_{ex}')
" 2>/dev/null || echo "ERROR")

    if [ "$REFRESH_RESULT" = "REFRESHED" ]; then
        echo "FIX_REFRESHED"
        exit 0
    fi
    echo "Token refresh failed ($REFRESH_RESULT), launching claude login..."
fi

# ── Step 3: Full re-login via Claude CLI ──
# Opens the browser for OAuth — user clicks through, credentials saved to Keychain

# Find claude binary: check PATH, then VS Code extension, then Cursor extension
CLAUDE_BIN=""
if command -v claude &>/dev/null; then
    CLAUDE_BIN="claude"
else
    # Search VS Code / Cursor extension directories for the native binary
    for ext_dir in "$HOME/.vscode/extensions" "$HOME/.cursor/extensions"; do
        if [ -d "$ext_dir" ]; then
            found=$(find "$ext_dir" -path "*/anthropic.claude-code-*/resources/native-binary/claude" -type f 2>/dev/null | sort -V | tail -1)
            if [ -n "$found" ] && [ -x "$found" ]; then
                CLAUDE_BIN="$found"
                break
            fi
        fi
    done
fi

if [ -z "$CLAUDE_BIN" ]; then
    echo "FIX_NO_CLI"
    exit 1
fi

# Open Terminal.app for claude auth login (needs a real TTY + environment)
MARKER_FILE=$(mktemp /tmp/davyjones-login-done.XXXXXX)
rm -f "$MARKER_FILE"

osascript -e "
    tell application \"Terminal\"
        activate
        do script \"'$CLAUDE_BIN' auth login && touch '$MARKER_FILE' && echo 'Done — you can close this window.'\"
    end tell
"

# Wait for login to complete (poll marker file, up to 4 minutes)
WAITED=0
while [ ! -f "$MARKER_FILE" ] && [ $WAITED -lt 240 ]; do
    sleep 2
    WAITED=$((WAITED + 2))
done
rm -f "$MARKER_FILE"

if [ $WAITED -ge 240 ]; then
    echo "FIX_LOGIN_TIMEOUT"
    exit 1
fi

# Re-extract the fresh credentials from Keychain
bash "$SCRIPT_DIR/extract_credentials.sh"
echo "FIX_RELOGIN"
