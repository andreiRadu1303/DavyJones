"""GitHub Activity Monitor — poll for repo events and update vault docs.

Runs in a daemon thread, polling the GitHub Events API for new activity
on a configured repository. When new events are detected, triggers the
overseer to create tasks that update vault documentation.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
import urllib.error

from src.config import (
    GITHUB_POLL_INTERVAL,
    GITHUB_REPO,
    OVERSEER_MAX_TURNS,
    OVERSEER_TIMEOUT_SECONDS,
    STATE_DIR,
)
from src.container_runner import run_raw
from src.overseer import execute_plan, extract_plan
from src.plan_models import OverseerPlan, validate_plan

logger = logging.getLogger(__name__)

_STATE_FILE = os.path.join(STATE_DIR, ".last_github_event")

# Event types we care about (skip noise like WatchEvent, ForkEvent)
_RELEVANT_EVENTS = {
    "PushEvent",
    "PullRequestEvent",
    "IssuesEvent",
    "IssueCommentEvent",
    "PullRequestReviewEvent",
    "PullRequestReviewCommentEvent",
    "CreateEvent",
    "DeleteEvent",
    "ReleaseEvent",
}


def _summarize_event(event: dict) -> str | None:
    """Convert a GitHub event into a human-readable summary line."""
    etype = event.get("type", "")
    payload = event.get("payload", {})
    actor = event.get("actor", {}).get("login", "unknown")

    if etype == "PushEvent":
        branch = (payload.get("ref") or "").replace("refs/heads/", "")
        commits = payload.get("commits", [])
        msgs = [c.get("message", "").split("\n")[0] for c in commits[:5]]
        summary = "; ".join(msgs)
        return f"Push: {len(commits)} commit(s) to {branch} by {actor} — {summary}"

    if etype == "PullRequestEvent":
        pr = payload.get("pull_request", {})
        action = payload.get("action", "")
        num = pr.get("number", "?")
        title = pr.get("title", "")
        return f"PR #{num} {action} by {actor}: {title}"

    if etype == "IssuesEvent":
        issue = payload.get("issue", {})
        action = payload.get("action", "")
        num = issue.get("number", "?")
        title = issue.get("title", "")
        return f"Issue #{num} {action} by {actor}: {title}"

    if etype == "IssueCommentEvent":
        issue = payload.get("issue", {})
        comment = payload.get("comment", {})
        num = issue.get("number", "?")
        body = (comment.get("body") or "")[:100]
        return f"Comment on #{num} by {actor}: {body}"

    if etype == "PullRequestReviewEvent":
        pr = payload.get("pull_request", {})
        review = payload.get("review", {})
        num = pr.get("number", "?")
        state = review.get("state", "")
        return f"Review on PR #{num} by {actor}: {state}"

    if etype == "PullRequestReviewCommentEvent":
        pr = payload.get("pull_request", {})
        comment = payload.get("comment", {})
        num = pr.get("number", "?")
        body = (comment.get("body") or "")[:100]
        return f"Review comment on PR #{num} by {actor}: {body}"

    if etype == "CreateEvent":
        ref_type = payload.get("ref_type", "")
        ref = payload.get("ref", "")
        return f"Created {ref_type}: {ref} by {actor}"

    if etype == "DeleteEvent":
        ref_type = payload.get("ref_type", "")
        ref = payload.get("ref", "")
        return f"Deleted {ref_type}: {ref} by {actor}"

    if etype == "ReleaseEvent":
        release = payload.get("release", {})
        tag = release.get("tag_name", "")
        name = release.get("name", "")
        action = payload.get("action", "")
        return f"Release {action}: {tag} — {name} by {actor}"

    return None


def _build_prompt(events: list[dict], repo: str) -> str:
    """Build an overseer prompt from a batch of GitHub events."""
    summaries = []
    for event in events:
        line = _summarize_event(event)
        if line:
            summaries.append(f"- {line}")

    activity_text = "\n".join(summaries) if summaries else "- (no relevant activity)"

    parts = [
        "You are the DavyJones overseer agent. New activity has been detected on the",
        f"GitHub repository `{repo}`. Your job is to analyze this activity and",
        "create tasks for agents to update the Obsidian vault documentation accordingly.",
        "",
        "You MUST respond with ONLY a JSON object (no explanation, no markdown",
        'fences, no extra text). The JSON must have this exact structure:',
        "",
        '{',
        '  "tasks": [',
        '    {',
        '      "id": "t1",',
        '      "description": "Brief description for logging",',
        '      "file_path": "path/to/relevant-file.md",',
        '      "prompt": "Focused instructions for ONE unit of work",',
        '      "depends_on": [],',
        '      "max_turns": 30',
        '    }',
        '  ]',
        '}',
        "",
        "## Guidelines",
        "",
        "- Each task should update or create documentation in the vault",
        "- Agents have access to the GitHub MCP — they can fetch full details",
        f"  (code, diffs, comments, etc.) from the `{repo}` repository",
        "- If a task involves reading GitHub data, include the repo name in the prompt",
        "- Use `file_path` to point to existing vault notes that should be updated,",
        "  or a logical path for new notes (e.g., `Projects/repo-name/changelog.md`)",
        "- Set max_turns generously (25-50) — agents need room to fetch data and write",
        "- If the activity is minor (bot commits, trivial tag updates), return empty tasks",
        "- Sub-tasks that are independent MUST have empty `depends_on` for concurrency",
        "",
        "## GitHub Activity Detected",
        "",
        f"Repository: `{repo}`",
        f"Events ({len(events)}):",
        "",
        activity_text,
        "",
        "## Your Response",
        "",
        "Analyze the above activity and respond with ONLY the JSON plan object.",
        'If no vault documentation updates are needed, return {"tasks": []}.',
    ]

    return "\n".join(parts)


class GitHubMonitor:
    """Polls the GitHub Events API and triggers overseer for vault updates."""

    def __init__(self, token: str, repo: str, auto_commit_fn) -> None:
        self.token = token
        self.repo = repo  # "owner/repo"
        self.auto_commit_fn = auto_commit_fn
        self._last_event_id: str | None = self._load_last_event_id()

    # ── State persistence ──────────────────────────────────────

    def _load_last_event_id(self) -> str | None:
        try:
            with open(_STATE_FILE) as f:
                eid = f.read().strip()
                return eid if eid else None
        except FileNotFoundError:
            return None

    def _save_last_event_id(self, event_id: str) -> None:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(_STATE_FILE, "w") as f:
            f.write(event_id)

    # ── GitHub API ─────────────────────────────────────────────

    def _fetch_events(self) -> list[dict]:
        """Fetch recent events from the GitHub Events API."""
        url = f"https://api.github.com/repos/{self.repo}/events?per_page=30"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "DavyJones-Dispatcher/1.0",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                logger.error("GitHub repo not found: %s", self.repo)
            elif e.code == 401:
                logger.error("GitHub token unauthorized for %s", self.repo)
            elif e.code == 403:
                logger.warning("GitHub API rate limit hit, will retry next cycle")
            else:
                logger.warning("GitHub API error (HTTP %d) for %s", e.code, self.repo)
            return []
        except Exception:
            logger.warning("GitHub API request failed, will retry next cycle")
            return []

    def _fetch_new_events(self) -> list[dict]:
        """Fetch events newer than the last-seen event ID."""
        all_events = self._fetch_events()
        if not all_events:
            return []

        # Filter to relevant event types
        relevant = [e for e in all_events if e.get("type") in _RELEVANT_EVENTS]

        if self._last_event_id is None:
            # First run — record current state, don't process history
            if all_events:
                newest_id = str(all_events[0].get("id", ""))
                if newest_id:
                    self._save_last_event_id(newest_id)
                    self._last_event_id = newest_id
                    logger.info("GitHub monitor cold start: recorded latest event %s", newest_id)
            return []

        # Find events newer than last-seen
        new_events = []
        for event in relevant:
            eid = str(event.get("id", ""))
            if eid == self._last_event_id:
                break
            new_events.append(event)

        return new_events

    # ── Event handling ─────────────────────────────────────────

    def _handle_events(self, events: list[dict]) -> None:
        """Process a batch of GitHub events through the overseer pipeline."""
        logger.info("GitHub monitor: %d new event(s) on %s", len(events), self.repo)
        for e in events[:10]:
            summary = _summarize_event(e)
            if summary:
                logger.info("  %s", summary)

        prompt = _build_prompt(events, self.repo)
        logger.debug("GitHub overseer prompt (first 500 chars): %s", prompt[:500])

        try:
            exit_code, stdout, stderr = run_raw(
                prompt=prompt,
                max_turns=OVERSEER_MAX_TURNS,
                timeout=OVERSEER_TIMEOUT_SECONDS,
            )
        except RuntimeError as e:
            logger.error("GitHub overseer credential error: %s", e)
            return

        if exit_code != 0:
            logger.error("GitHub overseer failed (exit=%d): %s",
                         exit_code, (stderr or stdout)[:500])
            return

        # Parse the plan
        plan_data = extract_plan(stdout)
        if plan_data is None:
            logger.warning("GitHub overseer returned unparseable output: %s", stdout[:500])
            return

        try:
            plan = OverseerPlan(**plan_data)
        except (TypeError, ValueError) as e:
            logger.error("Failed to parse GitHub overseer plan: %s", e)
            return

        errors = validate_plan(plan)
        if errors:
            logger.error("Invalid GitHub overseer plan: %s", errors)
            return

        if not plan.tasks:
            logger.info("GitHub overseer: no vault updates needed")
            return

        logger.info("GitHub overseer plan: %d tasks", len(plan.tasks))
        results = execute_plan(plan, self.auto_commit_fn)

        succeeded = sum(1 for r in results.values() if r.status == "completed")
        failed = sum(1 for r in results.values() if r.status == "failed")
        logger.info("GitHub plan completed: %d succeeded, %d failed", succeeded, failed)

        # Update last-seen event ID (events are newest-first)
        newest_id = str(events[0].get("id", ""))
        if newest_id:
            self._save_last_event_id(newest_id)
            self._last_event_id = newest_id

    # ── Main loop ──────────────────────────────────────────────

    def _poll_loop(self) -> None:
        """Background polling loop."""
        logger.info("GitHub monitor polling %s every %ds", self.repo, GITHUB_POLL_INTERVAL)
        while True:
            try:
                events = self._fetch_new_events()
                if events:
                    self._handle_events(events)
            except Exception:
                logger.exception("GitHub monitor error (will retry)")
            time.sleep(GITHUB_POLL_INTERVAL)

    def start(self) -> None:
        """Start the monitor in a daemon thread."""
        thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="github-monitor",
        )
        thread.start()
        logger.info("GitHub activity monitor started for %s", self.repo)
