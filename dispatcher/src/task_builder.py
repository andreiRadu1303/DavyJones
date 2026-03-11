from src.models import DispatchPayload, NoteMetadata


def build(
    task_file_path: str,
    metadata: NoteMetadata,
    content: str,
    context: str,
) -> DispatchPayload:
    """Assemble a DispatchPayload. The file body (content) is the prompt."""
    return DispatchPayload(
        task_file_path=task_file_path,
        prompt=content,
        context=context,
        metadata=metadata.model_dump(),
    )
