<p align="center">
  <img src="assets/icon.png" width="150" alt="DavyJones">
</p>

<h1 align="center">DavyJones</h1>

<p align="center">
  AI-powered task automation for Obsidian vaults, driven by Claude.
</p>

---

## What is DavyJones?

DavyJones turns your Obsidian vault into a task queue for AI agents. Write a note, commit it, and DavyJones picks it up — an overseer agent analyzes your changes, decomposes the work into sub-tasks, and ephemeral Claude-powered agents execute them concurrently inside Docker containers.

Agents connect to external services (GitHub, GitLab, Slack, Google Workspace) through MCP servers, so they can create branches, open PRs, post messages, manage calendars, and more — all triggered from a simple note or a scheduled event.

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                        Obsidian Vault                              │
│                                                                    │
│  Notes (.md)   Tasks (type: task)   Calendar (.davyjones-calendar) │
│                                                                    │
│  ┌──────────────────────────────────────┐                          │
│  │        DavyJones Obsidian Plugin     │                          │
│  │  Status bar · Task UI · Calendar     │                          │
│  │  Live Tasks · Reports · Settings     │                          │
│  └──────────────────────────────────────┘                          │
└──────────────┬──────────────────────┬──────────────────────────────┘
               │ git commit           │ HTTP API (:5555)
               v                      v
┌──────────────────────────────────────────────────────────────────────┐
│                         Dispatcher                                   │
│                                                                      │
│  Git Watcher ──> Overseer Agent ──> Task Executor (concurrent)       │
│                                                                      │
│  Background Services:                                                │
│  · Slack Listener    · GitHub Monitor    · Calendar Scheduler        │
│  · HTTP API          · Scribe (reports)  · MCP Manager               │
└──────────────┬───────────────────────────────────────────────────────┘
               │ spawns per task
               v
┌──────────────────────────────────────────────────────────────────────┐
│                    Ephemeral Agent Containers                         │
│                                                                      │
│  Claude CLI + MCP config ──> connects to service containers          │
│                                                                      │
│  ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌────────────┐           │
│  │ Obsidian  │ │  GitHub   │ │   Slack   │ │ DavyJones  │  ...      │
│  │ MCP :3010 │ │ MCP :3003 │ │ MCP :3001 │ │ MCP :3004  │           │
│  └───────────┘ └───────────┘ └───────────┘ └────────────┘           │
└──────────────────────────────────────────────────────────────────────┘
```

### Task Lifecycle

```
  Write/edit note ──> git commit ──> Dispatcher detects change
                                           │
                                     Overseer agent analyzes
                                     diff + vault context
                                           │
                                     Generates task plan
                                     (JSON with dependencies)
                                           │
                                     Topological sort into
                                     execution levels
                                           │
                              ┌────────────┼────────────┐
                              v            v            v
                          Agent A      Agent B      Agent C    (level 0, concurrent)
                              │            │            │
                              v            v            v
                          Agent D      Agent E               (level 1, after deps)
                              │            │
                              v            v
                        Auto-commit results to vault
                              │
                        Update frontmatter status
                              │
                        Generate HTML report
```

## Quick Start

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with Docker Compose
- [Obsidian](https://obsidian.md/)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`npm install -g @anthropic-ai/claude-code`)
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
#    Either: edit .davyjones.env in your vault root
#    Or: Obsidian → Settings → DavyJones → paste token → Apply Changes

# 5. Start the services
./davyjones start
```

A terminal window opens with live logs. Your vault is now connected.

### First Task

Create a note in Obsidian:

```markdown
Say hello! Write a greeting message in this file.
```

Commit the file (click the git indicator in the status bar, or commit manually). The dispatcher detects the change, the overseer analyzes it, and an agent executes the work — all within seconds.

No special frontmatter is required. The overseer reads the commit diff and decides what to do. You can optionally use `type: task` or `type: job` in YAML frontmatter for more structured workflows.

## CLI Reference

| Command | Description |
|---------|-------------|
| `./davyjones setup [vault-path]` | Install plugin, create config files, extract credentials |
| `./davyjones start` | Build images and start services (opens a new terminal window) |
| `./davyjones start --here` | Same, but runs in the current terminal |
| `./davyjones stop` | Stop all running services |
| `./davyjones clean` | Remove all containers, images, and volumes |
| `./davyjones logs` | Tail live service logs |

## Configuration

Tokens and settings live in `.davyjones.env` in your vault root. Edit directly or use the plugin's Settings tab.

| Variable | Required | Description |
|----------|----------|-------------|
| `CLAUDE_CODE_OAUTH_TOKEN` | Yes | Long-lived token from `claude setup-token` |
| `GITHUB_TOKEN` | No | GitHub Personal Access Token |
| `GITHUB_REPO` | No | Repository to monitor (`owner/repo`) |
| `GITHUB_POLL_INTERVAL` | No | Seconds between GitHub polls (default: `60`) |
| `GITLAB_TOKEN` | No | GitLab Personal Access Token |
| `SLACK_BOT_TOKEN` | No | Slack bot OAuth token |
| `SLACK_APP_TOKEN` | No | Slack Socket Mode token (for @mention triggers) |
| `GOOGLE_WORKSPACE_ENABLED` | No | Enable Google Workspace CLI (`true`/`false`) |
| `GWS_CONFIG_PATH` | No | Path to Google Workspace OAuth credentials |

### Dispatcher Tuning

These are set in `.env` in the project root:

| Variable | Default | Description |
|----------|---------|-------------|
| `POLL_INTERVAL_SECONDS` | `10` | Git polling frequency |
| `AGENT_TIMEOUT_SECONDS` | `1200` | Max duration per agent container |
| `OVERSEER_TIMEOUT_SECONDS` | `600` | Max duration for overseer planning |
| `OVERSEER_MAX_TURNS` | `50` | Max Claude turns for overseer |
| `MAX_CONCURRENT_AGENTS` | `3` | Parallel agent execution limit |
| `HTTP_PORT` | `5555` | Dispatcher HTTP API port |

### Vault Rules

Create `.davyjones-rules.json` in your vault root for per-vault customization:

```json
{
  "customInstructions": "Always write in Spanish. Use formal tone.",
  "verbosity": "concise",
  "allowedOperations": {
    "createFiles": true,
    "deleteFiles": false,
    "modifyFiles": true,
    "runGitCommands": true
  },
  "serviceInstances": [
    {
      "id": "github-work",
      "service": "github",
      "label": "Work Account",
      "token": "ghp_..."
    }
  ]
}
```

## Components

### Dispatcher

The central orchestrator, running as a Python service in Docker. It coordinates all task execution.

**Core loop:**
1. Polls the vault git repo for new commits (every 10s)
2. Filters for human commits (skips DavyJones auto-commits)
3. Extracts changed files and unified diff
4. Spawns an overseer agent to analyze and plan
5. Executes sub-tasks concurrently (respecting dependency order)
6. Auto-commits results and updates frontmatter status
7. Generates HTML execution reports

**Background services** run as daemon threads:
- **Slack Listener** — Receives @mention tasks via Socket Mode
- **GitHub Monitor** — Polls repo events, triggers vault documentation updates
- **Calendar Scheduler** — Dispatches tasks at scheduled times from `.davyjones-calendar.json`
- **HTTP API** — Direct task submission (`POST /api/task`), live task streaming, reports
- **Scribe** — Async HTML report generation from execution results
- **MCP Manager** — Provisions dynamic MCP containers for multi-account setups

### Obsidian Plugin

A JavaScript plugin that adds UI and controls directly in Obsidian.

**UI elements:**
- **Status bar** — Dispatcher heartbeat (green/red) and git dirty indicator with click-to-commit
- **Live Tasks view** — Real-time progress of in-flight tasks and their sub-tasks
- **Reports view** — Browse past execution reports with full agent output
- **Calendar view** — Visual calendar with drag-to-create events and .ics import
- **Control Panel** — Token management, vault rules, service configuration
- **New Task modal** — Submit tasks directly to the dispatcher without a git commit
- **File decorations** — Highlights files recently modified by agents in the file explorer

**Commands** (available from the command palette):
`Commit changes`, `New task`, `Open live tasks`, `Open reports`, `Open calendar`, `Open control panel`, `Switch vault`, `Import ICS`, `Clear Claude markers`

### Ephemeral Agents

Each sub-task runs in an isolated Docker container:

1. `entrypoint.sh` sets up Claude credentials (OAuth token or credential file)
2. Generates MCP config JSON with SSE URLs for all available services
3. Runs connectivity checks against each MCP server
4. Invokes Claude CLI with task prompt piped via stdin
5. Container exits after completion; dispatcher collects output

Agents have read-write access to the vault and connect to MCP servers over the Docker network.

### MCP Servers

| Server | Port | Transport | Description |
|--------|------|-----------|-------------|
| **obsidian-mcp** | 3010 | HTTP | Read, write, list, search vault files. Always on. |
| **davyjones-mcp** | 3004 | SSE | Calendar event CRUD, scheduled task management. Always on. |
| **slack-mcp** | 3001 | SSE | Slack Web API — channels, messages, reactions, search, users. Requires `SLACK_BOT_TOKEN`. |
| **github-mcp** | 3003 | SSE | Official GitHub MCP — repos, PRs, issues, branches, releases. Requires `GITHUB_TOKEN`. |
| **gitlab-mcp** | 3002 | SSE | GitLab API — projects, MRs, issues, commits. Requires `GITLAB_TOKEN`. |

Optional MCP servers start automatically when their tokens are configured. Additional service instances (e.g., a second GitHub account) are provisioned dynamically by the MCP Manager.

### Auto-Committer (Optional)

An optional service that watches the vault for file changes and auto-commits after a debounce period.

```bash
# Enable with the auto-commit profile
docker compose --profile auto-commit up
```

Configurable via `AUTO_COMMIT_DEBOUNCE_SECONDS` (default: 5).

## Task Sources

DavyJones supports multiple ways to trigger agent work:

### 1. Git Commits (Primary)

Write or edit notes in Obsidian, then commit. The dispatcher analyzes the diff and dispatches work automatically.

### 2. Direct Tasks (Plugin UI)

Click the "New Task" button in Obsidian to submit a task directly via the HTTP API — no commit needed. Track progress in the Live Tasks view.

### 3. Slack @Mentions

@mention DavyJones in any Slack channel. The agent executes the request and replies in-thread with the result.

```
@DavyJones summarize the last week of commits in our vault
```

### 4. Calendar Scheduling

Create scheduled tasks in the Calendar view. Events with `type: "task"` dispatch automatically at their start time. Supports recurrence (daily, weekly, monthly, yearly).

The **DavyJones MCP** lets agents manage calendar entries programmatically:
- `list_calendar_events` — query events by date range
- `get_calendar_event` — fetch a specific event
- `create_calendar_event` — create a regular event
- `create_calendar_task` — create a recurring agent task
- `delete_calendar_event` — remove an event

### 5. GitHub Activity Monitor

When configured, the dispatcher polls your GitHub repo for events (pushes, PRs, issues, releases) and creates tasks to update vault documentation accordingly.

## HTTP API

The dispatcher exposes a REST API on port 5555 (configurable):

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/task` | POST | Submit a new task (`{"prompt": "..."}`) |
| `/api/tasks` | GET | List all active/recent tasks with sub-task status |
| `/api/reports` | GET | List execution reports (paginated) |
| `/api/reports/<id>` | GET | Fetch a specific report (HTML) |
| `/api/claude-changes` | GET | List files recently modified by agents |
| `/api/claude-changes/clear` | POST | Clear the modification tracker |

## Google Workspace

When configured, agents can interact with Gmail, Drive, Calendar, Sheets, and Docs via the `gws` CLI tool:

```bash
gws gmail users messages list --params '{"userId":"me","q":"search query"}'
gws drive files list --params '{"q":"name contains \"report\""}'
gws calendar events list --params '{"calendarId":"primary"}'
gws sheets spreadsheets values get --params '{"spreadsheetId":"ID","range":"Sheet1"}'
```

Requires mounting Google Workspace OAuth credentials via `GWS_CONFIG_PATH`.

## Project Structure

```
DavyJones/
├── davyjones                 # CLI entry point (bash)
├── docker-compose.yml        # Service orchestration
├── .env                      # Project-level environment
│
├── obsidian-plugin/          # Obsidian plugin
│   ├── main.js               #   Plugin logic — UI, git, task submission, calendar
│   ├── styles.css            #   Plugin styles
│   └── manifest.json         #   Plugin metadata
│
├── dispatcher/               # Central orchestrator (Python)
│   ├── Dockerfile
│   └── src/
│       ├── main.py           #   Primary loop — git polling, task execution
│       ├── config.py         #   Environment config
│       ├── models.py         #   Data models (DispatchPayload, TaskResult)
│       ├── git_watcher.py    #   Git change detection
│       ├── overseer.py       #   Overseer agent execution
│       ├── overseer_prompt.py#   Overseer prompt construction
│       ├── plan_models.py    #   Task plan models + topological sort
│       ├── prompt_builder.py #   Agent prompt generation
│       ├── container_runner.py#  Docker container management
│       ├── context_resolver.py#  Vault context extraction
│       ├── task_builder.py   #   Task payload construction
│       ├── status_updater.py #   Frontmatter status updates
│       ├── http_api.py       #   Flask HTTP API
│       ├── scribe.py         #   Report generation
│       ├── slack_listener.py #   Slack Socket Mode listener
│       ├── slack_prompt.py   #   Slack task prompts
│       ├── github_monitor.py #   GitHub event polling
│       ├── calendar_scheduler.py# Scheduled task dispatch
│       ├── vault_rules.py    #   .davyjones-rules.json loader
│       ├── mcp_manager.py    #   Dynamic MCP container provisioning
│       ├── token_refresh.py  #   Credential validation
│       └── claude_changes.py #   File modification tracking
│
├── agent/                    # Ephemeral agent container
│   ├── Dockerfile
│   └── entrypoint.sh         #   Credential setup, MCP config, Claude CLI
│
├── davyjones-mcp/            # DavyJones MCP server (calendar tools)
│   ├── Dockerfile
│   ├── server.py
│   └── requirements.txt
│
├── obsidian-mcp/             # Vault file access MCP server
│   └── Dockerfile
│
├── slack-mcp/                # Slack MCP server
│   ├── Dockerfile
│   ├── server.py
│   └── requirements.txt
│
├── github-mcp/               # GitHub MCP server
│   └── Dockerfile
│
├── auto_committer/           # Optional auto-commit service
│   ├── Dockerfile
│   └── src/
│       ├── main.py
│       ├── watcher.py
│       └── committer.py
│
├── scripts/                  # Utility scripts
│   ├── setup.sh
│   ├── extract_credentials.sh
│   ├── fix_credentials.sh
│   └── seed_example_task.sh
│
└── assets/
    └── icon.png
```

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).
