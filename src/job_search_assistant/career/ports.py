"""Infrastructure-agnostic ports used by future Career application services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from job_search_assistant.career.types import ExtractionRun, ResumeDocument, ResumeText


@dataclass(frozen=True, slots=True)
class ExtractedResumeText:
    """Parser output before the application service stores it as :class:`ResumeText`."""

    text: str
    extractor_version: str
    locator_map: Mapping[str, Any] = field(default_factory=dict)


class ResumeTextExtractorPort(Protocol):
    """Extract text from an already stored resume without selecting storage itself."""

    def extract(self, *, document: ResumeDocument) -> ExtractedResumeText:
        """Return extracted text and source-location metadata for ``document``."""


class CareerExtractionPort(Protocol):
    """Obtain an untrusted JSON extraction draft for a persisted text extraction."""

    def extract(
        self,
        *,
        document: ResumeDocument,
        text: ResumeText,
        run: ExtractionRun,
    ) -> Mapping[str, Any]:
        """Return draft JSON; the caller must validate it before accepting output."""


class DocumentStoragePort(Protocol):
    """Controlled storage for original files and derived text outside SQLite blobs."""

    def store_document(self, *, content: bytes, content_hash: str) -> str:
        """Persist original document bytes and return a durable, opaque file reference."""

    def load_document(self, *, file_ref: str) -> bytes:
        """Load bytes for a previously returned document reference."""

    def store_text(self, *, text: str, content_hash: str) -> str:
        """Persist extracted text and return a durable, opaque text reference."""

    def load_text(self, *, text_ref: str) -> str:
        """Load text for a previously returned text reference."""
