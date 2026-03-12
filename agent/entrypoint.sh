#!/usr/bin/env bash
# Ephemeral agent entrypoint: set up credentials, generate MCP config, run Claude CLI.
set -euo pipefail

CREDS_FILE="/tmp/claude-credentials.json"
CLAUDE_DIR="$HOME/.claude"
PROMPT_FILE="/tmp/task-prompt.txt"
MCP_CONFIG="/tmp/mcp-config.json"
MAX_TURNS="${MAX_TURNS:-20}"

# --- Credential setup ---
mkdir -p "$CLAUDE_DIR"

if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    true  # Claude CLI reads CLAUDE_CODE_OAUTH_TOKEN automatically (1-year token)
elif [ -f "$CREDS_FILE" ]; then
    cp "$CREDS_FILE" "$CLAUDE_DIR/.credentials.json"
    chmod 600 "$CLAUDE_DIR/.credentials.json"

    ORG_UUID=$(python3 -c "
import json
with open('$CREDS_FILE') as f:
    data = json.load(f)
print(data.get('organizationUuid', ''))
" 2>/dev/null || echo "")

    python3 -c "
import json
config = {'oauthAccount': {'organizationUuid': '$ORG_UUID'}}
with open('$CLAUDE_DIR/.claude.json', 'w') as f:
    json.dump(config, f, indent=2)
"
else
    echo "[agent] ERROR: No CLAUDE_CODE_OAUTH_TOKEN or credentials file" >&2
    exit 1
fi

# --- Generate MCP config from environment ---
python3 -c "
import json, os, sys

servers = {}

# Obsidian MCP (always available on the Docker network)
obsidian_url = os.environ.get('OBSIDIAN_MCP_URL', 'http://obsidian-mcp:3010/sse')
if obsidian_url:
    servers['obsidian'] = {'type': 'sse', 'url': obsidian_url}

# Slack MCP (if token configured and enabled)
slack_url = os.environ.get('SLACK_MCP_URL', 'http://slack-mcp:3001/sse')
slack_token = os.environ.get('SLACK_BOT_TOKEN', '')
slack_enabled = os.environ.get('SLACK_MCP_ENABLED', 'true').lower() != 'false'
if slack_token and slack_enabled:
    servers['slack'] = {'type': 'sse', 'url': slack_url}

# GitLab MCP (SSE server on Docker network, or custom URL)
gitlab_token = os.environ.get('GITLAB_TOKEN', '')
gitlab_url = os.environ.get('GITLAB_MCP_URL', 'http://gitlab-mcp:3002/sse')
gitlab_enabled = os.environ.get('GITLAB_MCP_ENABLED', 'true').lower() != 'false'
if gitlab_token and gitlab_enabled:
    servers['gitlab'] = {'type': 'sse', 'url': gitlab_url}

# GitHub MCP (if token configured and enabled)
github_token = os.environ.get('GITHUB_TOKEN', '')
github_url = os.environ.get('GITHUB_MCP_URL', 'http://github-mcp:3003/sse')
github_enabled = os.environ.get('GITHUB_MCP_ENABLED', 'true').lower() != 'false'
if github_token and github_enabled:
    servers['github'] = {'type': 'sse', 'url': github_url}

# Dynamic MCP instances (additional GitHub/GitLab accounts from dispatcher)
mcp_instances_raw = os.environ.get('DAVYJONES_MCP_INSTANCES', '')
if mcp_instances_raw:
    try:
        instances = json.loads(mcp_instances_raw)
        for inst in instances:
            inst_id = inst.get('id', '')
            inst_url = inst.get('url', '')
            if inst_id and inst_url:
                servers[inst_id] = {'type': 'sse', 'url': inst_url}
    except Exception as e:
        print(f'[agent] Failed to parse DAVYJONES_MCP_INSTANCES: {e}', file=sys.stderr)

config = {'mcpServers': servers}
with open('$MCP_CONFIG', 'w') as f:
    json.dump(config, f, indent=2)

print('[agent] MCP servers:', list(servers.keys()), file=sys.stderr)
for name, srv in servers.items():
    print(f'  {name}: {srv.get(\"type\",\"?\")} -> {srv.get(\"url\",\"?\")}', file=sys.stderr)
"

# --- MCP connectivity diagnostics ---
python3 -c "
import json, subprocess, sys

with open('$MCP_CONFIG') as f:
    servers = json.load(f).get('mcpServers', {})

for name, srv in servers.items():
    url = srv.get('url', '')
    if not url:
        continue
    try:
        # Use --connect-timeout for TCP check, --max-time for total (SSE streams forever)
        result = subprocess.run(
            ['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', '--connect-timeout', '3', '--max-time', '4', url],
            capture_output=True, text=True, timeout=6
        )
        code = result.stdout.strip()
        # SSE returns 200 then streams (curl exits 28 on max-time), HTTP returns immediately
        # curl exit 0 = completed, exit 28 = timeout (but connected), others = failed
        if result.returncode == 0 or result.returncode == 28:
            if code and code != '000':
                print(f'[agent] reachable: {name} ({url}) HTTP {code}', file=sys.stderr)
            else:
                print(f'[agent] reachable: {name} ({url}) connected', file=sys.stderr)
        else:
            print(f'[agent] unreachable: {name} ({url}) exit={result.returncode}', file=sys.stderr)
    except Exception as e:
        print(f'[agent] check failed: {name} ({url}) {e}', file=sys.stderr)
" 2>/dev/null || true

# --- Read prompt and run Claude CLI ---
if [ ! -f "$PROMPT_FILE" ]; then
    echo "[agent] ERROR: No prompt file at $PROMPT_FILE" >&2
    exit 1
fi

PROMPT=$(cat "$PROMPT_FILE")

exec claude -p "$PROMPT" \
    --output-format json \
    --max-turns "$MAX_TURNS" \
    --dangerously-skip-permissions \
    --mcp-config "$MCP_CONFIG"
