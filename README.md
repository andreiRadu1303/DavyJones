<p align="center">
  <img src="assets/icon.png" width="150" alt="DavyJones">
</p>

<h1 align="center">DavyJones</h1>

<p align="center">
  AI-powered task automation for Obsidian vaults, driven by Claude.
</p>

---

## What is DavyJones?

DavyJones turns your Obsidian vault into a task queue for AI agents. Write a note, mark it as a task, and commit — DavyJones picks it up, plans the work, and executes it using Claude-powered agents running in Docker containers.

It connects your vault to external services (GitHub, GitLab, Slack) through MCP servers, so agents can create branches, open merge requests, post messages, and more — all triggered from a simple note.

## Architecture

```
Obsidian Vault
    |
    |  DavyJones Plugin (status bar, task commands, settings)
    |
    v
Git Commit ──> Dispatcher ──> Overseer Agent
                   |               |
                   |          Decomposes jobs
                   |          into sub-tasks
                   |               |
                   v               v
              Agent Containers (ephemeral)
              ┌─────────────────────────┐
              │  Claude CLI + MCP       │
              │                         │
              │  ┌─────┐ ┌──────────┐   │
              │  │Vault│ │ GitHub   │   │
              │  │(MCP)│ │ GitLab   │   │
              │  │     │ │ Slack    │   │
              │  └─────┘ └──────────┘   │
              └─────────────────────────┘
```

**Flow:** Write a note → git commit → dispatcher detects change → overseer analyzes and decomposes into sub-tasks → agents execute concurrently → results written back to vault.

## Quick Start

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (with Docker Compose)
- [Obsidian](https://obsidian.md/)
- [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) (`npm install -g @anthropic-ai/claude-code`)
- A Claude authentication token (`claude setup-token` for a long-lived token)

### Setup

```bash
# 1. Clone the repo
git clone https://github.com/andreiRadu1303/DavyJones.git
cd DavyJones

# 2. Generate a long-lived Claude token
claude setup-token

# 3. Install plugin into your vault and configure
./davyjones setup /path/to/your/vault

# 4. Add your Claude token
#    Either: edit the .davyjones.env in your vault root
#    Or: open Obsidian → Settings → DavyJones → paste token → Apply Changes

# 5. Start the services
./davyjones start
```

A terminal window opens with live logs. Your vault is now connected.

### First Task

Create a note in Obsidian and write what you want done:

```markdown
Say hello! Write a greeting message in this file.
```

Commit the file (click the git status indicator in Obsidian's status bar, or commit manually). The dispatcher detects the change, the overseer analyzes it, and an agent executes the work — all within seconds.

No special frontmatter is required. The overseer reads the commit diff and decides what work to do. You can optionally use `type: task` or `type: job` in frontmatter for more structured workflows.

## CLI Reference

| Command | Description |
|---------|-------------|
| `./davyjones setup [vault-path]` | Install plugin into vault, create config files, extract credentials |
| `./davyjones start` | Build images and start services (opens a new terminal window) |
| `./davyjones start --here` | Same, but runs in the current terminal |
| `./davyjones stop` | Stop all running services |
| `./davyjones clean` | Stop and remove all containers, images, and volumes |
| `./davyjones logs` | Tail live service logs |

## Configuration

Tokens and settings live in `.davyjones.env` in your vault root. You can edit this file directly or use the plugin's Settings tab in Obsidian.

| Variable | Required | Description |
|----------|----------|-------------|
| `CLAUDE_CODE_OAUTH_TOKEN` | Yes | Long-lived token from `claude setup-token` |
| `GITHUB_TOKEN` | No | GitHub Personal Access Token |
| `GITHUB_REPO` | No | Repository to monitor (`owner/repo`) |
| `GITLAB_TOKEN` | No | GitLab Personal Access Token |
| `SLACK_BOT_TOKEN` | No | Slack bot OAuth token |
| `SLACK_APP_TOKEN` | No | Slack Socket Mode token (for @mentions) |

Optional MCP services (Slack, GitHub, GitLab) start automatically when their tokens are configured.

## How It Works

1. **Plugin** — The Obsidian plugin adds a status bar (connection/git status), commands (send to Claude, commit, switch vault), and a settings tab for token management.

2. **Dispatcher** — A Python service running in Docker. Polls the vault's git repo for new commits. When it detects changes, it gathers context and sends it to the overseer.

3. **Overseer** — A Claude agent that analyzes the commit diff and decides what work to do. It decomposes jobs into focused sub-tasks with dependency ordering.

4. **Agents** — Ephemeral Docker containers, each running Claude CLI with MCP connections to the vault and any configured services. They execute one sub-task each, and independent tasks run concurrently.

5. **Results** — Agents write their output back to the vault files. The dispatcher updates task status in the note's frontmatter.

## Optional Integrations

### GitHub
Set `GITHUB_TOKEN` and `GITHUB_REPO` to enable:
- GitHub MCP server — agents can create branches, PRs, issues
- Activity monitor — polls for repo events and triggers vault documentation updates

### GitLab
Set `GITLAB_TOKEN` to enable:
- GitLab MCP server — agents can manage repositories, merge requests, issues

### Slack
Set `SLACK_BOT_TOKEN` (and optionally `SLACK_APP_TOKEN` for Socket Mode) to enable:
- Slack MCP server — agents can read/post messages
- Interactive bot — @mention DavyJones in Slack to trigger tasks

## Project Structure

```
DavyJones/
├── davyjones              # CLI entry point
├── docker-compose.yml     # Service definitions
├── obsidian-plugin/       # Obsidian plugin (JS)
├── dispatcher/            # Main orchestrator (Python)
├── agent/                 # Ephemeral Claude agent container
├── auto_committer/        # Optional auto-commit service
├── obsidian-mcp/          # Vault access MCP server
├── slack-mcp/             # Slack MCP server
├── github-mcp/            # GitHub MCP server
└── scripts/               # Setup and utility scripts
```

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).
