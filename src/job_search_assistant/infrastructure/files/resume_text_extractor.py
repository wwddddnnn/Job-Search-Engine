"""Minimal local-only text extraction adapter for S2 Career document import."""

from __future__ import annotations

from typing import Any, Mapping

from job_search_assistant.career.ports import (
    DocumentStoragePort,
    ExtractedResumeText,
    ResumeTextExtractionError,
)
from job_search_assistant.career.types import ResumeDocument
from job_search_assistant.core.errors import InfrastructureError, NotFoundError


_SUPPORTED_MIME_TYPES = frozenset({"text/plain", "text/markdown", "text/x-markdown"})


class PlainTextResumeExtractor:
    """Extract UTF-8 ``.txt`` and ``.md`` documents without third-party parsers.

    PDF, DOCX, and every other unsupported format fail with
    :class:`ResumeTextExtractionError`; this adapter never returns an empty
    string as a successful extraction.
    """

    version = "plain-text-v1"

    def __init__(self, storage: DocumentStoragePort) -> None:
        self._storage = storage

    def extract(self, *, document: ResumeDocument) -> ExtractedResumeText:
        """Read a controlled file and return its text plus a basic whole-file locator."""
        if document.mime_type not in _SUPPORTED_MIME_TYPES:
            raise ResumeTextExtractionError(
                "The resume format is not supported by the local text extractor.",
                reason="unsupported_format",
                details={"document_id": document.id, "mime_type": document.mime_type},
            )
        try:
            raw = self._storage.load_document(file_ref=document.file_ref)
        except ResumeTextExtractionError:
            raise
        except (InfrastructureError, NotFoundError):
            # A missing/corrupt controlled artifact is an infrastructure fault,
            # not a terminal statement about the resume's parseability.
            raise
        except Exception as exc:
            raise ResumeTextExtractionError(
                "The stored resume file could not be read for text extraction.",
                reason="document_unreadable",
                details={"document_id": document.id},
            ) from exc
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ResumeTextExtractionError(
                "The text resume is not valid UTF-8.",
                reason="invalid_text_encoding",
                details={"document_id": document.id, "encoding": "utf-8"},
            ) from exc
        if not text.strip():
            raise ResumeTextExtractionError(
                "The text resume contains no extractable text.",
                reason="empty_text",
                details={"document_id": document.id},
            )
        return ExtractedResumeText(
            text=text,
            extractor_version=self.version,
            locator_map=_whole_file_locator(text),
        )


def _whole_file_locator(text: str) -> Mapping[str, Any]:
    return {
        "format": "plain_text",
        "encoding": "utf-8",
        "spans": {"whole_document": {"start": 0, "end": len(text)}},
    }
