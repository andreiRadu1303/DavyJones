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


# Binary macOS/Windows filesystem metadata files that should never be tracked
# in a vault and whose binary delta patches break git apply --3way.
_BINARY_METADATA = frozenset([".DS_Store", "Thumbs.db", "desktop.ini"])


def _copy_worktree_files(worktree_path: str, vault_path: str) -> list[str]:
    """Copy new/modified files from worktree to vault directly.

    Used as a fallback when git apply fails to write anything. Skips
    deletions and binary metadata files — we only rescue content the
    agent created or modified.
    """
    result = _git(
        ["git", "diff", "--cached", "-z", "--name-status", "HEAD"],
        cwd=worktree_path,
    )
    logger.debug(
        "Task fallback: git diff --cached -z --name-status HEAD rc=%d output=%r",
        result.returncode, result.stdout[:500],
    )
    if result.returncode != 0:
        logger.error(
            "Task fallback: git diff --name-status failed (rc=%d stderr=%r)",
            result.returncode, result.stderr.decode(errors="replace")[:200],
        )
        return []

    # Output with -z: NUL-delimited pairs of <status NUL path NUL> (renames have two paths)
    entries = result.stdout.decode(errors="replace").split("\0")
    to_copy = []
    i = 0
    while i < len(entries):
        status = entries[i].strip()
        if not status:
            i += 1
            continue
        if status.startswith("R") or status.startswith("C"):
            # Rename/copy: next two entries are old path and new path
            path = entries[i + 2] if i + 2 < len(entries) else ""
            i += 3
        else:
            path = entries[i + 1] if i + 1 < len(entries) else ""
            i += 2
        if not path or status.startswith("D"):
            continue
        if os.path.basename(path) in _BINARY_METADATA:
            continue
        src = os.path.join(worktree_path, path)
        exists = os.path.isfile(src)
        logger.debug("Task fallback: status=%s path=%r src_exists=%s", status, path, exists)
        if exists:
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
    # Stage everything the agent wrote, then immediately unstage binary
    # filesystem metadata files whose delta patches break git apply --3way.
    _git(["git", "add", "-A"], cwd=worktree_path)
    _git(
        ["git", "rm", "--cached", "--ignore-unmatch"] + list(_BINARY_METADATA),
        cwd=worktree_path,
    )

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
