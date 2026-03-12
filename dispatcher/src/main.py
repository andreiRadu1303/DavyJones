import json
import logging
import os
import subprocess
import threading
import time

from src.config import POLL_INTERVAL_SECONDS, VAULT_PATH
from src.container_runner import run_task
from src.context_resolver import resolve
from src.frontmatter_parser import check_dependencies_met, is_actionable, parse_note
from src.token_refresh import ensure_valid_token, get_cred_health
from src.git_watcher import (
    get_changed_md_files,
    get_current_head,
    get_new_commit_ranges,
    get_repo,
    has_human_commits,
    load_last_sha,
    pull_remote,
    save_last_sha,
)
from src.overseer import execute_plan, gather_commit_data, run_overseer
from src.scribe import ScribeJob, enqueue as scribe_enqueue
from src.status_updater import _acquire_lock, _release_lock, update_status
from src.task_builder import build

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Track in-flight tasks so we don't double-dispatch
_active_tasks: set[str] = set()
_active_lock = threading.Lock()


def _auto_commit(file_path: str, status: str) -> None:
    """Commit agent changes + status update to the vault repo."""
    _acquire_lock()
    try:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "DavyJones Agent",
            "GIT_AUTHOR_EMAIL": "davyjones@local",
            "GIT_COMMITTER_NAME": "DavyJones Agent",
            "GIT_COMMITTER_EMAIL": "davyjones@local",
        }
        subprocess.run(
            ["git", "add", "-A"],
            cwd=VAULT_PATH, env=env, capture_output=True, timeout=10,
        )
        msg = f"DavyJones: {status} — {file_path}"
        result = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=VAULT_PATH, env=env, capture_output=True, timeout=10,
        )
        if result.returncode == 0:
            logger.info("Auto-committed: %s", msg)
        else:
            stderr = result.stderr.decode().strip()
            if "nothing to commit" in stderr:
                logger.info("Nothing to commit for %s", file_path)
            else:
                logger.warning("Commit failed: %s", stderr)
    except Exception:
        logger.exception("Auto-commit error for %s", file_path)
    finally:
        _release_lock()


def _run_in_thread(file_path: str, payload) -> None:
    """Run a task in a background thread (used by fallback dispatch)."""
    try:
        result = run_task(payload)
        update_status(file_path, result.status, result)
        logger.info("Task %s finished: %s", file_path, result.status)
        _auto_commit(file_path, result.status)
    except Exception:
        logger.exception("Error running task %s", file_path)
    finally:
        with _active_lock:
            _active_tasks.discard(file_path)


def _fallback_dispatch(changed_files: list[str]) -> None:
    """Fallback: process individual pending task files (pre-overseer behavior).

    Used when the overseer container fails entirely.
    """
    for file_path in changed_files:
        full_path = os.path.join(VAULT_PATH, file_path)
        if not os.path.isfile(full_path):
            continue
        try:
            _process_task(file_path)
        except Exception:
            logger.exception("Fallback error processing %s", file_path)


def _process_task(file_path: str) -> None:
    """Process a single pending task file (non-blocking)."""
    # Atomically check and reserve the task slot
    with _active_lock:
        if file_path in _active_tasks:
            logger.info("Skipping %s: already running", file_path)
            return
        _active_tasks.add(file_path)

    try:
        full_path = os.path.join(VAULT_PATH, file_path)

        metadata, content = parse_note(full_path)
        if not is_actionable(metadata):
            with _active_lock:
                _active_tasks.discard(file_path)
            return

        if not check_dependencies_met(metadata, VAULT_PATH):
            logger.info("Skipping %s: dependencies not met", file_path)
            with _active_lock:
                _active_tasks.discard(file_path)
            return

        logger.info("Fallback processing task: %s (type=%s, priority=%s)",
                    file_path, metadata.type, metadata.priority)

        context = resolve(VAULT_PATH, full_path)
        logger.info("Context length: %d chars", len(context))

        payload = build(file_path, metadata, content, context)

        # Mark in-progress and commit
        update_status(file_path, "in_progress")
        _auto_commit(file_path, "in_progress")
    except Exception:
        with _active_lock:
            _active_tasks.discard(file_path)
        raise

    # Fire and forget — runs in its own thread + container
    t = threading.Thread(
        target=_run_in_thread,
        args=(file_path, payload),
        daemon=True,
    )
    t.start()
    logger.info("Task %s dispatched to thread (fallback)", file_path)


def _handle_commit_range(repo, from_sha: str, to_sha: str) -> None:
    """Handle a single commit range through the overseer pipeline."""
    logger.info("New commits detected: %s..%s", from_sha[:8], to_sha[:8])

    # Skip DavyJones auto-commits
    if not has_human_commits(repo, from_sha, to_sha):
        logger.info("Skipping: all commits are DavyJones auto-commits")
        return

    # Gather commit data for the overseer
    commit_data = gather_commit_data(repo, from_sha, to_sha)
    if not commit_data.changed_files:
        logger.info("No changed .md files in commit range")
        return

    logger.info("Changed files: %s", commit_data.changed_files)

    # Run the overseer
    plan = run_overseer(commit_data)

    if plan is None:
        # Overseer failed — fall back to per-file dispatch
        logger.warning("Overseer failed — falling back to per-file dispatch")
        _fallback_dispatch(commit_data.changed_files)
        return

    if not plan.tasks:
        logger.info("Overseer decided: no tasks needed")
        return

    # Execute the plan
    logger.info("Executing overseer plan: %d tasks", len(plan.tasks))
    t_start = time.time()
    results = execute_plan(plan, _auto_commit)
    duration = time.time() - t_start

    succeeded = sum(1 for r in results.values() if r.status == "completed")
    failed = sum(1 for r in results.values() if r.status == "failed")
    logger.info("Plan completed: %d tasks, %d succeeded, %d failed",
                len(plan.tasks), succeeded, failed)

    # Enqueue Scribe report (fire-and-forget)
    try:
        scribe_enqueue(ScribeJob(
            plan=plan,
            results=results,
            source="commit",
            source_detail={"from_sha": from_sha, "to_sha": to_sha},
            description=f"Commit {from_sha[:8]}..{to_sha[:8]}: {len(plan.tasks)} tasks",
            duration_seconds=duration,
        ))
    except Exception:
        logger.exception("Failed to enqueue Scribe job")


def main() -> None:
    logger.info("DavyJones Dispatcher starting. Vault: %s, Poll interval: %ds",
                VAULT_PATH, POLL_INTERVAL_SECONDS)

    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        logger.info("Auth: long-lived OAuth token (no refresh needed)")
    else:
        logger.info("Auth: OAuth credentials file (auto-refresh enabled)")

    repo = get_repo()
    last_sha = load_last_sha()

    if last_sha is None:
        logger.info("First run detected. Recording current HEAD...")
        current = get_current_head(repo)
        if current:
            save_last_sha(current)
            last_sha = current
        else:
            logger.warning("Vault repo has no commits yet. Waiting...")

    # Start Slack listener if app token is configured
    slack_app_token = os.environ.get("SLACK_APP_TOKEN", "")
    slack_bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if slack_app_token and slack_bot_token:
        try:
            from src.slack_listener import SlackListener
            listener = SlackListener(
                bot_token=slack_bot_token,
                app_token=slack_app_token,
            )
            listener.start()
        except Exception:
            logger.exception("Failed to start Slack listener")
    elif slack_app_token and not slack_bot_token:
        logger.warning("SLACK_APP_TOKEN set but SLACK_BOT_TOKEN missing — Slack listener disabled")

    # Start GitHub activity monitor if repo is configured
    github_repo = os.environ.get("GITHUB_REPO", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if github_repo and github_token:
        try:
            from src.github_monitor import GitHubMonitor
            monitor = GitHubMonitor(
                token=github_token,
                repo=github_repo,
                auto_commit_fn=_auto_commit,
            )
            monitor.start()
        except Exception:
            logger.exception("Failed to start GitHub activity monitor")
    elif github_repo and not github_token:
        logger.warning("GITHUB_REPO set but GITHUB_TOKEN missing — GitHub monitor disabled")

    # Start HTTP API for direct task submission from Obsidian plugin
    try:
        from src.http_api import HttpApi
        HttpApi(auto_commit_fn=_auto_commit).start()
    except Exception:
        logger.exception("Failed to start HTTP API")

    # Start Scribe background worker for report generation
    try:
        from src.scribe import start as scribe_start
        scribe_start()
    except Exception:
        logger.exception("Failed to start Scribe worker")

    # Sync dynamic MCP containers for additional service instances
    try:
        from src.mcp_manager import sync as mcp_sync
        from src.vault_rules import load_vault_rules
        vault_rules = load_vault_rules()
        mcp_sync(vault_rules.get("serviceInstances", []))
    except Exception:
        logger.exception("Failed to sync MCP instances")

    logger.info("Dispatcher ready. Polling for changes (overseer mode)...")

    heartbeat_path = os.path.join(VAULT_PATH, ".davyjones")

    # Background heartbeat thread — keeps the heartbeat fresh even during
    # long-running agent execution (overseer + tasks can take minutes).
    def _heartbeat_loop():
        while True:
            try:
                cred_health = get_cred_health()
                hb = {
                    "active": True,
                    "ts": time.time(),
                    "creds": cred_health.to_dict(),
                }
                with open(heartbeat_path, "w") as f:
                    json.dump(hb, f)
            except Exception:
                pass
            time.sleep(POLL_INTERVAL_SECONDS)

    hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    hb_thread.start()

    while True:
        try:
            # Check credential health every cycle
            creds_local = "/tmp/claude-credentials.json"
            ensure_valid_token(creds_local)

            # Pull remote changes (if a remote is configured)
            pull_remote(repo)

            last_sha = load_last_sha()
            ranges = get_new_commit_ranges(repo, last_sha)

            for from_sha, to_sha in ranges:
                try:
                    _handle_commit_range(repo, from_sha, to_sha)
                except Exception:
                    logger.exception("Error handling commit range %s..%s",
                                    from_sha[:8], to_sha[:8])
                finally:
                    save_last_sha(to_sha)

        except Exception:
            logger.exception("Error in poll loop")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
