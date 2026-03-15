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


def _is_ignored_path(rel_path: str) -> bool:
    """Check if a vault-relative path should be excluded from trigger expansion."""
    parts = rel_path.replace("\\", "/").split("/")
    for part in parts:
        if part.startswith(".") or part in (".obsidian", ".git", ".trash"):
            return True
    return False


def _list_md_in_dir(vault_root: str, dir_rel: str) -> list[str]:
    """List .md files in a vault-relative directory (non-recursive).

    Returns vault-relative paths. Skips dotfiles and ignored dirs.
    """
    abs_dir = os.path.join(vault_root, dir_rel) if dir_rel != "." else vault_root
    results = []
    try:
        for name in sorted(os.listdir(abs_dir)):
            if not name.endswith(".md") or name.startswith("."):
                continue
            full = os.path.join(abs_dir, name)
            if not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, vault_root)
            if not _is_ignored_path(rel):
                results.append(rel)
    except OSError:
        pass
    return results


def expand_trigger_files(
    vault_root: str,
    changed_files: list[str],
    triggers: list[str],
    depth: int = 1,
    max_files: int = 30,
) -> list[str]:
    """Expand the changed files set to include related files based on trigger config.

    Returns a deduplicated list of vault-relative paths including both the
    original changed files and the triggered files.
    """
    vault_root = os.path.normpath(vault_root)
    if not triggers:
        return list(changed_files)

    expanded = set(changed_files)
    frontier = set(changed_files)

    for _round in range(depth):
        new_files: set[str] = set()

        for rel_path in frontier:
            file_dir = os.path.dirname(rel_path)
            if not file_dir:
                file_dir = "."

            if "folder" in triggers:
                for f in _list_md_in_dir(vault_root, file_dir):
                    if f not in expanded:
                        new_files.add(f)

            if "parent" in triggers:
                parent_dir = os.path.dirname(file_dir) if file_dir != "." else None
                if parent_dir is not None:
                    if not parent_dir:
                        parent_dir = "."
                    for f in _list_md_in_dir(vault_root, parent_dir):
                        if f not in expanded:
                            new_files.add(f)

            if "children" in triggers:
                abs_dir = os.path.join(vault_root, file_dir) if file_dir != "." else vault_root
                try:
                    for entry in sorted(os.listdir(abs_dir)):
                        child_abs = os.path.join(abs_dir, entry)
                        if os.path.isdir(child_abs) and not entry.startswith("."):
                            child_rel = os.path.relpath(child_abs, vault_root)
                            if not _is_ignored_path(child_rel):
                                for f in _list_md_in_dir(vault_root, child_rel):
                                    if f not in expanded:
                                        new_files.add(f)
                except OSError:
                    pass

        if not new_files:
            break

        expanded.update(new_files)
        frontier = new_files

        if len(expanded) >= max_files:
            break

    # Cap and return sorted
    result = sorted(expanded)
    if len(result) > max_files:
        # Keep original changed files + fill up to max with triggered ones
        originals = sorted(changed_files)
        triggered = [f for f in result if f not in set(changed_files)]
        result = originals + triggered[:max_files - len(originals)]

    return result


def build_hierarchy_tree(
    vault_root: str,
    changed_files: list[str],
    all_files: list[str],
    depth: int = 2,
) -> str:
    """Build a visual tree of the folder structure around changed/triggered files.

    Marks changed files with '← changed', triggered files with '← triggered'.
    """
    vault_root = os.path.normpath(vault_root)
    changed_set = set(changed_files)
    all_set = set(all_files)

    # Collect all directories that contain relevant files, up to `depth` levels up
    dirs_to_show: set[str] = set()
    for rel_path in all_files:
        current = os.path.dirname(rel_path)
        for _ in range(depth + 1):
            if current:
                dirs_to_show.add(current)
                current = os.path.dirname(current)
            else:
                dirs_to_show.add(".")
                break

    # Build tree structure: dir -> (subdirs, files)
    tree: dict[str, dict] = {}
    for d in sorted(dirs_to_show):
        abs_d = os.path.join(vault_root, d) if d != "." else vault_root
        subdirs = []
        files = []
        try:
            for entry in sorted(os.listdir(abs_d)):
                if entry.startswith("."):
                    continue
                full = os.path.join(abs_d, entry)
                entry_rel = os.path.relpath(full, vault_root)
                if os.path.isdir(full) and entry_rel in dirs_to_show:
                    subdirs.append(entry)
                elif entry.endswith(".md") and os.path.isfile(full):
                    files.append(entry_rel)
        except OSError:
            pass
        tree[d] = {"subdirs": subdirs, "files": files}

    # Render tree recursively
    lines = ["## Hierarchy Tree", ""]

    def _render(dir_key: str, indent: int):
        node = tree.get(dir_key)
        if not node:
            return

        for fname_rel in node["files"]:
            name = os.path.basename(fname_rel)
            marker = ""
            if fname_rel in changed_set:
                marker = "  ← changed"
            elif fname_rel in all_set:
                marker = "  ← triggered"
            lines.append(f"{'  ' * indent}{name}{marker}")

        for subdir_name in node["subdirs"]:
            child_key = os.path.join(dir_key, subdir_name) if dir_key != "." else subdir_name
            lines.append(f"{'  ' * indent}{subdir_name}/")
            _render(child_key, indent + 1)

    # Start from root dirs (those with no parent in dirs_to_show)
    def _parent_in_tree(d):
        if d == ".":
            return False  # root is always a root
        parent = os.path.dirname(d)
        if parent in dirs_to_show:
            return True
        # Empty parent means root level — check if "." is in the tree
        if not parent and "." in dirs_to_show:
            return True
        return False

    root_dirs = sorted(d for d in dirs_to_show if not _parent_in_tree(d))

    for rd in root_dirs:
        if rd == ".":
            _render(".", 0)
        else:
            lines.append(f"{rd}/")
            _render(rd, 1)

    output = "\n".join(lines)
    # Cap output size
    if len(output) > 10000:
        output = output[:10000] + "\n... (truncated)"
    return output


def resolve_batch(
    vault_root: str,
    file_paths: list[str],
    triggers: list[str] | None = None,
    trigger_depth: int = 1,
    trigger_max_files: int = 30,
    hierarchy_depth: int = 2,
) -> str:
    """Build accumulated lightweight context for multiple files.

    Used by the overseer to get context for all changed files in a commit.
    Each file_path should be vault-relative.

    If triggers are configured, expands the file set to include related files.
    Appends a hierarchy tree showing folder structure around changed files.
    """
    vault_root = os.path.normpath(vault_root)

    # Expand file set if triggers configured
    if triggers:
        all_files = expand_trigger_files(
            vault_root, file_paths, triggers, trigger_depth, trigger_max_files,
        )
    else:
        all_files = list(file_paths)

    sections = []
    changed_set = set(file_paths)

    for rel_path in all_files:
        full_path = os.path.join(vault_root, rel_path)
        if not os.path.isfile(full_path):
            sections.append(f"## File: `{rel_path}` (deleted)")
            continue
        tag = ""
        if rel_path not in changed_set:
            tag = " (triggered)"
        section = resolve(vault_root, full_path)
        if tag:
            # Annotate the file header
            section = section.replace(
                f"## File: `{rel_path}`",
                f"## File: `{rel_path}` ← triggered",
                1,
            )
        sections.append(section)

    # Append hierarchy tree
    if hierarchy_depth > 0 and all_files:
        tree = build_hierarchy_tree(vault_root, file_paths, all_files, hierarchy_depth)
        sections.append(tree)

    return "\n---\n\n".join(sections)
