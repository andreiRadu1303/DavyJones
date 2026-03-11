#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLUGIN_SRC="$PROJECT_ROOT/obsidian-plugin"

# ── Current vault ───────────────────────────────────────────────
CURRENT=""
if [ -f "$PROJECT_ROOT/.env" ]; then
    CURRENT=$(grep "^VAULT_DIR=" "$PROJECT_ROOT/.env" 2>/dev/null | cut -d= -f2- || true)
fi

# ── Resolve vault path ─────────────────────────────────────────
if [ -n "${1:-}" ]; then
    if [[ "$1" == /* ]]; then
        VAULT_ABS="$1"
    else
        VAULT_ABS="$(cd "$PROJECT_ROOT" && cd "$1" 2>/dev/null && pwd)" || \
        VAULT_ABS="$PROJECT_ROOT/$1"
    fi
else
    # Read from Obsidian's vault registry
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
        exit 1
    fi

    echo "Vaults:"
    for i in "${!VAULTS[@]}"; do
        marker="  "
        [ "${VAULTS[$i]}" = "$CURRENT" ] && marker="* "
        plugin=""
        [ -d "${VAULTS[$i]}/.obsidian/plugins/davyjones" ] && plugin="" || plugin=" (no plugin)"
        echo "  ${marker}$((i+1)). ${VAULTS[$i]}${plugin}"
    done
    [ -n "$CURRENT" ] && echo "  (* = current)"
    echo ""
    read -rp "Select vault [1-${#VAULTS[@]}]: " choice
    VAULT_ABS="${VAULTS[$((choice-1))]}"
fi

if [ ! -d "$VAULT_ABS" ]; then
    echo "Error: $VAULT_ABS does not exist"
    exit 1
fi

if [ "$VAULT_ABS" = "$CURRENT" ]; then
    echo "Already using $VAULT_ABS"
    exit 0
fi

# ── Install plugin if missing ───────────────────────────────────
if [ ! -d "$VAULT_ABS/.obsidian/plugins/davyjones" ]; then
    echo "Plugin not found — installing..."
    bash "$SCRIPT_DIR/setup.sh" "$VAULT_ABS"
else
    # Just update .env
    ENV_FILE="$PROJECT_ROOT/.env"
    if grep -q "^VAULT_DIR=" "$ENV_FILE"; then
        if [[ "$(uname -s)" == "Darwin" ]]; then
            sed -i '' "s|^VAULT_DIR=.*|VAULT_DIR=$VAULT_ABS|" "$ENV_FILE"
        else
            sed -i "s|^VAULT_DIR=.*|VAULT_DIR=$VAULT_ABS|" "$ENV_FILE"
        fi
    else
        echo "VAULT_DIR=$VAULT_ABS" >> "$ENV_FILE"
    fi
fi

# ── Remove heartbeat from old vault ─────────────────────────────
if [ -n "$CURRENT" ] && [ -f "$CURRENT/.davyjones" ]; then
    rm -f "$CURRENT/.davyjones"
fi

# ── Merge vault-specific env (Slack/GitLab tokens) ────────────
source "$SCRIPT_DIR/_merge_env.sh"
merge_vault_env "$VAULT_ABS" "$PROJECT_ROOT/.env"

# ── Re-extract credentials (may have been refreshed since last extract) ──
echo ""
echo "Refreshing credentials from Keychain..."
bash "$SCRIPT_DIR/extract_credentials.sh" 2>/dev/null || echo "WARNING: credential extraction failed"

# ── Restart services ────────────────────────────────────────────
echo ""
echo "Switching to: $VAULT_ABS"
cd "$PROJECT_ROOT"
if ! docker compose down 2>&1; then
    echo "ERROR: docker compose down failed" >&2
    exit 1
fi
if ! docker compose up -d 2>&1; then
    echo "ERROR: docker compose up failed" >&2
    exit 1
fi
echo ""
echo "Done. Now using: $VAULT_ABS"
