"""Kubernetes Job runtime — spawns agent containers as K8s Jobs.

Requires the `kubernetes` Python client and a valid kubeconfig or
in-cluster service account.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Callable, Optional

from src.config import K8S_AGENT_IMAGE, K8S_NAMESPACE
from src.runtime import ContainerRuntime

logger = logging.getLogger(__name__)

# Lazy import — only loaded when RUNTIME_BACKEND=k8s
_k8s_loaded = False
_batch_v1 = None
_core_v1 = None


def _ensure_k8s():
    """Load the kubernetes client on first use."""
    global _k8s_loaded, _batch_v1, _core_v1
    if _k8s_loaded:
        return

    from kubernetes import client, config as k8s_config

    try:
        k8s_config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes config")
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
        logger.info("Loaded local kubeconfig")

    _batch_v1 = client.BatchV1Api()
    _core_v1 = client.CoreV1Api()
    _k8s_loaded = True


def _format_stream_event(obj: dict) -> str:
    """Format a stream-json event (reused from docker_runtime)."""
    from src.docker_runtime import _format_stream_event
    return _format_stream_event(obj)


class K8sJobRuntime(ContainerRuntime):
    """Spawns agent containers as Kubernetes Jobs."""

    def __init__(self):
        _ensure_k8s()

    def run(
        self,
        prompt: str,
        env: dict[str, str],
        timeout: int,
    ) -> tuple[int, str, str]:
        from kubernetes import client

        job_name = f"agent-{int(time.time() * 1000) % 1_000_000}"
        configmap_name = f"{job_name}-prompt"
        namespace = K8S_NAMESPACE

        try:
            # Create ConfigMap with the prompt
            _core_v1.create_namespaced_config_map(
                namespace=namespace,
                body=client.V1ConfigMap(
                    metadata=client.V1ObjectMeta(name=configmap_name),
                    data={"prompt.txt": prompt},
                ),
            )

            # Build env vars list
            env_vars = [
                client.V1EnvVar(name=k, value=v) for k, v in env.items()
            ]

            # Create Job
            job = client.V1Job(
                metadata=client.V1ObjectMeta(name=job_name),
                spec=client.V1JobSpec(
                    backoff_limit=0,
                    active_deadline_seconds=timeout,
                    ttl_seconds_after_finished=300,
                    template=client.V1PodTemplateSpec(
                        spec=client.V1PodSpec(
                            restart_policy="Never",
                            containers=[
                                client.V1Container(
                                    name="agent",
                                    image=K8S_AGENT_IMAGE,
                                    env=env_vars,
                                    working_dir="/vault",
                                    volume_mounts=[
                                        client.V1VolumeMount(
                                            name="vault",
                                            mount_path="/vault",
                                        ),
                                        client.V1VolumeMount(
                                            name="prompt",
                                            mount_path="/tmp/task-prompt.txt",
                                            sub_path="prompt.txt",
                                        ),
                                    ],
                                ),
                            ],
                            volumes=[
                                client.V1Volume(
                                    name="vault",
                                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                                        claim_name="vault-data",
                                    ),
                                ),
                                client.V1Volume(
                                    name="prompt",
                                    config_map=client.V1ConfigMapVolumeSource(
                                        name=configmap_name,
                                    ),
                                ),
                            ],
                        ),
                    ),
                ),
            )

            _batch_v1.create_namespaced_job(namespace=namespace, body=job)
            logger.info("K8s Job %s created (timeout=%ds)", job_name, timeout)

            # Wait for completion
            exit_code, stdout, stderr = self._wait_for_job(namespace, job_name, timeout)
            return exit_code, stdout, stderr

        finally:
            # Cleanup ConfigMap (Job auto-cleans via TTL)
            try:
                _core_v1.delete_namespaced_config_map(configmap_name, namespace)
            except Exception:
                pass

    def run_streaming(
        self,
        prompt: str,
        env: dict[str, str],
        timeout: int,
        on_output: Optional[Callable[[str], None]] = None,
    ) -> tuple[int, str, str]:
        # For now, streaming uses the same Job approach but processes logs
        # after completion. Real-time streaming requires log follow.
        env["OUTPUT_FORMAT"] = "stream-json"
        exit_code, stdout, stderr = self.run(prompt, env, timeout)

        # Parse stream-json output into execution log
        log_parts = []
        result_json = ""
        for raw_line in stdout.split("\n"):
            line = raw_line.strip()
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
                else:
                    formatted = _format_stream_event(obj)
                    if formatted:
                        log_parts.append(formatted)
            except json.JSONDecodeError:
                log_parts.append(line)

        execution_log = "\n".join(log_parts)
        stdout_final = result_json if result_json else stdout

        if on_output and execution_log:
            try:
                on_output(execution_log)
            except Exception:
                pass

        return exit_code, stdout_final, execution_log

    def _wait_for_job(
        self,
        namespace: str,
        job_name: str,
        timeout: int,
    ) -> tuple[int, str, str]:
        """Poll until a K8s Job completes, then collect logs."""
        from kubernetes import client, watch

        deadline = time.time() + timeout + 30  # grace period
        pod_name = None

        # Wait for pod to be created and finish
        while time.time() < deadline:
            job = _batch_v1.read_namespaced_job(job_name, namespace)

            if job.status.succeeded and job.status.succeeded > 0:
                break
            if job.status.failed and job.status.failed > 0:
                break

            time.sleep(2)
        else:
            logger.error("K8s Job %s timed out", job_name)
            try:
                _batch_v1.delete_namespaced_job(
                    job_name, namespace,
                    body=client.V1DeleteOptions(propagation_policy="Background"),
                )
            except Exception:
                pass
            return 1, "", f"Job timed out after {timeout}s"

        # Find the pod
        pods = _core_v1.list_namespaced_pod(
            namespace,
            label_selector=f"job-name={job_name}",
        )
        if pods.items:
            pod_name = pods.items[0].metadata.name

        # Collect logs
        stdout = stderr = ""
        if pod_name:
            try:
                stdout = _core_v1.read_namespaced_pod_log(
                    pod_name, namespace, container="agent",
                )
            except Exception as e:
                logger.warning("Failed to read pod logs: %s", e)

        # Determine exit code
        exit_code = 0
        if job.status.failed and job.status.failed > 0:
            exit_code = 1

        return exit_code, stdout, stderr
