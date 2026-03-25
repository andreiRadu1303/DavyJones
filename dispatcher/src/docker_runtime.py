"""Docker container runtime — spawns agent containers via Docker socket."""

from __future__ import annotations

import io
import json
import logging
import tarfile
import threading
from typing import Callable, Optional

import docker
import requests.exceptions

from src.config import AGENT_IMAGE, DOCKER_NETWORK
from src.runtime import ContainerRuntime, build_agent_volumes

logger = logging.getLogger(__name__)


def _make_tar(name: str, data: bytes) -> io.BytesIO:
    """Create an in-memory tar archive containing a single file."""
    buf = io.BytesIO()
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


def _format_stream_event(obj: dict) -> str:
    """Format a stream-json event into a human-readable line for the execution log."""
    etype = obj.get("type", "")

    if etype == "system":
        return ""

    if etype == "assistant":
        msg = obj.get("message", {})
        parts = []
        for block in msg.get("content", []):
            if block.get("type") == "text":
                parts.append(block["text"])
            elif block.get("type") == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
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


class DockerRuntime(ContainerRuntime):
    """Spawns agent containers using the Docker socket."""

    def __init__(self):
        self._client = docker.from_env()

    def run(
        self,
        prompt: str,
        env: dict[str, str],
        timeout: int,
    ) -> tuple[int, str, str]:
        volumes = build_agent_volumes()
        container = None
        try:
            container = self._client.containers.create(
                image=AGENT_IMAGE,
                volumes=volumes,
                environment=env,
                network=DOCKER_NETWORK,
                working_dir="/vault",
            )
            prompt_tar = _make_tar("task-prompt.txt", prompt.encode("utf-8"))
            container.put_archive("/tmp", prompt_tar)
            container.start()
            logger.info("Container %s started (timeout=%ds)", container.short_id, timeout)

            try:
                result = container.wait(timeout=timeout)
            except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout) as e:
                logger.error("Container %s timed out after %ds", container.short_id, timeout)
                try:
                    container.stop(timeout=10)
                except Exception:
                    try:
                        container.kill()
                    except Exception:
                        pass
                stdout = stderr = ""
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
            return exit_code, stdout, stderr
        finally:
            if container:
                try:
                    container.remove(force=True)
                except Exception:
                    pass

    def run_streaming(
        self,
        prompt: str,
        env: dict[str, str],
        timeout: int,
        on_output: Optional[Callable[[str], None]] = None,
    ) -> tuple[int, str, str]:
        volumes = build_agent_volumes()
        container = None
        stdout_buf: list[str] = []
        stderr_buf: list[str] = []
        log_parts: list[str] = []
        result_json = ""

        def _stream_stderr(ctr):
            try:
                for chunk in ctr.logs(stream=True, follow=True, stdout=False, stderr=True):
                    stderr_buf.append(chunk.decode("utf-8", errors="replace"))
            except Exception:
                pass

        def _stream_stdout(ctr):
            nonlocal result_json
            line_buf = ""
            try:
                for chunk in ctr.logs(stream=True, follow=True, stdout=True, stderr=False):
                    text = chunk.decode("utf-8", errors="replace")
                    stdout_buf.append(text)
                    line_buf += text

                    while "\n" in line_buf:
                        line, line_buf = line_buf.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            if obj.get("type") == "result":
                                result_json = json.dumps(obj)
                                log_line = (
                                    f"[result] {obj.get('subtype', 'success')} | "
                                    f"turns: {obj.get('num_turns', '?')} | "
                                    f"cost: ${obj.get('total_cost_usd', 0):.4f}"
                                )
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
                            log_parts.append(line)
                            if on_output:
                                try:
                                    on_output(line + "\n")
                                except Exception:
                                    pass

                if line_buf.strip():
                    log_parts.append(line_buf.strip())
            except Exception:
                pass

        try:
            container = self._client.containers.create(
                image=AGENT_IMAGE,
                volumes=volumes,
                environment=env,
                network=DOCKER_NETWORK,
                working_dir="/vault",
            )
            prompt_tar = _make_tar("task-prompt.txt", prompt.encode("utf-8"))
            container.put_archive("/tmp", prompt_tar)
            container.start()
            logger.info("Container %s started [stream-json] (timeout=%ds)",
                        container.short_id, timeout)

            stream_thread = threading.Thread(target=_stream_stdout, args=(container,), daemon=True)
            stream_thread.start()
            stderr_thread = threading.Thread(target=_stream_stderr, args=(container,), daemon=True)
            stderr_thread.start()

            try:
                wait_result = container.wait(timeout=timeout)
            except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout):
                logger.error("Container %s timed out after %ds", container.short_id, timeout)
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
            stdout_final = result_json if result_json else "".join(stdout_buf)

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
