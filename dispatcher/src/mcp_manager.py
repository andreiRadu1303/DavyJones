"""MCP Manager — dynamic Docker containers for additional service instances.

When users configure multiple GitHub/GitLab accounts, the manager ensures
a dedicated MCP server container exists for each instance.  Containers
are created on the shared Docker network so agents can reach them via SSE.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import docker
import docker.errors

from src.config import DOCKER_NETWORK, VAULT_SLUG

logger = logging.getLogger(__name__)

_client: docker.DockerClient | None = None
_lock = threading.Lock()

# Tracks running instances: {instance_id: {service, label, container_name, port, url}}
_running: dict[str, dict[str, Any]] = {}

# Port pool for dynamic MCP containers (4001-4999)
_PORT_START = 4001
_next_port = _PORT_START

# Label used to tag managed containers so we can find them on startup
_LABEL_KEY = "davyjones.mcp-instance"

# Image / env config per service type
_SERVICE_CONFIG: dict[str, dict[str, Any]] = {
    "github": {
        "image": "davyjones-github-mcp",
        "default_port": 3003,
        "env_fn": lambda inst, port: {
            "GITHUB_PERSONAL_ACCESS_TOKEN": inst.get("token", ""),
        },
    },
    "gitlab": {
        "image": "zereight050/gitlab-mcp:latest",
        "default_port": 3002,
        "env_fn": lambda inst, port: {
            "GITLAB_PERSONAL_ACCESS_TOKEN": inst.get("token", ""),
            "GITLAB_API_URL": inst.get("config", {}).get("apiUrl", "https://gitlab.com"),
            "SSE": "true",
            "HOST": "0.0.0.0",
            "PORT": str(port),
        },
    },
}


def _get_client() -> docker.DockerClient:
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def _container_name(service: str, instance_id: str) -> str:
    """Derive a deterministic container name, scoped to this vault."""
    safe_id = instance_id.lower().replace(" ", "-")
    return f"davyjones-{VAULT_SLUG}-{service}-mcp-{safe_id}"


def _allocate_port() -> int:
    """Allocate the next available port."""
    global _next_port
    port = _next_port
    _next_port += 1
    if _next_port > 4999:
        _next_port = _PORT_START
    return port


def _is_container_running(client: docker.DockerClient, name: str) -> bool:
    """Check if a container exists and is running."""
    try:
        c = client.containers.get(name)
        return c.status == "running"
    except docker.errors.NotFound:
        return False


def _remove_container(client: docker.DockerClient, name: str) -> None:
    """Stop and remove a container if it exists."""
    try:
        c = client.containers.get(name)
        c.stop(timeout=10)
        c.remove(force=True)
        logger.info("Removed MCP container: %s", name)
    except docker.errors.NotFound:
        pass
    except Exception:
        logger.exception("Failed to remove container %s", name)


def _start_container(
    client: docker.DockerClient,
    instance: dict,
    port: int,
) -> str | None:
    """Create and start an MCP container for the given instance.

    Returns the container name on success, None on failure.
    """
    service = instance.get("service", "")
    instance_id = instance.get("id", "")
    svc_config = _SERVICE_CONFIG.get(service)

    if not svc_config:
        logger.error("Unknown service type '%s' for instance '%s'", service, instance_id)
        return None

    name = _container_name(service, instance_id)
    image = svc_config["image"]
    environment = svc_config["env_fn"](instance, port)

    try:
        # Remove stale container if exists
        _remove_container(client, name)

        container = client.containers.run(
            image=image,
            name=name,
            environment=environment,
            network=DOCKER_NETWORK,
            labels={_LABEL_KEY: "true", "davyjones.instance-id": instance_id},
            detach=True,
            restart_policy={"Name": "unless-stopped"},
        )
        logger.info(
            "Started MCP container: %s (image=%s, port=%d, network=%s)",
            name, image, port, DOCKER_NETWORK,
        )
        return name
    except Exception:
        logger.exception("Failed to start MCP container for instance '%s'", instance_id)
        return None


def sync(instances: list[dict]) -> None:
    """Synchronise running MCP containers with the desired instance list.

    - Starts containers for new instances
    - Removes containers for instances no longer in the list
    """
    with _lock:
        client = _get_client()
        desired_ids = set()

        for inst in instances:
            inst_id = inst.get("id", "")
            service = inst.get("service", "")
            token = inst.get("token", "")

            if not inst_id or not service or not token:
                logger.warning("Skipping invalid service instance: %s", inst)
                continue
            if service not in _SERVICE_CONFIG:
                logger.warning("Unsupported service '%s' for instance '%s'", service, inst_id)
                continue

            desired_ids.add(inst_id)
            name = _container_name(service, inst_id)

            # Already running?
            if inst_id in _running and _is_container_running(client, name):
                logger.debug("MCP container already running: %s", name)
                continue

            # Start new container
            port = _allocate_port()
            started = _start_container(client, inst, port)
            if started:
                # For GitHub, the image always exposes 3003 internally regardless of port
                # For GitLab, the PORT env var controls it
                internal_port = port if service == "gitlab" else _SERVICE_CONFIG[service]["default_port"]
                _running[inst_id] = {
                    "service": service,
                    "label": inst.get("label", inst_id),
                    "container_name": name,
                    "port": internal_port,
                    "url": f"http://{name}:{internal_port}/sse",
                }

        # Remove containers for instances no longer desired
        stale_ids = set(_running.keys()) - desired_ids
        for stale_id in stale_ids:
            info = _running.pop(stale_id)
            _remove_container(client, info["container_name"])

        # Also clean up any orphaned containers with our label
        try:
            labeled = client.containers.list(
                all=True, filters={"label": _LABEL_KEY}
            )
            known_names = {info["container_name"] for info in _running.values()}
            for c in labeled:
                if c.name not in known_names:
                    logger.info("Cleaning up orphaned MCP container: %s", c.name)
                    try:
                        c.stop(timeout=10)
                        c.remove(force=True)
                    except Exception:
                        pass
        except Exception:
            logger.exception("Error cleaning up orphaned MCP containers")

        logger.info(
            "MCP sync complete: %d instances running",
            len(_running),
        )


def get_instance_urls() -> list[dict]:
    """Return connection info for all running dynamic MCP instances."""
    with _lock:
        return [
            {
                "id": inst_id,
                "service": info["service"],
                "label": info["label"],
                "url": info["url"],
            }
            for inst_id, info in _running.items()
        ]
