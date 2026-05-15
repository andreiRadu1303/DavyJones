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


def _git_wt(
    args: list[str],
    *,
    gitdir: str,
    worktree_path: str,
    input: bytes | None = None,
) -> subprocess.CompletedProcess:
    """Run a git command with an explicit --git-dir and --work-tree.

    The agent container mounts the worktree at /vault. After it exits,
    git's auto-discovery of the gitdir (via the .git file) becomes
    unreliable — the path stored in the .git file uses the dispatcher's
    /vault prefix which may no longer match after the agent modifies the
    container's git environment. Passing --git-dir and --work-tree
    explicitly bypasses auto-discovery entirely.
    """
    return subprocess.run(
        ["git", "--git-dir", gitdir, "--work-tree", worktree_path] + args,
        cwd=worktree_path,
        input=input,
        capture_output=True,
    )


def _read_gitdir(worktree_path: str) -> str | None:
    """Read the gitdir path from the worktree's .git file."""
    dot_git = os.path.join(worktree_path, ".git")
    if not os.path.isfile(dot_git):
        return None
    with open(dot_git, errors="replace") as f:
        line = f.read().strip()
    if line.startswith("gitdir: "):
        return line[len("gitdir: "):]
    return None


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


def _copy_worktree_files(worktree_path: str, vault_path: str, gitdir: str) -> list[str]:
    """Copy new/modified files from worktree to vault directly.

    Used as a fallback when git apply fails to write anything. Skips
    deletions and binary metadata files — we only rescue content the
    agent created or modified.
    """
    result = _git_wt(
        ["diff", "--cached", "-z", "--name-status", "HEAD"],
        gitdir=gitdir,
        worktree_path=worktree_path,
    )
    if result.returncode != 0:
        logger.error(
            "Task fallback: git diff --name-status failed (rc=%d stderr=%r)",
            result.returncode, result.stderr.decode(errors="replace")[:200],
        )
        return []

    # Output with -z: NUL-delimited <status NUL path NUL> pairs
    entries = result.stdout.decode(errors="replace").split("\0")
    to_copy = []
    i = 0
    while i < len(entries):
        status = entries[i].strip()
        if not status:
            i += 1
            continue
        if status.startswith("R") or status.startswith("C"):
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
    # Read gitdir explicitly from the .git file and use it for all
    # worktree git commands. Auto-discovery fails after the agent
    # container runs because the path stored in .git uses the
    # dispatcher's /vault prefix, which git cannot resolve when the
    # worktree index or config is in an unexpected state.
    dot_git = os.path.join(worktree_path, ".git")
    if os.path.isfile(dot_git):
        gitdir = _read_gitdir(worktree_path)
        logger.info("Task %s: worktree gitdir=%r", task_id, gitdir)
    elif os.path.isdir(dot_git):
        gitdir = dot_git
        logger.error("Task %s: worktree .git is a DIRECTORY — agent replaced it", task_id)
    else:
        gitdir = None
        logger.error("Task %s: worktree .git is MISSING", task_id)

    if not gitdir:
        logger.error("Task %s: cannot determine gitdir — skipping merge", task_id)
        _remove(vault_path, worktree_path)
        return False

    # Verify the gitdir path actually exists (guards against stale .git files)
    if not os.path.isdir(gitdir):
        logger.error(
            "Task %s: gitdir %r does not exist — worktree metadata was cleaned up prematurely",
            task_id, gitdir,
        )
        _remove(vault_path, worktree_path)
        return False

    # Stage everything the agent wrote, then immediately unstage binary
    # filesystem metadata files whose delta patches break git apply --3way.
    _git_wt(["add", "-A"], gitdir=gitdir, worktree_path=worktree_path)
    _git_wt(
        ["rm", "--cached", "--ignore-unmatch"] + list(_BINARY_METADATA),
        gitdir=gitdir, worktree_path=worktree_path,
    )

    # Nothing to merge?
    if _git_wt(["diff", "--cached", "--quiet"], gitdir=gitdir, worktree_path=worktree_path).returncode == 0:
        logger.info("Task %s: agent made no file changes", task_id)
        _remove(vault_path, worktree_path)
        return True

    # Get the full diff of agent changes relative to the shared HEAD.
    diff_result = _git_wt(
        ["diff", "--cached", "--binary", "HEAD"],
        gitdir=gitdir, worktree_path=worktree_path,
    )
    diff = diff_result.stdout
    logger.info(
        "Task %s: diff rc=%d bytes=%d stderr=%r",
        task_id, diff_result.returncode, len(diff),
        diff_result.stderr.decode(errors="replace")[:200],
    )

    # Apply to the main vault with 3-way merge fallback.
    apply = _git(
        ["git", "apply", "--3way", "--index", "--binary", "-"],
        cwd=vault_path,
        input=diff,
    )
    logger.info(
        "Task %s: git apply rc=%d stderr=%r",
        task_id, apply.returncode,
        apply.stderr.decode(errors="replace")[:400],
    )

    had_conflicts = apply.returncode != 0
    if had_conflicts:
        apply_stderr = apply.stderr.decode().strip()
        logger.warning(
            "Task %s: merge conflicts — conflict markers written to affected files: %s",
            task_id, apply_stderr,
        )
        _git(["git", "add", "-u"], cwd=vault_path)

        nothing_staged = _git(["git", "diff", "--cached", "--quiet"], cwd=vault_path).returncode == 0
        if nothing_staged:
            copied = _copy_worktree_files(worktree_path, vault_path, gitdir)
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
