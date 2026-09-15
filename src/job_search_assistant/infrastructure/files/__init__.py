"""Filesystem adapters for controlled Career document storage and parsing."""

from job_search_assistant.infrastructure.files.document_storage import FileSystemDocumentStorage
from job_search_assistant.infrastructure.files.resume_text_extractor import PlainTextResumeExtractor

__all__ = ["FileSystemDocumentStorage", "PlainTextResumeExtractor"]
