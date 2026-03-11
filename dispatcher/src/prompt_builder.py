from src.models import DispatchPayload


def build_prompt(payload: DispatchPayload) -> str:
    """Build the full prompt for Claude CLI from context + task prompt."""
    parts = [
        "You are working inside an Obsidian vault at /vault.",
        "You have tools to read, write, and list files in this vault.",
    ]

    if payload.context:
        parts.extend(["", "## Context", "", payload.context])

    is_subtask = payload.metadata.get("is_subtask", False)

    parts.extend([
        "",
        "## Task",
        "",
        f"Task file: `{payload.task_file_path}`",
        "",
        payload.prompt,
        "",
        "## Instructions",
        "",
    ])

    if is_subtask:
        parts.extend([
            "- You are a sub-task agent — focus ONLY on the specific task described above.",
            "- Do your work using the available MCP tools (Slack, GitLab, Obsidian, etc.).",
            "- When done, output a clear summary: what you did, success or failure, and key details.",
            "- Do NOT modify, write to, or delete the task file — results are aggregated automatically.",
            f"- IMPORTANT: The file `{payload.task_file_path}` is managed by the dispatcher. Do not touch it.",
        ])
    else:
        parts.extend([
            "- Read and write files relative to /vault.",
            "- When creating new notes, include YAML frontmatter.",
            "- Use [[wiki-link]] syntax for cross-references where appropriate.",
            "- Write your results directly to the task file under a '## Results' section.",
        ])

    return "\n".join(parts)
