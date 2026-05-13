"""Isolated git worktrees for agent task execution (Docker mode only).

Each task gets its own worktree so agent writes never touch the user's
working tree. After the agent exits, changes are merged back into the
main vault using git apply --3way, which uses the pre-image blob as the
merge base. This means any user edits (committed or uncommitted) that
arrived in the main vault during the task are preserved through git's
content-level merge machinery.

Conflict markers are left in files only when both sides edited the same
lines — everything else merges cleanly with all changes preserved.

Not used in K8s mode: the vault PVC is already isolated from user writes
(the user only interacts via committed pushes to the Forgejo remote).
"""

import logging
import os
import shutil
import subprocess

from src.claude_changes import record_changed_files

logger = logging.getLogger(__name__)


def _git(args: list[str], *, cwd: str, input: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, input=input, capture_output=True)


def create(vault_path: str, worktrees_root: str, task_id: str) -> str:
    """Create a detached worktree for a task. Returns the worktree path."""
    os.makedirs(worktrees_root, exist_ok=True)
    worktree_path = os.path.join(worktrees_root, task_id)
    result = _git(
        ["git", "worktree", "add", "--detach", worktree_path],
        cwd=vault_path,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git worktree add failed for task {task_id}: "
            f"{result.stderr.decode().strip()}"
        )
    logger.info("Worktree created for task %s at %s", task_id, worktree_path)
    return worktree_path


def _copy_worktree_files(worktree_path: str, vault_path: str) -> list[str]:
    """Copy new/modified files from worktree to vault directly.

    Used as a fallback when git apply fails to write anything (e.g. the
    "No valid patches in input" error caused by binary files like .DS_Store
    making the whole patch batch unappliable). Skips deletions — we only
    rescue content the agent created or modified.
    """
    result = _git(
        ["git", "diff", "--cached", "--name-status", "HEAD"],
        cwd=worktree_path,
    )
    if result.returncode != 0:
        return []

    to_copy = []
    for line in result.stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        status, path = parts[0], parts[1]
        if status.startswith("D"):
            continue  # skip deletions on fallback
        src = os.path.join(worktree_path, path)
        if os.path.isfile(src):
            to_copy.append((path, src))

    if not to_copy:
        return []

    copied = []
    for path, src in to_copy:
        dst = os.path.join(vault_path, path)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(path)

    if copied:
        _git(["git", "add"] + copied, cwd=vault_path)

    return copied


def merge_back(vault_path: str, worktree_path: str, task_id: str) -> bool:
    """Merge agent changes from the worktree into the main vault.

    Strategy: diff what the agent changed relative to HEAD (the shared
    starting point), then apply that diff to the main vault with --3way.
    The --3way flag turns any apply failure into a 3-way content merge
    using the pre-image blob as base, so concurrent user edits survive
    even if git apply's context no longer matches.

    Returns True on a clean merge, False if conflict markers were written.
    """
    # Stage everything the agent wrote
    _git(["git", "add", "-A"], cwd=worktree_path)

    # Nothing to merge?
    if _git(["git", "diff", "--cached", "--quiet"], cwd=worktree_path).returncode == 0:
        logger.info("Task %s: agent made no file changes", task_id)
        _remove(vault_path, worktree_path)
        return True

    # Get the full diff of agent changes relative to the shared HEAD.
    # The diff includes pre-image blob SHAs which --3way needs for the
    # merge base lookup — those objects exist in the main vault's object
    # store because the worktree shares it.
    diff = _git(
        ["git", "diff", "--cached", "--binary", "HEAD"],
        cwd=worktree_path,
    ).stdout

    # Apply to the main vault with 3-way merge fallback.
    apply = _git(
        ["git", "apply", "--3way", "--index", "--binary", "-"],
        cwd=vault_path,
        input=diff,
    )

    had_conflicts = apply.returncode != 0
    if had_conflicts:
        apply_stderr = apply.stderr.decode().strip()
        logger.warning(
            "Task %s: merge conflicts — conflict markers written to affected files: %s",
            task_id,
            apply_stderr,
        )
        # Stage any conflict markers git apply wrote.
        _git(["git", "add", "-u"], cwd=vault_path)

        # If git apply wrote nothing at all (e.g. "No valid patches in input"
        # caused by a binary file like .DS_Store making the batch unparseable),
        # fall back to copying files directly from the worktree so they aren't
        # lost when the worktree is removed.
        nothing_staged = _git(["git", "diff", "--cached", "--quiet"], cwd=vault_path).returncode == 0
        if nothing_staged:
            copied = _copy_worktree_files(worktree_path, vault_path)
            if copied:
                logger.warning(
                    "Task %s: git apply wrote nothing — rescued %d file(s) via direct copy: %s",
                    task_id, len(copied), copied,
                )
            else:
                logger.error(
                    "Task %s: git apply wrote nothing and fallback copy found no files — "
                    "agent output may be lost",
                    task_id,
                )

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "DavyJones Agent",
        "GIT_AUTHOR_EMAIL": "davyjones@local",
        "GIT_COMMITTER_NAME": "DavyJones Agent",
        "GIT_COMMITTER_EMAIL": "davyjones@local",
    }
    suffix = " (merge conflicts — please resolve)" if had_conflicts else ""
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", f"DavyJones: task {task_id}{suffix}"],
        cwd=vault_path,
        env=env,
        capture_output=True,
    )

    # Record which files this commit touched (used by cloud-mode plugin pull)
    try:
        name_result = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
            cwd=vault_path,
            capture_output=True,
            encoding="utf8",
        )
        if name_result.returncode == 0:
            changed = [f.strip() for f in name_result.stdout.strip().split("\n") if f.strip()]
            record_changed_files(changed)
    except Exception:
        logger.exception("Failed to record changed files after worktree merge")

    _remove(vault_path, worktree_path)
    return not had_conflicts


def _remove(vault_path: str, worktree_path: str) -> None:
    _git(["git", "worktree", "remove", "--force", worktree_path], cwd=vault_path)
    logger.info("Worktree removed: %s", worktree_path)
