"""Build the prompt for the overseer agent container.

The overseer receives information about what changed in a commit and decides
whether any work needs to be done. It returns a structured JSON plan.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CommitData:
    """Everything the overseer needs to analyze a commit."""
    changed_files: list[str]  # vault-relative paths
    context: str  # accumulated lightweight context from resolve_batch()
    diff_text: str  # unified diff of .md file changes


def build(commit_data: CommitData, vault_rules: dict | None = None) -> str:
    """Build the full prompt for the overseer container."""
    parts = [
        "You are the DavyJones overseer agent. Your job is to analyze a git commit",
        "in an Obsidian vault, decide what work needs to be done, and DECOMPOSE",
        "each job into focused sub-tasks that run as separate agent containers.",
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
        '      "max_turns": 5',
        '    }',
        '  ]',
        '}',
        "",
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
        "- Sub-task prompts should instruct the agent to DO the work and output a summary of what it did",
        "- Sub-task prompts must NOT instruct agents to write results back to the task file",
        "  (the dispatcher aggregates all results automatically)",
        "",
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
        "## Example decomposition",
        "",
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
        'Another example — a vague job: "Keep our Slack team updated on the project."',
        "The overseer should interpret this and decide what to do:",
        '  t1: "Check GitLab for recent activity (issues, MRs, commits)" (max_turns: 30)',
        '  t2: "Read vault project notes for context" (max_turns: 25)',
        '  t3: "Post a project status summary to #general" (max_turns: 30, depends_on: ["t1","t2"])',
        "",
        "## Context format",
        "",
        "- Each file shows its full text, ancestor file paths, and sibling file paths",
        "- Wiki-links like [[path/to/file]] are cross-references the agent can follow",
        "",
    ]

    # Vault-specific rules
    if vault_rules:
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

    # Changed files summary
    parts.extend([
        "## Changed Files",
        "",
        f"The following {len(commit_data.changed_files)} file(s) were changed in this commit:",
        "",
    ])
    for path in commit_data.changed_files:
        parts.append(f"- `{path}`")
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
