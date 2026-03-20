"""Read vault rules from .davyjones-rules.json and .davyjones.env."""
import json
import logging
import os

from src.config import VAULT_PATH

logger = logging.getLogger(__name__)

VAULT_RULES_FILE = os.path.join(VAULT_PATH, ".davyjones-rules.json")
VAULT_ENV_FILE = os.path.join(VAULT_PATH, ".davyjones.env")

_DEFAULT_RULES = {
    "customInstructions": "",
    "verbosity": "normal",
    "maxTurns": 20,
    "timeout": 300,
    "autoCommit": False,
    "ignorePatterns": [],
    "allowedOperations": {
        "createFiles": True,
        "deleteFiles": True,
        "modifyFiles": True,
        "runGitCommands": True,
    },
    "secrets": {},
    "serviceInstances": [],
    "triggers": [],
    "triggerDepth": 1,
    "hierarchyDepth": 2,
    "triggerMaxFiles": 30,
}


def load_vault_rules() -> dict:
    """Load vault rules, returning defaults for missing fields."""
    try:
        with open(VAULT_RULES_FILE, "r") as f:
            rules = json.load(f)
        merged = {**_DEFAULT_RULES, **rules}
        merged["allowedOperations"] = {
            **_DEFAULT_RULES["allowedOperations"],
            **rules.get("allowedOperations", {}),
        }
        merged["secrets"] = {
            **_DEFAULT_RULES["secrets"],
            **rules.get("secrets", {}),
        }
        # serviceInstances is a list — use file version or default
        merged["serviceInstances"] = rules.get(
            "serviceInstances", list(_DEFAULT_RULES["serviceInstances"])
        )
        return merged
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULT_RULES)


def load_vault_env() -> dict[str, str]:
    """Parse .davyjones.env from the vault (written by the Obsidian control panel).

    Returns a dict of KEY=VALUE pairs. Comments and blank lines are skipped.
    """
    result: dict[str, str] = {}
    try:
        with open(VAULT_ENV_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                eq = line.find("=")
                if eq > 0:
                    key = line[:eq].strip()
                    value = line[eq + 1:].strip()
                    if key:
                        result[key] = value
    except FileNotFoundError:
        pass
    return result


def get_vault_env(key: str, default: str = "") -> str:
    """Get a value from .davyjones.env, falling back to os.environ, then default.

    Priority: os.environ (if non-empty) > .davyjones.env > default.
    """
    env_val = os.environ.get(key, "")
    if env_val:
        return env_val
    vault_env = load_vault_env()
    return vault_env.get(key, default)
