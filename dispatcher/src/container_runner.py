"""Agent container runner — high-level interface for spawning task agents.

Delegates to the active ContainerRuntime (Docker or K8s) via the runtime
abstraction layer.  This module handles credential checks, prompt building,
and result parsing — the runtime handles container lifecycle.
"""

import json
import logging
import os
from typing import Callable, Optional

from src.config import AGENT_TIMEOUT_SECONDS
from src.models import DispatchPayload, TaskResult
from src.prompt_builder import build_prompt
from src.runtime import build_agent_env, get_runtime
from src.token_refresh import CredStatus, ensure_valid_token, get_cred_health
from src.vault_rules import load_vault_rules

logger = logging.getLogger(__name__)


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
    """
    if timeout is None:
        timeout = AGENT_TIMEOUT_SECONDS

    cred_error = _check_credentials()
    if cred_error is not None:
        raise RuntimeError(cred_error.error)

    env = build_agent_env(max_turns, output_format="json")
    runtime = get_runtime()
    return runtime.run(prompt, env, timeout)


def run_raw_streaming(
    prompt: str,
    max_turns: int = 20,
    timeout: int | None = None,
    on_output: Optional[Callable[[str], None]] = None,
) -> tuple[int, str, str]:
    """Like run_raw(), but uses stream-json output format to capture the full
    conversation (thinking, tool calls, results) as an execution log.

    Returns (exit_code, result_json, execution_log).
    """
    if timeout is None:
        timeout = AGENT_TIMEOUT_SECONDS

    cred_error = _check_credentials()
    if cred_error is not None:
        raise RuntimeError(cred_error.error)

    env = build_agent_env(max_turns, output_format="stream-json")
    runtime = get_runtime()
    return runtime.run_streaming(prompt, env, timeout, on_output)


def run_task(
    payload: DispatchPayload,
    timeout_override: int | None = None,
    on_output: Optional[Callable[[str], None]] = None,
) -> TaskResult:
    """Spawn an ephemeral agent container to process a task.

    High-level wrapper that builds the prompt, runs via run_raw_streaming()
    (stream-json mode), and parses the result into a TaskResult.
    """
    vault_rules = load_vault_rules()
    prompt = build_prompt(payload, vault_rules=vault_rules)
    max_turns = payload.metadata.get("max_iterations") or 20
    timeout = timeout_override or AGENT_TIMEOUT_SECONDS

    logger.info("Spawning agent container for %s (max_turns=%d, timeout=%ds)",
                payload.task_file_path, max_turns, timeout)

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
