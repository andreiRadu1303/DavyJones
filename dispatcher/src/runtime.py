"""Container runtime abstraction.

Provides a pluggable interface for spawning ephemeral agent containers.
Two implementations:
  - DockerRuntime: uses Docker socket (local development)
  - K8sJobRuntime: uses Kubernetes Job API (cloud deployment)

The active runtime is selected by the RUNTIME_BACKEND env var.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Callable, Optional

from src.config import (
    AGENT_IMAGE,
    CREDS_HOST_PATH,
    DISPATCHER_HOSTNAME,
    DOCKER_NETWORK,
    HTTP_PORT,
    RUNTIME_BACKEND,
    VAULT_HOST_PATH,
    VAULT_SLUG,
)
from src.vault_rules import get_vault_env, load_vault_rules

logger = logging.getLogger(__name__)


def build_agent_env(max_turns: int, output_format: str = "json") -> dict[str, str]:
    """Build the common environment dict passed to every agent container."""
    env = {
        "MAX_TURNS": str(max_turns),
        "CLAUDE_CODE_OAUTH_TOKEN": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
        "GITLAB_TOKEN": os.environ.get("GITLAB_TOKEN", ""),
        "SLACK_BOT_TOKEN": os.environ.get("SLACK_BOT_TOKEN", ""),
        "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN", ""),
        "SLACK_MCP_ENABLED": os.environ.get("SLACK_MCP_ENABLED", "true"),
        "GITHUB_MCP_ENABLED": os.environ.get("GITHUB_MCP_ENABLED", "true"),
        "GITLAB_MCP_ENABLED": os.environ.get("GITLAB_MCP_ENABLED", "true"),
        "GOOGLE_WORKSPACE_ENABLED": get_vault_env("GOOGLE_WORKSPACE_ENABLED", "true"),
    }

    if output_format != "json":
        env["OUTPUT_FORMAT"] = output_format

    # Google Workspace CLI token
    gws_token = get_vault_env("GOOGLE_WORKSPACE_CLI_TOKEN")
    if gws_token:
        env["GOOGLE_WORKSPACE_CLI_TOKEN"] = gws_token

    # Custom secrets from vault rules
    vault_rules = load_vault_rules()
    for key, value in vault_rules.get("secrets", {}).items():
        env[key] = str(value)

    # Dynamic MCP instance URLs
    from src.mcp_manager import get_instance_urls
    mcp_instances = get_instance_urls()
    if mcp_instances:
        env["DAVYJONES_MCP_INSTANCES"] = json.dumps(mcp_instances)

    # Dispatcher API URL
    env["DAVYJONES_API_URL"] = f"http://{DISPATCHER_HOSTNAME}:{HTTP_PORT}"

    # MCP service URLs (project-scoped container names on shared network)
    _prefix = f"davyjones-{VAULT_SLUG}" if VAULT_SLUG != "default" else "davyjones"
    env["OBSIDIAN_MCP_URL"] = f"http://{_prefix}-obsidian-mcp-1:3010/mcp"
    env["SLACK_MCP_URL"] = f"http://{_prefix}-slack-mcp-1:3001/sse"
    env["GITLAB_MCP_URL"] = f"http://{_prefix}-gitlab-mcp-1:3002/sse"
    env["GITHUB_MCP_URL"] = f"http://{_prefix}-github-mcp-1:3003/sse"
    env["DAVYJONES_MCP_URL"] = f"http://{_prefix}-davyjones-mcp-1:3004/sse"

    return env


def build_agent_volumes() -> dict[str, dict]:
    """Build the volume mounts dict for agent containers (Docker format)."""
    volumes = {
        VAULT_HOST_PATH: {"bind": "/vault", "mode": "rw"},
    }
    if CREDS_HOST_PATH:
        volumes[CREDS_HOST_PATH] = {"bind": "/tmp/claude-credentials.json", "mode": "ro"}

    gws_config_path = get_vault_env("GWS_CONFIG_PATH")
    if gws_config_path:
        volumes[gws_config_path] = {"bind": "/home/agent/.config/gws", "mode": "rw"}

    return volumes


class ContainerRuntime(ABC):
    """Abstract base for container runtimes."""

    @abstractmethod
    def run(
        self,
        prompt: str,
        env: dict[str, str],
        timeout: int,
    ) -> tuple[int, str, str]:
        """Run agent container and return (exit_code, stdout, stderr).

        Used for simple JSON output mode.
        """
        ...

    @abstractmethod
    def run_streaming(
        self,
        prompt: str,
        env: dict[str, str],
        timeout: int,
        on_output: Optional[Callable[[str], None]] = None,
    ) -> tuple[int, str, str]:
        """Run agent container with stream-json output.

        Returns (exit_code, result_json, execution_log).
        on_output receives formatted log chunks in real-time.
        """
        ...


_runtime: ContainerRuntime | None = None


def get_runtime() -> ContainerRuntime:
    """Get or create the active container runtime."""
    global _runtime
    if _runtime is not None:
        return _runtime

    if RUNTIME_BACKEND == "k8s":
        from src.k8s_runtime import K8sJobRuntime
        _runtime = K8sJobRuntime()
        logger.info("Using Kubernetes Job runtime")
    else:
        from src.docker_runtime import DockerRuntime
        _runtime = DockerRuntime()
        logger.info("Using Docker runtime")

    return _runtime
