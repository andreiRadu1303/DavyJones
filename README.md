<p align="center">
  <img src="assets/icon.png" width="150" alt="DavyJones">
</p>

<h1 align="center">DavyJones</h1>

<p align="center">
  Your Obsidian vault, but it works for you.
</p>

---

## Your Notes, Now With a Crew

DavyJones adds AI agents to your Obsidian vault. Write down what you need done, and agents handle it — research, drafting, organizing, scheduling, communicating across your tools. Your notes stop being static files and start being actionable.

You keep writing in Obsidian the way you always have. DavyJones watches for changes, understands what needs doing, and gets to work. Results appear right back in your vault.

## What Makes This Different

Obsidian is a great place to think. DavyJones makes it a great place to get things done.

- **Your vault becomes a task queue.** Any note can trigger work. Write "research competitors in the CRM space" and commit — an agent picks it up, does the research, and writes the results back into your vault.

- **Agents understand your vault.** They read your existing notes for context. A task about a project pulls in related notes, linked files, and folder structure automatically. No copy-pasting context into a chat window.

- **Everything stays in your vault.** Results, reports, summaries — they all land as markdown files you own. No vendor lock-in, no external dashboards. Your vault is the source of truth.

- **Tasks run in the background.** You don't sit and wait. Submit a task, keep writing, and check the results when they're ready. The Live Tasks view shows progress in real time.

- **Complex work gets broken down automatically.** Big tasks are decomposed into focused sub-tasks that run concurrently. A request like "update all project docs and notify the team on Slack" becomes multiple agents working in parallel.

- **Scheduled tasks run themselves.** Set up recurring agent tasks on a calendar — daily summaries, weekly reviews, Monday morning briefings. They run at the scheduled time without any manual trigger.

- **Works with your existing tools.** Agents can read and post on Slack, create GitHub PRs, manage GitLab issues, query Google Workspace — all from instructions written in plain English in your notes.

## Integrations

| Integration | What Agents Can Do | How You Connect |
|-------------|-------------------|-----------------|
| **GitHub** | Create branches, open PRs, comment on issues, manage releases, monitor repo activity | Personal Access Token from github.com/settings/tokens |
| **GitLab** | Manage merge requests, create issues, work with repositories and CI/CD. Self-hosted instances supported | Personal Access Token + (optionally) your custom API URL |
| **Slack** | Read channels, post messages, search conversations, react to messages. Trigger tasks by @mentioning DavyJones | Bot Token (`xoxb-`) for bot-as-agent, or User Token (`xoxp-`) to have agents act as you |
| **Google Workspace** | Search Gmail, read emails, query Calendar, write to Sheets and Docs, manage Drive files the agent created | OAuth via your own Google Cloud project (sidesteps verification — see [docs/gws-setup.md](docs/gws-setup.md)) |
| **DavyJones Calendar** | Schedule one-off or recurring agent tasks, manage events programmatically | Built-in, no setup |

All integrations are optional. Enable them by adding the relevant credential — the corresponding service starts automatically.

DavyJones uses a **bring-your-own-credential** pattern across the board: you create a token (or OAuth client) in each service yourself and paste it in. We never hold credentials for many users centrally, and you're not gated by Google's verification queue or Slack's marketplace review.

## Use Cases

### The Professional

*Sarah is a product manager who tracks everything in Obsidian — meeting notes, roadmaps, competitive analysis, stakeholder updates.*

She writes a note before her Monday morning:

> Summarize all Slack messages from #product-team and #engineering from the past week. Pull any open GitHub PRs that are waiting for review. Write a briefing note I can skim in 5 minutes.

She commits and heads to make coffee. When she opens Obsidian ten minutes later, a new briefing note sits in her vault with a Slack digest, PR status table, and key decisions she missed on Friday. She schedules this as a recurring Monday 8am task so it's always ready before standup.

---

### The Personal User

*Marco uses Obsidian as his life dashboard — journal entries, travel plans, reading lists, household projects.*

He creates a note for an upcoming trip:

> I'm traveling to Kyoto from April 10-17. Research the best neighborhoods to stay in, create a day-by-day itinerary with a mix of temples, food spots, and less touristy walks. Add a packing checklist for spring weather in Japan.

The agent creates a structured travel plan with daily breakdowns, links between related notes, and a packing list — all as vault files he can edit and refine. When his plans change, he updates the note and the agent adjusts.

---

### The Athlete

*Lena is a competitive triathlete who logs training, nutrition, and race prep in Obsidian.*

She has a weekly recurring task every Sunday evening:

> Review my training log entries from this week. Calculate total swim/bike/run volume. Compare against my 12-week plan targets. Flag any sessions I missed or where intensity was below target. Write a weekly summary with recommendations for next week's focus.

Every Sunday at 7pm, an agent reads her daily logs, crunches the numbers, and produces a training review note. It catches that she skipped Thursday's interval session and suggests doubling the intensity on Tuesday to stay on track for her half-iron distance race.

---

### The Developer

*James maintains several open-source projects and uses Obsidian to track contributions, decisions, and documentation.*

He writes a task in his vault:

> Go through all open issues on our GitHub repo tagged "good first issue". For each one, check if there's already a linked PR. If not, write a comment with a suggested approach and tag it "needs-contributor". Update my vault's contributor pipeline note with the current status of each issue.

Agents fan out across the issues — one per issue — checking PR links, posting helpful comments on GitHub, and updating a status table in his vault. What would have taken an hour of tab-switching happens while he's writing code.

## Getting Started

DavyJones runs in two modes. Pick whichever matches your situation:

- **Cloud mode** — agents run on our managed Kubernetes cluster. Zero local setup beyond installing the Obsidian plugin. You get isolated per-vault containers, no Docker on your laptop, vault changes sync automatically over git. Best for everyday use.
- **Self-hosted mode** — agents run locally via Docker Compose. Best if you want everything on your machine, are running on infrastructure you control, or want to develop on the project itself.

### Cloud mode (recommended)

**Prerequisites:** [Obsidian](https://obsidian.md/) and a Claude subscription token (`claude setup-token` from the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)).

1. Install the DavyJones plugin into your Obsidian vault (community plugin browser, or copy `obsidian-plugin/` into `<vault>/.obsidian/plugins/davyjones/`).
2. Open the vault in Obsidian. The onboarding modal pops up automatically.
3. Sign in with Google, paste your Claude token, click **Connect**.
4. Add tokens for any integrations you want (GitHub PAT, GitLab PAT, Slack token, Google Workspace OAuth — see [docs/gws-setup.md](docs/gws-setup.md) for the GWS one-time GCP setup).

You're done. Your vault is now an inbox for agents. Write a task, commit, and the agent's output appears back in your vault automatically.

### Self-hosted mode

**Prerequisites:**
- [Docker](https://docs.docker.com/get-docker/) with Docker Compose
- [Obsidian](https://obsidian.md/)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`npm install -g @anthropic-ai/claude-code`)
- A Claude authentication token (`claude setup-token`)

```bash
git clone https://github.com/andreiRadu1303/DavyJones.git
cd DavyJones

claude setup-token

./davyjones setup /path/to/your/vault
./davyjones start
```

A terminal opens with live logs. Open Obsidian, write something, commit, and watch it work.

Add tokens for optional integrations either by editing `.davyjones.env` in your vault root, or via Settings → DavyJones in Obsidian.

| Command | What it does |
|---------|-------------|
| `./davyjones setup [vault]` | Install plugin and configure vault |
| `./davyjones start` | Start all services |
| `./davyjones start --here` | Start in current terminal |
| `./davyjones stop` | Stop services |
| `./davyjones logs` | Tail service logs |
| `./davyjones clean` | Remove all containers and images |

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for technical details — component breakdown, data flow, MCP servers, HTTP API, configuration reference, and project structure.

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).
