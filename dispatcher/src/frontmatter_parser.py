import logging
from typing import Optional

import frontmatter

from src.models import NoteMetadata

logger = logging.getLogger(__name__)


def parse_note(file_path: str) -> tuple[NoteMetadata, str]:
    """Parse a markdown file's YAML frontmatter and body content.

    Returns (metadata, content_body).
    """
    post = frontmatter.load(file_path)
    metadata = NoteMetadata(**post.metadata) if post.metadata else NoteMetadata()
    return metadata, post.content


def is_actionable(metadata: NoteMetadata) -> bool:
    """Check if a note represents a pending task or job."""
    return metadata.type in ("task", "job") and metadata.status == "pending"


def check_dependencies_met(metadata: NoteMetadata, vault_path: str) -> bool:
    """Check if all depends_on tasks are completed."""
    if not metadata.depends_on:
        return True

    import os

    for dep_path in metadata.depends_on:
        full_path = os.path.join(vault_path, dep_path)
        if not os.path.isfile(full_path):
            logger.warning("Dependency not found: %s", dep_path)
            return False
        dep_meta, _ = parse_note(full_path)
        if dep_meta.status != "completed":
            logger.info("Dependency not yet completed: %s (status=%s)", dep_path, dep_meta.status)
            return False
    return True
