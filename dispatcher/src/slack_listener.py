"""Slack Socket Mode listener — receive tasks via @mention.

Listens for app_mention events using Slack's Socket Mode (WebSocket).
When the bot is @mentioned, spawns an agent container to handle the
request, then posts the result back in the Slack thread.
"""

from __future__ import annotations

import logging
import re
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from src.config import AGENT_TIMEOUT_PER_TURN, SLACK_MAX_TURNS
from src.container_runner import run_raw
from src.slack_prompt import build as build_slack_prompt

logger = logging.getLogger(__name__)

# Max characters for a single Slack message
_SLACK_MSG_LIMIT = 3900


def _strip_mention(text: str) -> str:
    """Remove the @mention prefix from a Slack message."""
    # Slack encodes mentions as <@U12345> or <@U12345|name>
    return re.sub(r"<@[A-Z0-9]+(?:\|[^>]*)?>", "", text).strip()


def _truncate(text: str, limit: int = _SLACK_MSG_LIMIT) -> str:
    """Truncate text to fit within Slack's message limit."""
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n\n... (truncated)"


class SlackListener:
    """Slack Socket Mode listener that dispatches tasks to agent containers."""

    def __init__(self, bot_token: str, app_token: str) -> None:
        self.app = App(token=bot_token)
        self.app_token = app_token
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self.app.event("app_mention")
        def handle_mention(event, say, client):
            self._handle_mention(event, say, client)

    def _handle_mention(self, event: dict, say, client) -> None:
        """Handle an @mention event: dispatch agent and reply."""
        text = event.get("text", "")
        channel = event.get("channel", "")
        ts = event.get("ts", "")
        user = event.get("user", "")

        user_message = _strip_mention(text)
        if not user_message:
            say(text="I was mentioned but no task was provided. "
                "Try: `@DavyJones list the files in the vault`",
                thread_ts=ts)
            return

        logger.info("Slack task from user %s in %s: %s",
                     user, channel, user_message[:100])

        # Acknowledge with eyes reaction
        try:
            client.reactions_add(channel=channel, timestamp=ts, name="eyes")
        except Exception:
            logger.debug("Could not add reaction")

        # Resolve channel name for the prompt
        channel_name = channel
        try:
            info = client.conversations_info(channel=channel)
            channel_name = info["channel"]["name"]
        except Exception:
            pass

        # Build prompt and dispatch agent
        prompt = build_slack_prompt(user_message, channel_name)
        max_turns = SLACK_MAX_TURNS
        timeout = max(max_turns * AGENT_TIMEOUT_PER_TURN, 120)

        try:
            exit_code, stdout, stderr = run_raw(
                prompt=prompt,
                max_turns=max_turns,
                timeout=timeout,
            )
        except RuntimeError as e:
            logger.error("Slack agent credential error: %s", e)
            say(text=f"Agent failed: {e}", thread_ts=ts)
            self._react(client, channel, ts, "x")
            return

        if exit_code != 0:
            error_msg = (stderr or stdout or "Unknown error")[:500]
            logger.error("Slack agent failed (exit=%d): %s", exit_code, error_msg)
            say(text=f"Agent failed (exit code {exit_code}):\n```\n{_truncate(error_msg, 500)}\n```",
                thread_ts=ts)
            self._react(client, channel, ts, "x")
            return

        # Parse output and reply
        from src.container_runner import _parse_claude_output
        output_text, _ = _parse_claude_output(stdout)
        reply = _truncate(output_text) if output_text else "Task completed (no output)."

        say(text=reply, thread_ts=ts)
        self._react(client, channel, ts, "white_check_mark")
        logger.info("Slack task completed for user %s", user)

    def _react(self, client, channel: str, ts: str, emoji: str) -> None:
        """Add a reaction, removing eyes first."""
        try:
            client.reactions_remove(channel=channel, timestamp=ts, name="eyes")
        except Exception:
            pass
        try:
            client.reactions_add(channel=channel, timestamp=ts, name=emoji)
        except Exception:
            logger.debug("Could not add %s reaction", emoji)

    def start(self) -> None:
        """Start the Socket Mode handler in a daemon thread."""
        handler = SocketModeHandler(self.app, self.app_token)

        thread = threading.Thread(
            target=handler.start,
            daemon=True,
            name="slack-listener",
        )
        thread.start()
        logger.info("Slack Socket Mode listener started")
