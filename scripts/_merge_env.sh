#!/usr/bin/env bash
# Shared helper: merge vault-specific .davyjones.env into root .env
# Sourced by switch.sh and start.sh — not run directly.

# Vault-specific keys that get cleared/merged on switch
_VAULT_KEYS=(CLAUDE_CODE_OAUTH_TOKEN SLACK_BOT_TOKEN SLACK_APP_TOKEN GITLAB_TOKEN GITLAB_MCP_URL GITHUB_TOKEN GITHUB_REPO GITHUB_POLL_INTERVAL SLACK_MCP_ENABLED GITHUB_MCP_ENABLED GITLAB_MCP_ENABLED)

merge_vault_env() {
    local vault_dir="$1"
    local env_file="$2"  # root .env path
    local vault_env="$vault_dir/.davyjones.env"

    # Clear vault-specific keys to defaults first
    for key in "${_VAULT_KEYS[@]}"; do
        if grep -q "^${key}=" "$env_file" 2>/dev/null; then
            sed -i '' "s|^${key}=.*|${key}=|" "$env_file"
        fi
    done

    # Merge from vault's .davyjones.env if it exists
    if [ -f "$vault_env" ]; then
        while IFS= read -r line; do
            # Skip comments and blank lines
            [[ "$line" =~ ^[[:space:]]*# ]] && continue
            [[ -z "${line// /}" ]] && continue

            # Parse KEY=VALUE
            local key="${line%%=*}"
            local value="${line#*=}"
            key="$(echo "$key" | xargs)"  # trim whitespace

            [ -z "$key" ] && continue

            if grep -q "^${key}=" "$env_file" 2>/dev/null; then
                sed -i '' "s|^${key}=.*|${key}=${value}|" "$env_file"
            else
                echo "${key}=${value}" >> "$env_file"
            fi
        done < "$vault_env"
        echo "Loaded vault config from $vault_env"
    else
        echo "No .davyjones.env in vault (Slack/GitLab not configured for this vault)"
    fi
}
