"""Infrastructure-agnostic ports used by future Career application services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from job_search_assistant.career.types import ExtractionRun, ResumeDocument, ResumeText
from job_search_assistant.core.errors import ApplicationError, ValidationError


class ResumeTextExtractionError(ApplicationError):
    """A structured, terminal failure while turning a stored file into resume text."""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        error_details = {"reason": reason}
        if details:
            error_details.update(details)
        super().__init__("resume_text_extraction_failed", message, error_details)


@dataclass(frozen=True, slots=True)
class ExtractedResumeText:
    """Parser output before the application service stores it as :class:`ResumeText`."""

    text: str
    extractor_version: str
    locator_map: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValidationError(
                "Extracted resume text must be a non-blank string.",
                details={"field": "text"},
            )
        if not isinstance(self.extractor_version, str) or not self.extractor_version.strip():
            raise ValidationError(
                "extractor_version must be a non-blank string.",
                details={"field": "extractor_version"},
            )
        if not isinstance(self.locator_map, Mapping):
            raise ValidationError(
                "locator_map must be an object.",
                details={"field": "locator_map"},
            )


class ResumeTextExtractorPort(Protocol):
    """Extract text from an already stored resume without selecting storage itself."""

    def extract(self, *, document: ResumeDocument) -> ExtractedResumeText:
        """Return extracted text and source-location metadata for ``document``."""


class CareerExtractionPort(Protocol):
    """Obtain locator-backed, untrusted JSON facts from persisted resume text.

    Implementations receive the extracted text and immutable run metadata, and
    must only propose experience, achievement, and skill candidates with a
    source locator.  They must never label a result verified or confirmed.
    """

    def extract(
        self,
        *,
        extracted_text: str,
        run: ExtractionRun,
    ) -> Mapping[str, Any]:
        """Return draft JSON; the caller validates it before accepting output."""


class DocumentStoragePort(Protocol):
    """Controlled storage for original files and derived text outside SQLite blobs.

    An extracted-text reference addresses one immutable artifact containing both
    the UTF-8 text and its locator map.  ``locator_map_ref`` therefore uses the
    same value as ``text_ref``; implementations must preserve both values
    together and never overwrite an existing artifact for a content hash.
    """

    def store_document(self, *, content: bytes, content_hash: str) -> str:
        """Persist original document bytes and return a durable, opaque file reference."""

    def load_document(self, *, file_ref: str) -> bytes:
        """Load bytes for a previously returned document reference."""

    def store_text(
        self,
        *,
        text: str,
        content_hash: str,
        locator_map: Mapping[str, Any] | None = None,
    ) -> str:
        """Persist extracted text plus its locator map and return their shared reference."""

    def load_text(self, *, text_ref: str) -> str:
        """Load text for a previously returned text reference."""

    def load_locator_map(self, *, text_ref: str) -> Mapping[str, Any]:
        """Load locator metadata bundled with the extracted-text artifact."""

    def store_extraction_draft(self, *, draft: Mapping[str, Any]) -> str:
        """Persist one schema-validated draft and return an opaque immutable reference."""

    def load_extraction_draft(self, *, output_ref: str) -> Mapping[str, Any]:
        """Load a draft previously stored through :meth:`store_extraction_draft`."""
