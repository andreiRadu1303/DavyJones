from src.models import DispatchPayload


def build_prompt(payload: DispatchPayload, vault_rules: dict | None = None) -> str:
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

    # Heading rule applies to ALL agents that modify vault notes
    heading_rule = (
        "- NEVER start a note with a heading that duplicates the filename. "
        "For example, if the file is 'Zelda.md', do NOT add '# Zelda' as the first line. "
        "Obsidian already displays the filename as the note title — a duplicate heading "
        "looks broken. Start the note body with frontmatter or directly with content."
    )

    if is_subtask:
        parts.extend([
            "- You are a sub-task agent — focus ONLY on the specific task described above.",
            "- Do your work using the available MCP tools (Slack, GitLab, Obsidian, etc.).",
            "- Do NOT modify, write to, or delete the task file — results are aggregated automatically.",
            f"- IMPORTANT: The file `{payload.task_file_path}` is managed by the dispatcher. Do not touch it.",
            heading_rule,
        ])
    else:
        parts.extend([
            "- Read and write files relative to /vault.",
            "- When creating new notes, include YAML frontmatter.",
            heading_rule,
            "- Use [[wiki-link]] syntax for cross-references where appropriate.",
            "- Write your results directly to the task file under a '## Results' section.",
        ])

    # Vault-specific custom instructions
    if vault_rules:
        custom = vault_rules.get("customInstructions", "")
        if custom:
            parts.extend(["", "## Vault Custom Instructions", "", custom])

        ops = vault_rules.get("allowedOperations", {})
        restrictions = []
        if not ops.get("createFiles", True):
            restrictions.append("- Do NOT create new files")
        if not ops.get("deleteFiles", True):
            restrictions.append("- Do NOT delete files")
        if not ops.get("modifyFiles", True):
            restrictions.append("- Do NOT modify existing files (read-only)")
        if not ops.get("runGitCommands", True):
            restrictions.append("- Do NOT run git commands")
        if restrictions:
            parts.extend(["", "## Operation Restrictions", ""] + restrictions)

        verbosity = vault_rules.get("verbosity", "normal")
        if verbosity == "concise":
            parts.extend(["", "- Keep your output concise and minimal."])
        elif verbosity == "detailed":
            parts.extend(["", "- Be thorough and detailed in your output."])

    return "\n".join(parts)
