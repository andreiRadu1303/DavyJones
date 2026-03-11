"""Lightweight context resolver for DavyJones.

A file's context consists of:
  1. Its own full text (frontmatter + body)
  2. Links to ancestor files (vault-relative paths only, not their content)
  3. Links to sibling files (vault-relative paths only, not their content)
  4. Wiki-links in the file body (naturally included via the full text)
"""

import logging
import os

import frontmatter

logger = logging.getLogger(__name__)


def _read_md_file(path: str) -> str:
    """Read a markdown file and return 'filename + frontmatter + body' as text."""
    try:
        post = frontmatter.load(path)
        parts = [f"### `{os.path.basename(path)}`"]
        # Include frontmatter fields as context (except internal ones like status)
        if post.metadata:
            meta_lines = []
            for k, v in post.metadata.items():
                if k in ("status", "completed_at", "error_message"):
                    continue
                meta_lines.append(f"- **{k}**: {v}")
            if meta_lines:
                parts.append("\n".join(meta_lines))
        if post.content.strip():
            parts.append(post.content.strip())
        return "\n".join(parts)
    except Exception:
        logger.exception("Failed to read %s", path)
        return ""


def _list_md_files(dir_path: str, exclude: str = "") -> list[str]:
    """List .md files in a directory (non-recursive), excluding a given path."""
    exclude = os.path.normpath(exclude) if exclude else ""
    files = []
    try:
        for name in sorted(os.listdir(dir_path)):
            full = os.path.join(dir_path, name)
            if not name.endswith(".md") or not os.path.isfile(full):
                continue
            if os.path.normpath(full) == exclude:
                continue
            files.append(full)
    except OSError:
        pass
    return files


def _to_vault_relative(vault_root: str, full_path: str) -> str:
    """Convert an absolute path to a vault-relative path."""
    return os.path.relpath(full_path, vault_root)


def _get_ancestor_paths(vault_root: str, task_file_path: str) -> list[str]:
    """Get vault-relative paths of all .md files in ancestor directories.

    Walks from vault root down to (but excluding) the task file's own directory.
    Returns paths root-first.
    """
    vault_root = os.path.normpath(vault_root)
    task_dir = os.path.normpath(os.path.dirname(task_file_path))

    # Build hierarchy from task dir up to vault root
    hierarchy = []
    current = task_dir
    while True:
        hierarchy.append(current)
        if os.path.normpath(current) == vault_root:
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    # Reverse to get root-first, and exclude the task's own directory
    hierarchy.reverse()
    ancestor_dirs = hierarchy[:-1] if len(hierarchy) > 1 else []

    paths = []
    for dir_path in ancestor_dirs:
        for full in _list_md_files(dir_path):
            paths.append(_to_vault_relative(vault_root, full))
    return paths


def _get_sibling_paths(vault_root: str, task_file_path: str) -> list[str]:
    """Get vault-relative paths of sibling .md files in the same directory."""
    task_dir = os.path.dirname(task_file_path)
    siblings = _list_md_files(task_dir, exclude=task_file_path)
    return [_to_vault_relative(vault_root, s) for s in siblings]


def resolve(vault_root: str, task_file_path: str) -> str:
    """Build a lightweight context string for a single file.

    Returns:
      - Full text of the task file itself
      - Vault-relative paths of ancestor files (not their content)
      - Vault-relative paths of sibling files (not their content)
    """
    vault_root = os.path.normpath(vault_root)
    task_file_path = os.path.normpath(task_file_path)
    rel_path = _to_vault_relative(vault_root, task_file_path)

    parts = [f"## File: `{rel_path}`", ""]

    # Full text of the file itself
    file_text = _read_md_file(task_file_path)
    if file_text:
        parts.append(file_text)
        parts.append("")

    # Ancestor paths
    ancestors = _get_ancestor_paths(vault_root, task_file_path)
    if ancestors:
        parts.append("## Ancestors")
        for a in ancestors:
            parts.append(f"- `{a}`")
        parts.append("")

    # Sibling paths
    siblings = _get_sibling_paths(vault_root, task_file_path)
    if siblings:
        parts.append("## Siblings")
        for s in siblings:
            parts.append(f"- `{s}`")
        parts.append("")

    return "\n".join(parts)


def resolve_batch(vault_root: str, file_paths: list[str]) -> str:
    """Build accumulated lightweight context for multiple files.

    Used by the overseer to get context for all changed files in a commit.
    Each file_path should be vault-relative.
    """
    vault_root = os.path.normpath(vault_root)
    sections = []

    for rel_path in file_paths:
        full_path = os.path.join(vault_root, rel_path)
        if not os.path.isfile(full_path):
            # File was deleted in this commit — just note the path
            sections.append(f"## File: `{rel_path}` (deleted)")
            continue
        sections.append(resolve(vault_root, full_path))

    return "\n---\n\n".join(sections)
