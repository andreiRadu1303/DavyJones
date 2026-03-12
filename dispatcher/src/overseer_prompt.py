"""Build prompts for the overseer agent container.

Supports two modes:
1. Commit-based: analyze a git commit and decide what work is needed.
2. Direct task: user submitted a task explicitly from the Obsidian plugin.

Both share the same JSON schema, decomposition rules, and vault rules.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CommitData:
    """Everything the overseer needs to analyze a commit."""
    changed_files: list[str]  # vault-relative paths
    context: str  # accumulated lightweight context from resolve_batch()
    diff_text: str  # unified diff of .md file changes


# ─── Shared prompt sections ────────────────────────────────────


def _json_schema_block() -> list[str]:
    """The JSON plan schema all overseer prompts share."""
    return [
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
        '      "max_turns": 5',
        '    }',
        '  ]',
        '}',
        "",
    ]


def _decomposition_rules() -> list[str]:
    """Job decomposition rules shared by all overseer modes."""
    return [
        "## Job Decomposition Rules",
        "",
        "Each pending task/job file is a JOB. Decompose each job into multiple sub-tasks:",
        "",
        "- Break the job into logical sub-tasks — YOU decide the decomposition based on the goal",
        "- The user describes WHAT they want, not HOW to split it. You figure out the tasks.",
        "- Each sub-task should be a focused unit of work (one service call or tightly coupled pair)",
        "- Sub-tasks that are independent MUST have empty `depends_on` — they run as CONCURRENT agents",
        "- Sub-tasks that require ordering use `depends_on` (e.g., create files after creating a branch)",
        "- Set `max_turns` generously — agents that hit max_turns waste more resources retrying:",
        "  - Simple single-call tasks (post a message, add a reaction): 25",
        "  - Tasks requiring lookup first (find a channel/user, then act): 30",
        "  - Multi-step tasks (create + configure + verify): 35",
        "  - Complex cross-service tasks: 40-50",
        "- All sub-tasks for the same job share the same `file_path` (the job file)",
        "- Each sub-task `prompt` must be self-contained — the agent won't see other tasks or this plan",
        "- Give each agent enough context in its prompt to work independently (IDs, names, paths)",
        "- Sub-task prompts should instruct the agent to DO the work",
        "- Sub-task prompts must NOT instruct agents to write results back to the task file",
        "  (the dispatcher aggregates all results automatically)",
        "",
        "## Batch Vault Work — Split by File Groups, NOT by Phase",
        "",
        "When a job applies the SAME operation to many vault files (reformat notes, add tags,",
        "unify templates, etc.), split the work BY FILE GROUPS across concurrent agents.",
        "",
        "WRONG — splitting by phase (sequential, slow):",
        '  t1: "Discover all video game notes" (depends_on: [])',
        '  t2: "Reformat all video game notes" (depends_on: ["t1"])',
        "This is bad because: (a) t2 waits for t1 to finish, doubling time; (b) you ALREADY have",
        "the file list and context below — there is nothing to 'discover'.",
        "",
        "RIGHT — splitting by file groups (concurrent, fast):",
        '  t1: "Reformat notes: FileA.md, FileB.md, FileC.md" (depends_on: [])',
        '  t2: "Reformat notes: FileD.md, FileE.md, FileF.md" (depends_on: [])',
        '  t3: "Reformat notes: FileG.md, FileH.md" (depends_on: [])',
        "All agents run in parallel. 3x faster than sequential.",
        "",
        "KEY RULES for batch vault work:",
        "- You ALREADY have the full file list and content in the Context section below.",
        "  Do NOT create 'discover', 'analyze', or 'gather info' tasks — that work is DONE.",
        "- Divide the files roughly evenly across tasks (aim for 3-5 concurrent agents)",
        "- Each task prompt MUST list the specific file paths it should work on",
        "- Each task prompt MUST include the full instructions for what to do to each file",
        "- If there's a template/pattern to follow, include it in EVERY task prompt (they're independent)",
        "",
    ]


def _examples_block() -> list[str]:
    """Example decompositions shared by all overseer modes."""
    return [
        "## Example decomposition",
        "",
        "### External service work (sequential dependencies)",
        'A job says: "Set up sorting algorithms in our GitLab project."',
        "Good plan (overseer decides the decomposition):",
        '  t1: "Create branch feature/sorting from main" (max_turns: 25, depends_on: [])',
        '  t2: "Create file algorithms/bubble_sort.py with ..." (max_turns: 30, depends_on: ["t1"])',
        '  t3: "Create file algorithms/quick_sort.py with ..." (max_turns: 30, depends_on: ["t1"])',
        '  t4: "Create file algorithms/merge_sort.py with ..." (max_turns: 30, depends_on: ["t1"])',
        '  t5: "Create merge request from feature/sorting to main" (max_turns: 25, depends_on: ["t2","t3","t4"])',
        "",
        "This runs as: Level 0 → t1 (1 agent), Level 1 → t2,t3,t4 (3 concurrent agents),",
        "Level 2 → t5 (1 agent). Total: 5 agents, 3 levels.",
        "",
        "### Batch vault work (file-group splitting)",
        'A job says: "Unify the template for all video game notes." Context shows 8 files.',
        "Good plan (split by file groups, all concurrent):",
        '  t1: "Reformat Games/Zelda.md, Games/Mario.md, Games/Metroid.md to template" (max_turns: 15, depends_on: [])',
        '  t2: "Reformat Games/Halo.md, Games/Doom.md, Games/Quake.md to template" (max_turns: 15, depends_on: [])',
        '  t3: "Reformat Games/Skyrim.md, Games/Witcher.md to template" (max_turns: 15, depends_on: [])',
        "",
        "This runs as: Level 0 → t1,t2,t3 (3 concurrent agents). Total: 3 agents, 1 level.",
        "Each prompt includes the full template and lists the specific files to process.",
        "",
        "### Cross-service work (mixed dependencies)",
        'Another example — a vague job: "Keep our Slack team updated on the project."',
        "The overseer should interpret this and decide what to do:",
        '  t1: "Check GitLab for recent activity (issues, MRs, commits)" (max_turns: 30)',
        '  t2: "Read vault project notes for context" (max_turns: 25)',
        '  t3: "Post a project status summary to #general" (max_turns: 30, depends_on: ["t1","t2"])',
        "",
    ]


def _task_prompt_rules() -> list[str]:
    """Rules that the overseer should embed in every sub-task prompt."""
    return [
        "## Important rules for task prompts",
        "",
        "Include these rules in EVERY sub-task prompt that modifies vault notes:",
        "- NEVER start a note with a heading that duplicates the filename (e.g., if the file is",
        '  named "Zelda.md", do NOT add "# Zelda" as the first line). Obsidian already shows the',
        "  filename as the note's title. Starting content with a duplicate heading looks broken.",
        "- Preserve existing YAML frontmatter — update fields but don't remove the block.",
        "",
    ]


def _context_format_block() -> list[str]:
    """Describes the context section format."""
    return [
        "## Context format",
        "",
        "- Each file shows its full text, ancestor file paths, and sibling file paths",
        "- Wiki-links like [[path/to/file]] are cross-references the agent can follow",
        "",
    ]


def _reports_api_block() -> list[str]:
    """Instructions for querying the execution reports API."""
    return [
        "## Execution Reports",
        "",
        "If the user's request references previous work (e.g., \"the files you just added\",",
        "\"finish what you started\", \"add those to the table of contents\"), query the",
        "execution reports API to understand what was done:",
        "",
        "  curl -s $DAVYJONES_API_URL/api/reports?limit=5",
        "",
        "This returns an index of recent reports with: id, description, status, task_count.",
        "To get full details including per-task file paths and summaries:",
        "",
        "  curl -s $DAVYJONES_API_URL/api/reports/<report_id>",
        "",
        "Use this to identify exactly which files were created or modified and what each",
        "agent did. Include the specific file paths in your sub-task prompts so agents",
        "know exactly what to work with. Do NOT re-execute completed work.",
        "",
    ]


def _vault_rules_block(vault_rules: dict | None) -> list[str]:
    """Vault-specific rules injection (custom instructions, ignore patterns, etc.)."""
    if not vault_rules:
        return []

    parts: list[str] = []

    custom = vault_rules.get("customInstructions", "")
    if custom:
        parts.extend([
            "## Custom Instructions (from vault owner)",
            "",
            custom,
            "",
        ])

    ignore_patterns = vault_rules.get("ignorePatterns", [])
    if ignore_patterns:
        parts.extend([
            "## File Ignore Patterns",
            "",
            "Skip these files/patterns — do not create tasks for them:",
            "",
        ])
        for pattern in ignore_patterns:
            parts.append(f"- `{pattern}`")
        parts.append("")

    ops = vault_rules.get("allowedOperations", {})
    restrictions = []
    if not ops.get("createFiles", True):
        restrictions.append("Do NOT create new files")
    if not ops.get("deleteFiles", True):
        restrictions.append("Do NOT delete files")
    if not ops.get("modifyFiles", True):
        restrictions.append("Do NOT modify existing files (read-only)")
    if not ops.get("runGitCommands", True):
        restrictions.append("Do NOT run git commands")
    if restrictions:
        parts.extend([
            "## Operation Restrictions",
            "",
            "The vault owner has restricted the following operations:",
            "",
        ])
        for r in restrictions:
            parts.append(f"- {r}")
        parts.append("")

    verbosity = vault_rules.get("verbosity", "normal")
    if verbosity == "concise":
        parts.extend(["When setting task prompts, instruct agents to be concise and minimal.", ""])
    elif verbosity == "detailed":
        parts.extend(["When setting task prompts, instruct agents to be thorough and detailed.", ""])

    return parts


# ─── Commit-based prompt ───────────────────────────────────────


def build(commit_data: CommitData, vault_rules: dict | None = None) -> str:
    """Build the full prompt for the commit-based overseer container."""
    parts = [
        "You are the DavyJones overseer agent. Your job is to analyze a git commit",
        "in an Obsidian vault, decide what work needs to be done, and DECOMPOSE",
        "each job into focused sub-tasks that run as separate agent containers.",
        "",
    ]

    parts.extend(_json_schema_block())
    parts.extend(_decomposition_rules())

    parts.extend([
        "## When NOT to decompose",
        "",
        "- If no work is needed, return {\"tasks\": []}",
        "- Context-only changes (_context.md, folder restructuring) → no tasks",
        "- Minor edits (typo fixes, formatting) → no tasks",
        "- A trivial single-step task (e.g., 'post one message') → 1 task is fine",
        "",
        "## What triggers work",
        "",
        '- Files with frontmatter `type: task` or `type: job` → decompose and process',
        '  (if `status` is missing, treat it as pending; skip only if `status: completed` or `status: cancelled`)',
        "- New or substantially changed notes requesting work → create tasks",
        "",
    ])

    parts.extend(_examples_block())
    parts.extend(_task_prompt_rules())
    parts.extend(_context_format_block())
    parts.extend(_vault_rules_block(vault_rules))

    # Changed files summary
    parts.extend([
        "## Changed Files",
        "",
        f"The following {len(commit_data.changed_files)} file(s) were changed in this commit:",
        "",
    ])
    for fpath in commit_data.changed_files:
        parts.append(f"- `{fpath}`")
    parts.append("")

    # Context (accumulated from resolve_batch)
    if commit_data.context:
        parts.extend([
            "## Context",
            "",
            commit_data.context,
            "",
        ])

    # Diff
    if commit_data.diff_text:
        parts.extend([
            "## Git Diff",
            "",
            "```diff",
            commit_data.diff_text,
            "```",
            "",
        ])

    parts.extend([
        "## Your Response",
        "",
        "Analyze the above changes and respond with ONLY the JSON plan object.",
    ])

    return "\n".join(parts)


# ─── Direct-task prompt ────────────────────────────────────────


def build_direct_task(
    description: str,
    scope_files: list[str],
    scope_context: str,
    vault_rules: dict | None = None,
) -> str:
    """Build the overseer prompt for a direct user task (no commit/diff).

    Used when the user submits a task from the Obsidian plugin modal,
    bypassing the commit-based workflow.
    """
    parts = [
        "You are the DavyJones overseer agent. A user has submitted a direct task",
        "from the Obsidian plugin. Your job is to analyze the request and DECOMPOSE",
        "it into focused sub-tasks that run as separate agent containers.",
        "",
        "The user explicitly asked for this work — do NOT return an empty plan",
        "unless the request is truly nonsensical.",
        "",
    ]

    parts.extend(_json_schema_block())
    parts.extend(_decomposition_rules())

    parts.extend([
        "## When NOT to decompose",
        "",
        "- A trivial single-step task (e.g., 'post one message') → 1 task is fine",
        "- If the user's request is unclear, create a single task that interprets and executes it",
        "",
    ])

    parts.extend(_examples_block())
    parts.extend(_task_prompt_rules())
    parts.extend(_context_format_block())
    parts.extend(_vault_rules_block(vault_rules))
    parts.extend(_reports_api_block())

    # User task description
    parts.extend([
        "## User Task",
        "",
        description,
        "",
    ])

    # For direct tasks, always use a placeholder file_path
    parts.extend([
        'For all sub-tasks, set `"file_path"` to `".davyjones-direct-task"`. This is a',
        "placeholder — there is no task note file for direct submissions. Agents are free",
        "to read and modify any vault file they need to complete their work.",
        "",
    ])

    # Scope files
    if scope_files:
        parts.extend([
            "## Scope Files",
            "",
            f"The user selected {len(scope_files)} file(s) for this task:",
            "",
        ])
        for fpath in scope_files:
            parts.append(f"- `{fpath}`")
        parts.append("")

    # Context
    if scope_context:
        parts.extend([
            "## Context",
            "",
            scope_context,
            "",
        ])

    parts.extend([
        "## Your Response",
        "",
        "Analyze the user's task and respond with ONLY the JSON plan object.",
    ])

    return "\n".join(parts)
