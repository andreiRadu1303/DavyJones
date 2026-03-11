#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLUGIN_SRC="$PROJECT_ROOT/obsidian-plugin"

# ── Find or accept vault path ───────────────────────────────────
if [ -n "${1:-}" ]; then
    VAULT_INPUT="$1"
    # Resolve to absolute path
    if [[ "$VAULT_INPUT" == /* ]]; then
        VAULT_ABS="$VAULT_INPUT"
    else
        VAULT_ABS="$(cd "$PROJECT_ROOT" && cd "$VAULT_INPUT" 2>/dev/null && pwd)" || \
        VAULT_ABS="$PROJECT_ROOT/$VAULT_INPUT"
    fi
else
    # Auto-detect from Obsidian's vault registry (cross-platform)
    case "$(uname -s)" in
        Darwin)  OBSIDIAN_CONFIG="$HOME/Library/Application Support/obsidian/obsidian.json" ;;
        Linux)   OBSIDIAN_CONFIG="$HOME/.config/obsidian/obsidian.json" ;;
        MINGW*|MSYS*|CYGWIN*) OBSIDIAN_CONFIG="$APPDATA/obsidian/obsidian.json" ;;
        *)       OBSIDIAN_CONFIG="" ;;
    esac
    VAULTS=()
    if [ -n "$OBSIDIAN_CONFIG" ] && [ -f "$OBSIDIAN_CONFIG" ]; then
        while IFS= read -r vpath; do
            [ -d "$vpath" ] && VAULTS+=("$vpath")
        done < <(python3 -c "
import json, os
with open(os.path.expanduser('$OBSIDIAN_CONFIG')) as f:
    data = json.load(f)
for v in data.get('vaults', {}).values():
    print(v['path'])
" 2>/dev/null)
    fi

    if [ ${#VAULTS[@]} -eq 0 ]; then
        echo "No Obsidian vaults found."
        echo "Usage: $0 <path-to-vault>"
        echo "  e.g. $0 /Users/me/MyVault"
        exit 1
    elif [ ${#VAULTS[@]} -eq 1 ]; then
        VAULT_ABS="${VAULTS[0]}"
        echo "Auto-detected vault: $VAULT_ABS"
    else
        echo "Obsidian vaults found:"
        for i in "${!VAULTS[@]}"; do
            echo "  $((i+1)). ${VAULTS[$i]}"
        done
        read -rp "Select vault [1-${#VAULTS[@]}]: " choice
        VAULT_ABS="${VAULTS[$((choice-1))]}"
    fi
fi

if [ ! -d "$VAULT_ABS" ]; then
    echo "Error: $VAULT_ABS does not exist"
    exit 1
fi

echo "=== DavyJones Setup ==="
echo "Vault: $VAULT_ABS"
echo ""

# ── Ensure .obsidian structure exists ───────────────────────────
mkdir -p "$VAULT_ABS/.obsidian/plugins"

# ── Install plugin ──────────────────────────────────────────────
PLUGIN_DEST="$VAULT_ABS/.obsidian/plugins/davyjones"

if [ -L "$PLUGIN_DEST" ]; then
    echo "Plugin already symlinked."
elif [ -d "$PLUGIN_DEST" ]; then
    echo "Replacing plugin copy with symlink..."
    rm -rf "$PLUGIN_DEST"
    ln -s "$PLUGIN_SRC" "$PLUGIN_DEST"
    echo "Plugin symlinked."
else
    echo "Installing DavyJones plugin (symlink)..."
    ln -s "$PLUGIN_SRC" "$PLUGIN_DEST"
    echo "Plugin symlinked."
fi

# ── Enable plugin in community-plugins.json ─────────────────────
COMMUNITY_PLUGINS="$VAULT_ABS/.obsidian/community-plugins.json"
if [ -f "$COMMUNITY_PLUGINS" ]; then
    if ! python3 -c "import json; d=json.load(open('$COMMUNITY_PLUGINS')); exit(0 if 'davyjones' in d else 1)" 2>/dev/null; then
        python3 -c "
import json
with open('$COMMUNITY_PLUGINS') as f:
    d = json.load(f)
d.append('davyjones')
with open('$COMMUNITY_PLUGINS', 'w') as f:
    json.dump(d, f, indent=2)
"
        echo "  Added davyjones to community-plugins.json"
    fi
else
    echo '["davyjones"]' > "$COMMUNITY_PLUGINS"
    echo "  Created community-plugins.json"
fi

# ── Write plugin config (project path for switch feature) ───────
echo "{\"projectRoot\": \"$PROJECT_ROOT\"}" > "$VAULT_ABS/.obsidian/davyjones-config.json"
echo "  Plugin installed."

# ── Set VAULT_DIR in .env (always absolute) ─────────────────────
ENV_FILE="$PROJECT_ROOT/.env"
if [ -f "$ENV_FILE" ]; then
    if grep -q "^VAULT_DIR=" "$ENV_FILE"; then
        if [[ "$(uname -s)" == "Darwin" ]]; then
            sed -i '' "s|^VAULT_DIR=.*|VAULT_DIR=$VAULT_ABS|" "$ENV_FILE"
        else
            sed -i "s|^VAULT_DIR=.*|VAULT_DIR=$VAULT_ABS|" "$ENV_FILE"
        fi
    else
        echo "VAULT_DIR=$VAULT_ABS" >> "$ENV_FILE"
    fi
else
    cp "$PROJECT_ROOT/.env.example" "$ENV_FILE" 2>/dev/null || true
    echo "VAULT_DIR=$VAULT_ABS" >> "$ENV_FILE"
fi
echo "  VAULT_DIR=$VAULT_ABS set in .env"

# ── Create vault-specific .davyjones.env (if not already present) ──
VAULT_ENV="$VAULT_ABS/.davyjones.env"
if [ ! -f "$VAULT_ENV" ]; then
    cat > "$VAULT_ENV" << 'ENVEOF'
# DavyJones vault configuration
# Managed by the DavyJones Obsidian plugin — edit via Settings > DavyJones

# Long-lived OAuth token from 'claude setup-token' (valid 1 year)
CLAUDE_CODE_OAUTH_TOKEN=

# GitHub Personal Access Token — GitHub > Settings > Developer settings > Personal access tokens
GITHUB_TOKEN=

# GitLab Personal Access Token — GitLab > Settings > Access Tokens
GITLAB_TOKEN=

# Self-hosted GitLab only (default: gitlab.com):
# GITLAB_MCP_URL=

# Slack Bot Token — api.slack.com/apps > OAuth & Permissions
SLACK_BOT_TOKEN=

# Slack App Token (Socket Mode) — api.slack.com/apps > Socket Mode
SLACK_APP_TOKEN=
ENVEOF
    echo "  Created .davyjones.env (configure tokens via Obsidian Settings > DavyJones)"
fi

# ── Merge vault env into root .env ───────────────────────────────
source "$SCRIPT_DIR/_merge_env.sh"
merge_vault_env "$VAULT_ABS" "$ENV_FILE"

# ── Ensure .davyjones and .davyjones.env are gitignored ──────────────
if [ -f "$VAULT_ABS/.gitignore" ]; then
    if ! grep -q "^\.davyjones$" "$VAULT_ABS/.gitignore" 2>/dev/null; then
        echo ".davyjones" >> "$VAULT_ABS/.gitignore"
    fi
    if ! grep -q "^\.davyjones\.env$" "$VAULT_ABS/.gitignore" 2>/dev/null; then
        echo ".davyjones.env" >> "$VAULT_ABS/.gitignore"
    fi
fi

# ── Ensure vault is a git repo ──────────────────────────────────
if [ ! -d "$VAULT_ABS/.git" ]; then
    echo ""
    echo "Initializing git repo in vault..."
    cd "$VAULT_ABS"
    git init
    cat >> .gitignore << 'EOF'
.obsidian/workspace.json
.davyjones
.davyjones.env
EOF
    git add -A
    git commit -m "Initial vault commit"
fi

# ── Extract credentials ──────────────────────────────────────
echo ""
echo "Extracting Claude credentials from Keychain..."
if bash "$SCRIPT_DIR/extract_credentials.sh" 2>/dev/null; then
    echo "  Credentials ready."
else
    echo "  WARNING: Could not extract credentials."
    echo "  Run 'claude login' and then 'scripts/extract_credentials.sh' before starting."
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  scripts/start.sh"
