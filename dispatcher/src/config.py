import os


VAULT_PATH = os.environ.get("VAULT_PATH", "/vault")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "10"))
STATE_DIR = os.environ.get("STATE_DIR", "/app/state")
LAST_SHA_FILE = os.path.join(STATE_DIR, ".last_sha")
AGENT_TIMEOUT_SECONDS = int(os.environ.get("AGENT_TIMEOUT_SECONDS", "1200"))
AGENT_TIMEOUT_PER_TURN = int(os.environ.get("AGENT_TIMEOUT_PER_TURN", "20"))

# Slack listener (Socket Mode)
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
SLACK_MAX_TURNS = int(os.environ.get("SLACK_MAX_TURNS", "20"))

# Overseer agent
OVERSEER_TIMEOUT_SECONDS = int(os.environ.get("OVERSEER_TIMEOUT_SECONDS", "600"))
OVERSEER_MAX_TURNS = int(os.environ.get("OVERSEER_MAX_TURNS", "50"))
MAX_CONCURRENT_AGENTS = int(os.environ.get("MAX_CONCURRENT_AGENTS", "3"))

# GitHub Activity Monitoring
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_POLL_INTERVAL = int(os.environ.get("GITHUB_POLL_INTERVAL", "60"))

# MCP service toggles
SLACK_MCP_ENABLED = os.environ.get("SLACK_MCP_ENABLED", "true").lower() != "false"
GITHUB_MCP_ENABLED = os.environ.get("GITHUB_MCP_ENABLED", "true").lower() != "false"
GITLAB_MCP_ENABLED = os.environ.get("GITLAB_MCP_ENABLED", "true").lower() != "false"

# HTTP API for direct task submission
HTTP_PORT = int(os.environ.get("HTTP_PORT", "5555"))

# Scribe (report generator)
REPORTS_DIR = os.path.join(STATE_DIR, "reports")
MAX_REPORTS = int(os.environ.get("MAX_REPORTS", "200"))
SCRIBE_MAX_TURNS = int(os.environ.get("SCRIBE_MAX_TURNS", "5"))
SCRIBE_TIMEOUT = int(os.environ.get("SCRIBE_TIMEOUT", "120"))

# Docker container spawning
VAULT_HOST_PATH = os.environ.get("VAULT_HOST_PATH", "")
CREDS_HOST_PATH = os.environ.get("CREDS_HOST_PATH", "")
DOCKER_NETWORK = os.environ.get("DOCKER_NETWORK", "davyjones")
AGENT_IMAGE = os.environ.get("AGENT_IMAGE", "davyjones-agent")

# Multi-vault: dispatcher identity
DISPATCHER_HOSTNAME = os.environ.get("DISPATCHER_HOSTNAME", "davyjones-dispatcher-1")
VAULT_SLUG = os.environ.get("VAULT_SLUG", "default")

# Container runtime: "docker" (local) or "k8s" (cloud)
RUNTIME_BACKEND = os.environ.get("RUNTIME_BACKEND", "docker")
K8S_NAMESPACE = os.environ.get("K8S_NAMESPACE", "default")
K8S_AGENT_IMAGE = os.environ.get("K8S_AGENT_IMAGE", "davyjones-agent:latest")
