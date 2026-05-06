"""K8s provisioner — creates per-vault namespace, PVCs, Deployment, and Service."""

import logging
import os
import string

logger = logging.getLogger(__name__)

# Tier resource quotas
TIER_QUOTAS = {
    "free":  {"max_pods": "10",  "max_cpu": "1",    "max_memory": "2Gi",  "replicas": "0"},
    "pro":   {"max_pods": "20",  "max_cpu": "4",    "max_memory": "8Gi",  "replicas": "1"},
    "team":  {"max_pods": "50",  "max_cpu": "10",   "max_memory": "20Gi", "replicas": "1"},
}


def _load_template() -> str:
    """Load vault-template.yaml from the expected path."""
    from cloud.config import settings

    # Try the configured path first, then relative paths
    candidates = [
        settings.k8s_vault_template_path,
        "/app/k8s/vault-template.yaml",
        os.path.join(os.path.dirname(__file__), "..", "k8s", "base", "vault-template.yaml"),
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path) as f:
                return f.read()
    raise FileNotFoundError("vault-template.yaml not found in any expected location")


def _render_template(template: str, variables: dict) -> str:
    """Replace ${VAR} placeholders in the template."""
    for key, value in variables.items():
        template = template.replace(f"${{{key}}}", str(value))
    return template


async def provision_vault(
    user_id: str,
    vault_id: str,
    vault_slug: str,
    git_repo_url: str,
    claude_token: str = "",
    plan: str = "free",
) -> None:
    """
    Create the K8s namespace and all vault resources for a new vault.

    Creates:
    - Namespace dj-{user_id[:8]}
    - PVCs for vault data + dispatcher state
    - K8s Secret with credentials
    - Dispatcher Deployment + Service
    - obsidian-mcp Deployment + Service
    - ResourceQuota
    - ServiceAccount + RBAC for dispatcher (to spawn Jobs)
    """
    try:
        from kubernetes import client as k8s_client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
    except ImportError:
        logger.warning("kubernetes client not installed — skipping K8s provisioning")
        return
    except Exception as e:
        logger.warning(f"K8s config not available — skipping provisioning: {e}")
        return

    from cloud.config import settings

    namespace = f"{settings.k8s_namespace_prefix}{user_id[:8]}"
    quotas = TIER_QUOTAS.get(plan, TIER_QUOTAS["free"])

    # ── 1. Ensure namespace exists ──────────────────────────────────────────
    core = k8s_client.CoreV1Api()
    try:
        core.read_namespace(namespace)
        logger.info(f"Namespace {namespace} already exists")
    except k8s_client.exceptions.ApiException as e:
        if e.status == 404:
            ns = k8s_client.V1Namespace(
                metadata=k8s_client.V1ObjectMeta(
                    name=namespace,
                    labels={"app": "davyjones", "user": user_id[:8]},
                )
            )
            core.create_namespace(ns)
            logger.info(f"Created namespace {namespace}")
        else:
            raise

    # ── 2. Apply ServiceAccount + RBAC for dispatcher ──────────────────────
    rbac = k8s_client.RbacAuthorizationV1Api()

    # ServiceAccount
    try:
        core.read_namespaced_service_account("dispatcher", namespace)
    except k8s_client.exceptions.ApiException as e:
        if e.status == 404:
            core.create_namespaced_service_account(
                namespace,
                k8s_client.V1ServiceAccount(
                    metadata=k8s_client.V1ObjectMeta(name="dispatcher")
                ),
            )

    # Role: can create/get/delete Jobs and ConfigMaps
    role_name = "dispatcher-role"
    role = k8s_client.V1Role(
        metadata=k8s_client.V1ObjectMeta(name=role_name, namespace=namespace),
        rules=[
            k8s_client.V1PolicyRule(
                api_groups=["batch"],
                resources=["jobs"],
                verbs=["create", "get", "list", "watch", "delete"],
            ),
            k8s_client.V1PolicyRule(
                api_groups=[""],
                resources=["configmaps", "pods", "pods/log"],
                verbs=["create", "get", "list", "watch", "delete"],
            ),
        ],
    )
    try:
        rbac.replace_namespaced_role(role_name, namespace, role)
    except k8s_client.exceptions.ApiException as e:
        if e.status == 404:
            rbac.create_namespaced_role(namespace, role)
        else:
            raise

    # RoleBinding
    rb = k8s_client.V1RoleBinding(
        metadata=k8s_client.V1ObjectMeta(name="dispatcher-rolebinding", namespace=namespace),
        role_ref=k8s_client.V1RoleRef(
            api_group="rbac.authorization.k8s.io",
            kind="Role",
            name=role_name,
        ),
        subjects=[{"kind": "ServiceAccount", "name": "dispatcher", "namespace": namespace}],
    )
    try:
        rbac.replace_namespaced_role_binding("dispatcher-rolebinding", namespace, rb)
    except k8s_client.exceptions.ApiException as e:
        if e.status == 404:
            rbac.create_namespaced_role_binding(namespace, rb)
        else:
            raise

    # ── 3. Render and apply vault-template.yaml ────────────────────────────
    # Build authenticated git URL for the init container clone
    from cloud.config import settings
    from urllib.parse import urlparse, urlunparse
    if settings.forgejo_admin_token and git_repo_url:
        parsed = urlparse(git_repo_url)
        git_auth_url = urlunparse((
            parsed.scheme,
            f"{settings.forgejo_admin_user}:{settings.forgejo_admin_token}@{parsed.netloc}",
            parsed.path, "", "", "",
        ))
    else:
        git_auth_url = git_repo_url

    template = _load_template()
    rendered = _render_template(template, {
        "USER_ID": user_id[:8],
        "VAULT_SLUG": vault_slug,
        "VAULT_ID": vault_id,
        "GIT_REPO_URL": git_auth_url,
        "CLAUDE_TOKEN": claude_token,
        "GITHUB_TOKEN": "",
        "GITLAB_TOKEN": "",
        "SLACK_BOT_TOKEN": "",
        "SLACK_APP_TOKEN": "",
        "REPLICAS": quotas["replicas"],
        "MAX_PODS": quotas["max_pods"],
        "MAX_CPU": quotas["max_cpu"],
        "MAX_MEMORY": quotas["max_memory"],
    })

    # Apply each YAML document in the template
    import yaml
    docs = list(yaml.safe_load_all(rendered))
    for doc in docs:
        if doc is None:
            continue
        await _apply_k8s_object(doc, namespace)

    logger.info(f"Provisioned vault {vault_slug} in namespace {namespace}")


async def _apply_k8s_object(doc: dict, namespace: str) -> None:
    """Apply a single K8s manifest dict (create or replace)."""
    from kubernetes import client as k8s_client
    from kubernetes.utils import create_from_dict
    from kubernetes import config as k8s_config

    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()

    api_client = k8s_client.ApiClient()
    try:
        create_from_dict(api_client, doc, namespace=namespace)
    except Exception as e:
        # If it already exists (409), that's fine
        err_str = str(e)
        if "AlreadyExists" in err_str or "already exists" in err_str.lower():
            logger.debug(f"Resource already exists, skipping: {doc.get('kind')} {doc.get('metadata', {}).get('name')}")
        else:
            logger.error(f"Failed to apply {doc.get('kind')} {doc.get('metadata', {}).get('name')}: {e}")
    finally:
        api_client.close()


async def deprovision_vault(user_id: str, vault_slug: str) -> None:
    """Delete vault-specific resources (PVCs, Deployments, Secrets, Services)."""
    try:
        from kubernetes import client as k8s_client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
    except Exception as e:
        logger.warning(f"K8s not available for deprovisioning: {e}")
        return

    from cloud.config import settings

    namespace = f"{settings.k8s_namespace_prefix}{user_id[:8]}"
    core = k8s_client.CoreV1Api()
    apps = k8s_client.AppsV1Api()

    label = f"vault={vault_slug}"
    delete_opts = k8s_client.V1DeleteOptions(propagation_policy="Foreground")

    # Delete deployments, services, PVCs, secrets with vault label
    for delete_fn in [
        lambda: apps.delete_collection_namespaced_deployment(namespace, label_selector=label),
        lambda: core.delete_collection_namespaced_service(namespace, label_selector=label),
        lambda: core.delete_collection_namespaced_persistent_volume_claim(namespace, label_selector=label),
    ]:
        try:
            delete_fn()
        except Exception as e:
            logger.warning(f"Deprovision cleanup error: {e}")

    # Delete secret (not label-selectable for collection delete)
    try:
        core.delete_namespaced_secret(f"vault-credentials-{vault_slug}", namespace)
    except Exception:
        pass

    logger.info(f"Deprovisioned vault {vault_slug} from namespace {namespace}")
