import io
import json
import logging
import os
import tarfile

import docker
import requests.exceptions

from src.config import (
    AGENT_IMAGE,
    AGENT_TIMEOUT_SECONDS,
    CREDS_HOST_PATH,
    DOCKER_NETWORK,
    HTTP_PORT,
    VAULT_HOST_PATH,
)
from src.models import DispatchPayload, TaskResult
from src.prompt_builder import build_prompt
from src.token_refresh import CredStatus, ensure_valid_token, get_cred_health
from src.vault_rules import load_vault_rules

logger = logging.getLogger(__name__)

client = docker.from_env()


def _make_tar(name: str, data: bytes) -> io.BytesIO:
    """Create an in-memory tar archive containing a single file."""
    buf = io.BytesIO()
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


def _check_credentials() -> TaskResult | None:
    """Refresh credentials and fail fast if dead. Returns error result or None."""
    creds_local = "/tmp/claude-credentials.json"
    if os.path.isfile(creds_local):
        ensure_valid_token(creds_local)

    health = get_cred_health()
    if health.status == CredStatus.AUTH_EXPIRED:
        logger.error("Cannot dispatch: auth expired. Run 'claude login' on host.")
        return TaskResult(
            status="failed",
            error="Authentication expired. Run 'claude login' on the host "
                  "and click 'fix credentials' in Obsidian.",
        )
    if health.status == CredStatus.NO_CREDENTIALS:
        logger.error("Cannot dispatch: no credentials file.")
        return TaskResult(
            status="failed",
            error="No credentials found. Run 'scripts/start.sh' on the host.",
        )
    return None


def run_raw(
    prompt: str,
    max_turns: int = 20,
    timeout: int | None = None,
) -> tuple[int, str, str]:
    """Spawn an ephemeral agent container and return (exit_code, stdout, stderr).

    This is the low-level primitive used by both the overseer and task agents.
    Handles: credential check, container create, prompt injection, start,
    wait, log collection, and cleanup.

    Raises RuntimeError if credentials are dead.
    """
    if timeout is None:
        timeout = AGENT_TIMEOUT_SECONDS

    # Fail fast on dead credentials
    cred_error = _check_credentials()
    if cred_error is not None:
        raise RuntimeError(cred_error.error)

    container = None
    try:
        environment = {
            "MAX_TURNS": str(max_turns),
            "CLAUDE_CODE_OAUTH_TOKEN": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
            "GITLAB_TOKEN": os.environ.get("GITLAB_TOKEN", ""),
            "SLACK_BOT_TOKEN": os.environ.get("SLACK_BOT_TOKEN", ""),
            "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN", ""),
            "SLACK_MCP_ENABLED": os.environ.get("SLACK_MCP_ENABLED", "true"),
            "GITHUB_MCP_ENABLED": os.environ.get("GITHUB_MCP_ENABLED", "true"),
            "GITLAB_MCP_ENABLED": os.environ.get("GITLAB_MCP_ENABLED", "true"),
        }

        # Inject custom secrets from vault rules
        vault_rules = load_vault_rules()
        for key, value in vault_rules.get("secrets", {}).items():
            environment[key] = str(value)

        # Inject dynamic MCP instance URLs so the agent can discover them
        from src.mcp_manager import get_instance_urls
        mcp_instances = get_instance_urls()
        if mcp_instances:
            environment["DAVYJONES_MCP_INSTANCES"] = json.dumps(mcp_instances)

        # Expose dispatcher API so agents can query execution reports
        environment["DAVYJONES_API_URL"] = f"http://davyjones-dispatcher:{HTTP_PORT}"

        volumes = {
            VAULT_HOST_PATH: {"bind": "/vault", "mode": "rw"},
        }

        # Mount credentials from host if available
        if CREDS_HOST_PATH:
            volumes[CREDS_HOST_PATH] = {"bind": "/tmp/claude-credentials.json", "mode": "ro"}

        # Create container (not started) so we can inject the prompt file
        container = client.containers.create(
            image=AGENT_IMAGE,
            volumes=volumes,
            environment=environment,
            network=DOCKER_NETWORK,
            working_dir="/vault",
        )

        # Inject prompt file via Docker put_archive API
        prompt_tar = _make_tar("task-prompt.txt", prompt.encode("utf-8"))
        container.put_archive("/tmp", prompt_tar)

        # Now start
        container.start()
        logger.info("Container %s started (max_turns=%d, timeout=%ds)",
                    container.short_id, max_turns, timeout)

        # Wait for completion
        try:
            result = container.wait(timeout=timeout)
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout) as e:
            # Docker socket timeout — container is still running
            logger.error("Container %s timed out after %ds, killing it",
                         container.short_id, timeout)
            try:
                container.stop(timeout=10)
            except Exception:
                try:
                    container.kill()
                except Exception:
                    pass

            # Grab whatever logs the container produced before timeout
            stdout = ""
            stderr = ""
            try:
                stdout = container.logs(stdout=True, stderr=False).decode()
                stderr = container.logs(stdout=False, stderr=True).decode()
            except Exception:
                pass

            try:
                container.remove(force=True)
            except Exception:
                pass

            return 1, stdout, f"Container timed out after {timeout}s: {e}"

        exit_code = result.get("StatusCode", -1)
        stdout = container.logs(stdout=True, stderr=False).decode()
        stderr = container.logs(stdout=False, stderr=True).decode()

        logger.info("Container %s exited with code %d", container.short_id, exit_code)
        if stderr:
            logger.info("Container stderr: %s", stderr[:500])

        return exit_code, stdout, stderr
    finally:
        if container:
            try:
                container.remove(force=True)
            except Exception:
                pass


def run_task(
    payload: DispatchPayload,
    timeout_override: int | None = None,
) -> TaskResult:
    """Spawn an ephemeral agent container to process a task.

    High-level wrapper around run_raw() that builds the prompt and parses
    Claude CLI output into a TaskResult.
    """
    vault_rules = load_vault_rules()
    prompt = build_prompt(payload, vault_rules=vault_rules)
    max_turns = payload.metadata.get("max_iterations") or 20
    timeout = timeout_override or AGENT_TIMEOUT_SECONDS

    logger.info("Spawning agent container for %s (max_turns=%d, timeout=%ds)",
                payload.task_file_path, max_turns, timeout)

    try:
        exit_code, stdout, stderr = run_raw(
            prompt=prompt,
            max_turns=max_turns,
            timeout=timeout,
        )
    except RuntimeError as e:
        return TaskResult(status="failed", error=str(e))
    except Exception as e:
        logger.exception("Unexpected error running agent container for %s",
                         payload.task_file_path)
        return TaskResult(status="failed", error=f"Container error: {e}")

    if exit_code != 0:
        error_msg = stderr.strip() or stdout.strip() or "Unknown error"
        logger.error("Agent container failed: %s", error_msg[:500])
        return TaskResult(status="failed", error=error_msg[:2000])

    # Parse Claude CLI JSON output and detect max_turns
    output_text, hit_max = _parse_claude_output(stdout)
    if hit_max:
        logger.warning("Agent hit max_turns limit for %s", payload.task_file_path)
    return TaskResult(status="completed", output_text=output_text, hit_max_turns=hit_max)


def _parse_claude_output(stdout: str) -> tuple[str, bool]:
    """Parse Claude CLI JSON output into readable text.

    Returns (text, hit_max_turns).
    """
    hit_max = False
    try:
        output = json.loads(stdout)
        if isinstance(output, dict):
            # Detect error_max_turns in the CLI result envelope
            if output.get("subtype") == "error_max_turns":
                hit_max = True
            return output.get("result", stdout), hit_max
        elif isinstance(output, list):
            text_parts = []
            for block in output:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            return ("\n".join(text_parts) if text_parts else stdout), hit_max
        else:
            return str(output), hit_max
    except json.JSONDecodeError:
        return stdout, hit_max
