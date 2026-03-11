"""Read vault rules from .davyjones-rules.json."""
import json
import logging
import os

from src.config import VAULT_PATH

logger = logging.getLogger(__name__)

VAULT_RULES_FILE = os.path.join(VAULT_PATH, ".davyjones-rules.json")

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
        return merged
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULT_RULES)
