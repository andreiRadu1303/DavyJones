"""Custom Slack MCP server with comprehensive tool coverage.

Exposes Slack Web API methods as MCP tools over SSE transport.
Replaces the limited korotovsky/slack-mcp-server with full coverage
of channel management, messaging, users, reactions, search, and more.
"""

import json
import os

from mcp.server.fastmcp import FastMCP
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

app = FastMCP("slack", host="0.0.0.0", port=3001)

token = os.environ.get("SLACK_BOT_TOKEN", "")
client = WebClient(token=token)


def _err(e: SlackApiError) -> str:
    return f"Slack API error: {e.response['error']}"


def _fmt_channel(ch: dict) -> str:
    kind = "private" if ch.get("is_private") else "public"
    members = ch.get("num_members", "?")
    return f"#{ch['name']} (id: {ch['id']}, {kind}, {members} members)"


def _fmt_message(msg: dict) -> str:
    user = msg.get("user", msg.get("bot_id", "unknown"))
    text = msg.get("text", "")[:300]
    ts = msg.get("ts", "")
    return f"[{ts}] {user}: {text}"


def _fmt_user(u: dict) -> str:
    profile = u.get("profile", {})
    name = profile.get("real_name", u.get("name", "unknown"))
    title = profile.get("title", "")
    email = profile.get("email", "")
    parts = [f"{name} (id: {u['id']})"]
    if title:
        parts.append(f"title: {title}")
    if email:
        parts.append(f"email: {email}")
    return " | ".join(parts)


# ── Channel Management ───────────────────────────────────────────────


@app.tool()
def list_channels(limit: int = 100, include_private: bool = False) -> str:
    """List Slack channels in the workspace."""
    try:
        types = "public_channel,private_channel" if include_private else "public_channel"
        result = client.conversations_list(limit=limit, types=types)
        channels = result["channels"]
        if not channels:
            return "No channels found."
        lines = [_fmt_channel(ch) for ch in channels]
        return f"Found {len(channels)} channels:\n" + "\n".join(lines)
    except SlackApiError as e:
        return _err(e)


@app.tool()
def get_channel_info(channel: str) -> str:
    """Get detailed info about a channel. Accepts channel ID or #name."""
    try:
        channel_id = _resolve_channel(channel)
        result = client.conversations_info(channel=channel_id)
        ch = result["channel"]
        topic = ch.get("topic", {}).get("value", "")
        purpose = ch.get("purpose", {}).get("value", "")
        info = [
            _fmt_channel(ch),
            f"  topic: {topic}" if topic else "",
            f"  purpose: {purpose}" if purpose else "",
            f"  created: {ch.get('created', '?')}",
        ]
        return "\n".join(line for line in info if line)
    except SlackApiError as e:
        return _err(e)


@app.tool()
def create_channel(name: str, is_private: bool = False) -> str:
    """Create a new Slack channel."""
    try:
        result = client.conversations_create(name=name, is_private=is_private)
        ch = result["channel"]
        return f"Created channel #{ch['name']} (id: {ch['id']})"
    except SlackApiError as e:
        return _err(e)


@app.tool()
def set_channel_topic(channel: str, topic: str) -> str:
    """Set the topic of a channel. Accepts channel ID or #name."""
    try:
        channel_id = _resolve_channel(channel)
        client.conversations_setTopic(channel=channel_id, topic=topic)
        return f"Topic set on {channel}"
    except SlackApiError as e:
        return _err(e)


@app.tool()
def set_channel_purpose(channel: str, purpose: str) -> str:
    """Set the purpose/description of a channel. Accepts channel ID or #name."""
    try:
        channel_id = _resolve_channel(channel)
        client.conversations_setPurpose(channel=channel_id, purpose=purpose)
        return f"Purpose set on {channel}"
    except SlackApiError as e:
        return _err(e)


@app.tool()
def invite_to_channel(channel: str, user_id: str) -> str:
    """Invite a user to a channel. Accepts channel ID or #name."""
    try:
        channel_id = _resolve_channel(channel)
        client.conversations_invite(channel=channel_id, users=user_id)
        return f"Invited {user_id} to {channel}"
    except SlackApiError as e:
        return _err(e)


@app.tool()
def join_channel(channel: str) -> str:
    """Join a public channel. Accepts channel ID or #name."""
    try:
        channel_id = _resolve_channel(channel)
        client.conversations_join(channel=channel_id)
        return f"Joined {channel}"
    except SlackApiError as e:
        return _err(e)


@app.tool()
def archive_channel(channel: str) -> str:
    """Archive a channel. Accepts channel ID or #name."""
    try:
        channel_id = _resolve_channel(channel)
        client.conversations_archive(channel=channel_id)
        return f"Archived {channel}"
    except SlackApiError as e:
        return _err(e)


# ── Messaging ────────────────────────────────────────────────────────


@app.tool()
def post_message(channel: str, text: str, thread_ts: str = "") -> str:
    """Post a message to a Slack channel. Use thread_ts to reply in a thread."""
    try:
        kwargs: dict = {"channel": channel, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        result = client.chat_postMessage(**kwargs)
        return f"Message posted to {channel} (ts: {result['ts']})"
    except SlackApiError as e:
        return _err(e)


@app.tool()
def update_message(channel: str, ts: str, text: str) -> str:
    """Update an existing message."""
    try:
        client.chat_update(channel=channel, ts=ts, text=text)
        return f"Message {ts} updated in {channel}"
    except SlackApiError as e:
        return _err(e)


@app.tool()
def delete_message(channel: str, ts: str) -> str:
    """Delete a message."""
    try:
        client.chat_delete(channel=channel, ts=ts)
        return f"Message {ts} deleted from {channel}"
    except SlackApiError as e:
        return _err(e)


@app.tool()
def get_channel_history(channel: str, limit: int = 10) -> str:
    """Get recent messages from a channel. Accepts channel ID or #name."""
    try:
        channel_id = _resolve_channel(channel)
        result = client.conversations_history(channel=channel_id, limit=limit)
        messages = result.get("messages", [])
        if not messages:
            return "No messages found."
        lines = [_fmt_message(m) for m in messages]
        return f"Last {len(messages)} messages:\n" + "\n".join(lines)
    except SlackApiError as e:
        return _err(e)


@app.tool()
def get_thread_replies(channel: str, thread_ts: str, limit: int = 10) -> str:
    """Get replies in a thread."""
    try:
        channel_id = _resolve_channel(channel)
        result = client.conversations_replies(
            channel=channel_id, ts=thread_ts, limit=limit
        )
        messages = result.get("messages", [])
        if not messages:
            return "No replies found."
        lines = [_fmt_message(m) for m in messages]
        return f"{len(messages)} messages in thread:\n" + "\n".join(lines)
    except SlackApiError as e:
        return _err(e)


# ── Reactions ────────────────────────────────────────────────────────


@app.tool()
def add_reaction(channel: str, timestamp: str, emoji: str) -> str:
    """Add an emoji reaction to a message. Emoji name without colons."""
    try:
        client.reactions_add(channel=channel, timestamp=timestamp, name=emoji)
        return f"Added :{emoji}: to message {timestamp}"
    except SlackApiError as e:
        return _err(e)


@app.tool()
def remove_reaction(channel: str, timestamp: str, emoji: str) -> str:
    """Remove an emoji reaction from a message."""
    try:
        client.reactions_remove(channel=channel, timestamp=timestamp, name=emoji)
        return f"Removed :{emoji}: from message {timestamp}"
    except SlackApiError as e:
        return _err(e)


# ── Users ────────────────────────────────────────────────────────────


@app.tool()
def list_users(limit: int = 100) -> str:
    """List users in the workspace."""
    try:
        result = client.users_list(limit=limit)
        members = [u for u in result["members"] if not u.get("is_bot") and not u.get("deleted")]
        if not members:
            return "No users found."
        lines = [_fmt_user(u) for u in members]
        return f"Found {len(members)} users:\n" + "\n".join(lines)
    except SlackApiError as e:
        return _err(e)


@app.tool()
def get_user_info(user_id: str) -> str:
    """Get detailed info about a user by their ID."""
    try:
        result = client.users_info(user=user_id)
        u = result["user"]
        profile = u.get("profile", {})
        info = [
            f"Name: {profile.get('real_name', u.get('name', '?'))}",
            f"ID: {u['id']}",
            f"Display name: {profile.get('display_name', '')}",
            f"Title: {profile.get('title', '')}",
            f"Email: {profile.get('email', '')}",
            f"Status: {profile.get('status_text', '')}",
            f"Timezone: {u.get('tz', '')}",
            f"Bot: {u.get('is_bot', False)}",
            f"Admin: {u.get('is_admin', False)}",
        ]
        return "\n".join(info)
    except SlackApiError as e:
        return _err(e)


# ── Search ───────────────────────────────────────────────────────────


@app.tool()
def search_messages(query: str, count: int = 5) -> str:
    """Search messages in the workspace. Requires a user token for some workspaces."""
    try:
        result = client.search_messages(query=query, count=count)
        matches = result.get("messages", {}).get("matches", [])
        if not matches:
            return f"No messages found for query: {query}"
        lines = []
        for m in matches:
            ch = m.get("channel", {}).get("name", "?")
            user = m.get("username", "?")
            text = m.get("text", "")[:200]
            ts = m.get("ts", "")
            lines.append(f"[#{ch}] {user} ({ts}): {text}")
        return f"Found {len(matches)} results:\n" + "\n".join(lines)
    except SlackApiError as e:
        return _err(e)


# ── Pins ─────────────────────────────────────────────────────────────


@app.tool()
def pin_message(channel: str, timestamp: str) -> str:
    """Pin a message in a channel."""
    try:
        client.pins_add(channel=channel, timestamp=timestamp)
        return f"Pinned message {timestamp} in {channel}"
    except SlackApiError as e:
        return _err(e)


@app.tool()
def unpin_message(channel: str, timestamp: str) -> str:
    """Unpin a message in a channel."""
    try:
        client.pins_remove(channel=channel, timestamp=timestamp)
        return f"Unpinned message {timestamp} in {channel}"
    except SlackApiError as e:
        return _err(e)


# ── Bookmarks ────────────────────────────────────────────────────────


@app.tool()
def add_bookmark(channel: str, title: str, link: str) -> str:
    """Add a bookmark to a channel."""
    try:
        channel_id = _resolve_channel(channel)
        client.bookmarks_add(channel_id=channel_id, title=title, type="link", link=link)
        return f"Bookmark '{title}' added to {channel}"
    except SlackApiError as e:
        return _err(e)


# ── Reminders ────────────────────────────────────────────────────────


@app.tool()
def add_reminder(text: str, time: str) -> str:
    """Add a reminder. Time can be unix timestamp, or natural language like 'in 5 minutes', 'tomorrow at 9am'."""
    try:
        client.reminders_add(text=text, time=time)
        return f"Reminder set: {text}"
    except SlackApiError as e:
        return _err(e)


# ── User Groups ──────────────────────────────────────────────────────


@app.tool()
def list_usergroups() -> str:
    """List user groups in the workspace."""
    try:
        result = client.usergroups_list()
        groups = result.get("usergroups", [])
        if not groups:
            return "No user groups found."
        lines = []
        for g in groups:
            lines.append(f"@{g['handle']} — {g.get('name', '?')} (id: {g['id']})")
        return f"Found {len(groups)} user groups:\n" + "\n".join(lines)
    except SlackApiError as e:
        return _err(e)


@app.tool()
def create_usergroup(name: str, handle: str, description: str = "") -> str:
    """Create a user group (e.g., @team-name)."""
    try:
        kwargs: dict = {"name": name, "handle": handle}
        if description:
            kwargs["description"] = description
        result = client.usergroups_create(**kwargs)
        g = result["usergroup"]
        return f"Created user group @{g['handle']} (id: {g['id']})"
    except SlackApiError as e:
        return _err(e)


# ── Files ────────────────────────────────────────────────────────────


@app.tool()
def upload_file(channels: str, content: str, filename: str, title: str = "") -> str:
    """Upload a text file to a channel. Content is the file text."""
    try:
        kwargs: dict = {
            "channels": channels,
            "content": content,
            "filename": filename,
        }
        if title:
            kwargs["title"] = title
        client.files_upload_v2(**kwargs)
        return f"File '{filename}' uploaded to {channels}"
    except SlackApiError as e:
        return _err(e)


# ── Helpers ──────────────────────────────────────────────────────────


_channel_cache: dict[str, str] = {}


def _resolve_channel(channel: str) -> str:
    """Resolve a channel name to an ID. Passes through if already an ID."""
    if channel.startswith("C") and len(channel) >= 9:
        return channel  # Already an ID

    name = channel.lstrip("#")
    if name in _channel_cache:
        return _channel_cache[name]

    try:
        result = client.conversations_list(limit=200, types="public_channel,private_channel")
        for ch in result["channels"]:
            _channel_cache[ch["name"]] = ch["id"]
            if ch["name"] == name:
                return ch["id"]
    except SlackApiError:
        pass

    return channel  # Return as-is, let Slack API handle the error


if __name__ == "__main__":
    app.run(transport="sse")
