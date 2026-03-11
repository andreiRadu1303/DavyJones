import os


VAULT_PATH = os.environ.get("VAULT_PATH", "/vault")
DEBOUNCE_SECONDS = int(os.environ.get("AUTO_COMMIT_DEBOUNCE_SECONDS", "5"))
GIT_AUTHOR_NAME = os.environ.get("GIT_AUTHOR_NAME", "DavyJones Auto-Committer")
GIT_AUTHOR_EMAIL = os.environ.get("GIT_AUTHOR_EMAIL", "davyjones@local")
