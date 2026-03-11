import logging
import os
from typing import Optional

import git

from src.config import LAST_SHA_FILE, STATE_DIR, VAULT_PATH

logger = logging.getLogger(__name__)


def get_repo() -> git.Repo:
    """Open the git repo at VAULT_PATH."""
    return git.Repo(VAULT_PATH)


def load_last_sha() -> Optional[str]:
    """Load the last processed commit SHA from state file."""
    if os.path.isfile(LAST_SHA_FILE):
        with open(LAST_SHA_FILE, "r") as f:
            sha = f.read().strip()
            return sha if sha else None
    return None


def save_last_sha(sha: str) -> None:
    """Persist the last processed commit SHA."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LAST_SHA_FILE, "w") as f:
        f.write(sha)


def get_current_head(repo: git.Repo) -> Optional[str]:
    """Get the current HEAD SHA, or None if repo has no commits."""
    try:
        return str(repo.head.commit.hexsha)
    except ValueError:
        return None


def get_changed_md_files(repo: git.Repo, since_sha: str, until_sha: str) -> list[str]:
    """Get list of changed .md file paths between two commits.

    Returns paths relative to the vault root.
    Excludes .obsidian/ files and _context.md files (context changes don't trigger tasks).
    """
    try:
        old_commit = repo.commit(since_sha)
        new_commit = repo.commit(until_sha)
    except Exception:
        logger.exception("Failed to get commits %s..%s", since_sha, until_sha)
        return []

    diff = old_commit.diff(new_commit)
    changed_files = set()

    for change_type in ("A", "M", "R", "D"):
        for d in diff.iter_change_type(change_type):
            path = d.b_path if d.b_path else d.a_path
            if (
                path.endswith(".md")
                and not path.startswith(".obsidian/")
                and os.path.basename(path) != "_context.md"
            ):
                changed_files.add(path)

    return list(changed_files)


def has_human_commits(repo: git.Repo, from_sha: str, to_sha: str) -> bool:
    """Check if the commit range contains at least one non-DavyJones commit.

    Returns False if ALL commits in the range were authored by davyjones@local.
    """
    try:
        for commit in repo.iter_commits(f"{from_sha}..{to_sha}"):
            if commit.author.email != "davyjones@local":
                return True
    except Exception:
        logger.exception("Error checking commit authors %s..%s", from_sha, to_sha)
        return True  # assume human on error to avoid swallowing commits
    return False


def get_commit_diff_text(repo: git.Repo, from_sha: str, to_sha: str) -> str:
    """Get a unified diff string between two commits (for .md files only).

    Returns a human-readable diff that the overseer can analyze.
    """
    try:
        old_commit = repo.commit(from_sha)
        new_commit = repo.commit(to_sha)
        diff_text = repo.git.diff(old_commit.hexsha, new_commit.hexsha,
                                  "--", "*.md",
                                  unified=3, no_color=True)
        return diff_text
    except Exception:
        logger.exception("Failed to get diff %s..%s", from_sha, to_sha)
        return ""


def pull_remote(repo: git.Repo) -> bool:
    """Pull from the tracking remote (if configured).

    Returns True if new commits were fetched, False otherwise.
    Silently does nothing if there is no remote or no tracking branch.
    """
    try:
        if not repo.remotes:
            return False
        remote = repo.remotes[0]  # typically "origin"
        branch = repo.active_branch
        tracking = branch.tracking_branch()
        if tracking is None:
            return False

        old_sha = str(repo.head.commit.hexsha)
        remote.pull(rebase=True)
        new_sha = str(repo.head.commit.hexsha)

        if old_sha != new_sha:
            logger.info("Pulled new commits from %s/%s (%s → %s)",
                        remote.name, branch.name, old_sha[:8], new_sha[:8])
            return True
        return False
    except git.GitCommandError as e:
        # Merge conflicts, dirty worktree, etc. — log and carry on
        logger.warning("git pull failed (will retry next cycle): %s", e)
        return False
    except Exception:
        logger.debug("No remote tracking branch or pull skipped")
        return False


def get_new_commit_ranges(repo: git.Repo, last_sha: Optional[str]) -> list[tuple[str, str]]:
    """Get commit ranges to process since last_sha.

    Returns list of (from_sha, to_sha) pairs.
    On first run (last_sha=None), returns empty (cold start).
    """
    current = get_current_head(repo)
    if current is None:
        return []

    if last_sha is None:
        # Cold start: record current HEAD, don't process history
        logger.info("Cold start: recording HEAD=%s, skipping existing history", current[:8])
        save_last_sha(current)
        return []

    if last_sha == current:
        return []

    # Return a single range from last processed to current HEAD
    return [(last_sha, current)]
