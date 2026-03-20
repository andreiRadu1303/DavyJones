import io
import json
import logging
import os
import tarfile
import threading
from typing import Callable, Optional

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
            "GOOGLE_WORKSPACE_ENABLED": os.environ.get("GOOGLE_WORKSPACE_ENABLED", "true"),
        }

        # Google Workspace CLI: pass token and credentials file path if available
        gws_token = os.environ.get("GOOGLE_WORKSPACE_CLI_TOKEN", "")
        if gws_token:
            environment["GOOGLE_WORKSPACE_CLI_TOKEN"] = gws_token

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

        # Mount Google Workspace CLI credentials if available
        # Note: path is a HOST path, not accessible from within the dispatcher container,
        # so we skip os.path.isdir() and trust the env var — Docker will mount it.
        # Mounted rw so gws can cache refreshed access tokens.
        gws_config_path = os.environ.get("GWS_CONFIG_PATH", "")
        if gws_config_path:
            volumes[gws_config_path] = {"bind": "/home/agent/.config/gws", "mode": "rw"}
            environment["GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"] = "/home/agent/.config/gws/credentials.json"

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


def _format_stream_event(obj: dict) -> str:
    """Format a stream-json event into a human-readable line for the execution log."""
    etype = obj.get("type", "")

    if etype == "system":
        return ""  # skip init noise

    if etype == "assistant":
        msg = obj.get("message", {})
        parts = []
        for block in msg.get("content", []):
            if block.get("type") == "text":
                parts.append(block["text"])
            elif block.get("type") == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
                # Show a concise version of the tool input
                inp_str = json.dumps(inp, ensure_ascii=False)
                if len(inp_str) > 300:
                    inp_str = inp_str[:300] + "..."
                parts.append(f"[tool_use] {name}: {inp_str}")
        return "\n".join(parts) if parts else ""

    if etype == "user":
        msg = obj.get("message", {})
        parts = []
        for block in msg.get("content", []):
            if block.get("type") == "tool_result":
                content = block.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "") for b in content if isinstance(b, dict)
                    )
                if len(content) > 500:
                    content = content[:500] + "..."
                parts.append(f"[tool_result] {content}")
        return "\n".join(parts) if parts else ""

    return ""


def run_raw_streaming(
    prompt: str,
    max_turns: int = 20,
    timeout: int | None = None,
    on_output: Optional[Callable[[str], None]] = None,
) -> tuple[int, str, str]:
    """Like run_raw(), but uses stream-json output format to capture the full
    conversation (thinking, tool calls, results) as an execution log.

    Streams stdout (stream-json lines) in a background thread.  Each line is
    parsed and formatted into a human-readable execution log.  The on_output
    callback receives formatted chunks as they arrive (for live display).

    Returns (exit_code, result_json, execution_log):
      - result_json: the final result extracted from the stream (or raw stdout)
      - execution_log: formatted conversation log (replaces stderr in the tuple)
    """
    if timeout is None:
        timeout = AGENT_TIMEOUT_SECONDS

    cred_error = _check_credentials()
    if cred_error is not None:
        raise RuntimeError(cred_error.error)

    container = None
    stdout_buf: list[str] = []
    stderr_buf: list[str] = []
    log_parts: list[str] = []
    result_json = ""

    def _stream_stderr(ctr):
        """Background thread: capture stderr (entrypoint diagnostics, errors)."""
        try:
            for chunk in ctr.logs(stream=True, follow=True, stdout=False, stderr=True):
                text = chunk.decode("utf-8", errors="replace")
                stderr_buf.append(text)
        except Exception:
            pass

    def _stream_stdout(ctr):
        """Background thread: read stream-json lines, build execution log."""
        nonlocal result_json
        line_buf = ""
        try:
            for chunk in ctr.logs(stream=True, follow=True, stdout=True, stderr=False):
                text = chunk.decode("utf-8", errors="replace")
                stdout_buf.append(text)
                line_buf += text

                # Process complete lines
                while "\n" in line_buf:
                    line, line_buf = line_buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if obj.get("type") == "result":
                            # This is the final result — extract it
                            result_json = json.dumps(obj)
                            # Also add a summary to the log
                            log_line = f"[result] {obj.get('subtype', 'success')} | turns: {obj.get('num_turns', '?')} | cost: ${obj.get('cost_usd', 0):.4f}"
                            log_parts.append(log_line)
                            if on_output:
                                try:
                                    on_output(log_line + "\n")
                                except Exception:
                                    pass
                        else:
                            formatted = _format_stream_event(obj)
                            if formatted:
                                log_parts.append(formatted)
                                if on_output:
                                    try:
                                        on_output(formatted + "\n")
                                    except Exception:
                                        pass
                    except json.JSONDecodeError:
                        # Not JSON — include as-is (e.g. raw text from entrypoint)
                        log_parts.append(line)
                        if on_output:
                            try:
                                on_output(line + "\n")
                            except Exception:
                                pass

                # Handle remaining partial line on final chunk
            if line_buf.strip():
                log_parts.append(line_buf.strip())
        except Exception:
            pass  # container removed or stopped

    try:
        environment = {
            "MAX_TURNS": str(max_turns),
            "OUTPUT_FORMAT": "stream-json",  # stream-json for full conversation
            "CLAUDE_CODE_OAUTH_TOKEN": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
            "GITLAB_TOKEN": os.environ.get("GITLAB_TOKEN", ""),
            "SLACK_BOT_TOKEN": os.environ.get("SLACK_BOT_TOKEN", ""),
            "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN", ""),
            "SLACK_MCP_ENABLED": os.environ.get("SLACK_MCP_ENABLED", "true"),
            "GITHUB_MCP_ENABLED": os.environ.get("GITHUB_MCP_ENABLED", "true"),
            "GITLAB_MCP_ENABLED": os.environ.get("GITLAB_MCP_ENABLED", "true"),
            "GOOGLE_WORKSPACE_ENABLED": os.environ.get("GOOGLE_WORKSPACE_ENABLED", "true"),
        }

        # Google Workspace CLI: pass token and credentials file path if available
        gws_token = os.environ.get("GOOGLE_WORKSPACE_CLI_TOKEN", "")
        if gws_token:
            environment["GOOGLE_WORKSPACE_CLI_TOKEN"] = gws_token

        vault_rules = load_vault_rules()
        for key, value in vault_rules.get("secrets", {}).items():
            environment[key] = str(value)

        from src.mcp_manager import get_instance_urls
        mcp_instances = get_instance_urls()
        if mcp_instances:
            environment["DAVYJONES_MCP_INSTANCES"] = json.dumps(mcp_instances)

        environment["DAVYJONES_API_URL"] = f"http://davyjones-dispatcher:{HTTP_PORT}"

        volumes = {
            VAULT_HOST_PATH: {"bind": "/vault", "mode": "rw"},
        }
        if CREDS_HOST_PATH:
            volumes[CREDS_HOST_PATH] = {"bind": "/tmp/claude-credentials.json", "mode": "ro"}

        # Mount Google Workspace CLI credentials if available
        # Note: path is a HOST path, not accessible from within the dispatcher container,
        # so we skip os.path.isdir() and trust the env var — Docker will mount it.
        # Mounted rw so gws can cache refreshed access tokens.
        gws_config_path = os.environ.get("GWS_CONFIG_PATH", "")
        if gws_config_path:
            volumes[gws_config_path] = {"bind": "/home/agent/.config/gws", "mode": "rw"}
            environment["GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"] = "/home/agent/.config/gws/credentials.json"

        container = client.containers.create(
            image=AGENT_IMAGE,
            volumes=volumes,
            environment=environment,
            network=DOCKER_NETWORK,
            working_dir="/vault",
        )

        prompt_tar = _make_tar("task-prompt.txt", prompt.encode("utf-8"))
        container.put_archive("/tmp", prompt_tar)

        container.start()
        logger.info("Container %s started [stream-json] (max_turns=%d, timeout=%ds)",
                    container.short_id, max_turns, timeout)

        # Start streaming stdout + stderr in background
        stream_thread = threading.Thread(
            target=_stream_stdout, args=(container,), daemon=True,
        )
        stream_thread.start()
        stderr_thread = threading.Thread(
            target=_stream_stderr, args=(container,), daemon=True,
        )
        stderr_thread.start()

        # Wait for completion
        try:
            wait_result = container.wait(timeout=timeout)
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout) as e:
            logger.error("Container %s timed out after %ds, killing it",
                         container.short_id, timeout)
            try:
                container.stop(timeout=10)
            except Exception:
                try:
                    container.kill()
                except Exception:
                    pass

            stream_thread.join(timeout=5)
            stderr_thread.join(timeout=2)
            try:
                container.remove(force=True)
            except Exception:
                pass

            execution_log = "\n".join(log_parts) or "".join(stderr_buf).strip()
            return 1, result_json or "".join(stdout_buf), execution_log

        stream_thread.join(timeout=5)
        stderr_thread.join(timeout=2)

        exit_code = wait_result.get("StatusCode", -1)
        stderr_text = "".join(stderr_buf).strip()
        execution_log = "\n".join(log_parts)

        # If we didn't extract a result from stream, fall back to raw stdout
        stdout_final = result_json if result_json else "".join(stdout_buf)

        # If no stdout log but we have stderr, include it (e.g. entrypoint errors)
        if not execution_log and stderr_text:
            execution_log = stderr_text

        logger.info("Container %s exited with code %d (log: %d lines, stderr: %d chars)",
                    container.short_id, exit_code, len(log_parts), len(stderr_text))

        return exit_code, stdout_final, execution_log
    finally:
        if container:
            try:
                container.remove(force=True)
            except Exception:
                pass


def run_task(
    payload: DispatchPayload,
    timeout_override: int | None = None,
    on_output: Optional[Callable[[str], None]] = None,
) -> TaskResult:
    """Spawn an ephemeral agent container to process a task.

    High-level wrapper that builds the prompt, runs via run_raw_streaming()
    (stream-json mode), and parses the result into a TaskResult.

    The on_output callback receives formatted execution log chunks in
    real-time (thinking, tool calls, results).
    """
    vault_rules = load_vault_rules()
    prompt = build_prompt(payload, vault_rules=vault_rules)
    max_turns = payload.metadata.get("max_iterations") or 20
    timeout = timeout_override or AGENT_TIMEOUT_SECONDS

    logger.info("Spawning agent container for %s (max_turns=%d, timeout=%ds)",
                payload.task_file_path, max_turns, timeout)

    # Always use streaming so we capture the full execution log
    try:
        exit_code, result_json, execution_log = run_raw_streaming(
            prompt=prompt,
            max_turns=max_turns,
            timeout=timeout,
            on_output=on_output,
        )
    except RuntimeError as e:
        return TaskResult(status="failed", error=str(e))
    except Exception as e:
        logger.exception("Unexpected error running agent container for %s",
                         payload.task_file_path)
        return TaskResult(status="failed", error=f"Container error: {e}")

    if exit_code != 0:
        error_msg = execution_log.strip() or result_json.strip() or "Unknown error"
        logger.error("Agent container failed: %s", error_msg[:500])
        return TaskResult(status="failed", error=error_msg[:2000], execution_log=execution_log)

    # Parse the result JSON and detect max_turns
    output_text, hit_max = _parse_claude_output(result_json)
    if hit_max:
        logger.warning("Agent hit max_turns limit for %s", payload.task_file_path)
    return TaskResult(status="completed", output_text=output_text, hit_max_turns=hit_max,
                      execution_log=execution_log)


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
