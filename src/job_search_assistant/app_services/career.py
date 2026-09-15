"""Application service for the S2 controlled resume-document import workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import mimetypes
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from job_search_assistant.career.ports import DocumentStoragePort, ResumeTextExtractorPort
from job_search_assistant.career.store import CareerStore
from job_search_assistant.career.types import DocumentStatus, ResumeDocument, ResumeText
from job_search_assistant.core.context import RequestContext
from job_search_assistant.core.errors import ApplicationError, ValidationError
from job_search_assistant.core.idempotency import IdempotencyReservationState


@dataclass(frozen=True, slots=True)
class ImportResumeDocumentResult:
    """Safe result returned by an import command or an idempotent replay."""

    document_id: str
    status: DocumentStatus

    @classmethod
    def from_document(cls, document: ResumeDocument) -> "ImportResumeDocumentResult":
        """Build the externally safe representation without exposing raw content references."""
        return cls(document_id=document.id, status=document.status)

    def to_dict(self) -> dict[str, str]:
        """Serialize the stable replay result used by adapters and tests."""
        return {"document_id": self.document_id, "status": self.status.value}


@dataclass(slots=True)
class ImportResumeDocument:
    """Import one local resume file, retain it, and record a terminal parse outcome.

    Source-file reads, controlled-file writes, and parser work occur outside
    SQLite transactions.  The store first records an ``Imported`` checkpoint
    with its idempotency reservation, then atomically records either extracted
    text or ``ExtractionFailed`` together with audit history and the replayable
    idempotency response.
    """

    store: CareerStore
    storage: DocumentStoragePort
    text_extractor: ResumeTextExtractorPort

    def execute(
        self,
        *,
        file_ref: Path | str,
        metadata: Mapping[str, Any],
        idempotency_key: str,
        context: RequestContext | None = None,
    ) -> ImportResumeDocumentResult:
        """Run the idempotent import command and return its document identifier/status."""
        request_context = context or RequestContext.create(source="career-import")
        source_path = _source_path(file_ref)
        normalized_metadata, mime_type = _normalize_metadata(metadata, source_path)
        normalized_key = _require_identifier(idempotency_key, "idempotency_key")

        # This local source read intentionally precedes any database transaction.
        content = _read_source_file(source_path)
        content_hash = sha256(content).hexdigest()
        stored_file_ref = self.storage.store_document(content=content, content_hash=content_hash)

        document = ResumeDocument(
            id=str(uuid4()),
            file_ref=stored_file_ref,
            mime_type=mime_type,
            content_hash=content_hash,
            uploaded_at=datetime.now(UTC),
            metadata=normalized_metadata,
        )
        request = {
            "file_ref": str(source_path),
            "content_hash": content_hash,
            "metadata": normalized_metadata,
        }
        reservation = self.store.reserve_resume_import(
            document=document,
            idempotency_key=normalized_key,
            request=request,
            context=request_context,
        )
        if reservation.state is IdempotencyReservationState.COMPLETED:
            return ImportResumeDocumentResult.from_document(reservation.document)

        imported = reservation.document
        terminal_document, extracted_text = self._extract_and_store(imported)
        persisted = self.store.finalize_resume_import(
            document=terminal_document,
            text=extracted_text,
            idempotency_record_id=reservation.idempotency_record_id,
            context=request_context,
        )
        return ImportResumeDocumentResult.from_document(persisted)

    def import_document(
        self,
        *,
        file_ref: Path | str,
        metadata: Mapping[str, Any],
        idempotency_key: str,
        context: RequestContext | None = None,
    ) -> ImportResumeDocumentResult:
        """Compatibility-friendly verb for callers that do not use command-style names."""
        return self.execute(
            file_ref=file_ref,
            metadata=metadata,
            idempotency_key=idempotency_key,
            context=context,
        )

    def _extract_and_store(
        self,
        document: ResumeDocument,
    ) -> tuple[ResumeDocument, ResumeText | None]:
        try:
            extracted = self.text_extractor.extract(document=document)
            text_hash = sha256(extracted.text.encode("utf-8")).hexdigest()
            text_ref = self.storage.store_text(
                text=extracted.text,
                content_hash=text_hash,
                locator_map=extracted.locator_map,
            )
            text = ResumeText(
                id=str(uuid4()),
                document_id=document.id,
                text_ref=text_ref,
                content_hash=text_hash,
                extractor_version=extracted.extractor_version,
                locator_map_ref=text_ref,
                created_at=datetime.now(UTC),
            )
            return (
                document.mark_text_extracted(
                    text_ref=text_ref,
                    extractor_version=extracted.extractor_version,
                ),
                text,
            )
        except Exception as exc:
            return document.mark_extraction_failed(error=_safe_extraction_error(exc)), None


def _source_path(file_ref: Path | str) -> Path:
    if isinstance(file_ref, Path):
        path = file_ref
    elif isinstance(file_ref, str) and file_ref.strip():
        path = Path(file_ref)
    else:
        raise ValidationError(
            "file_ref must be a non-blank local file path.",
            details={"field": "file_ref"},
        )
    if path.exists() and path.is_dir():
        raise ValidationError(
            "file_ref must point to a file, not a directory.",
            details={"field": "file_ref"},
        )
    return path


def _read_source_file(source_path: Path) -> bytes:
    try:
        return source_path.read_bytes()
    except FileNotFoundError as exc:
        raise ValidationError(
            "file_ref does not point to a readable local file.",
            details={"field": "file_ref", "file_ref": str(source_path)},
        ) from exc
    except OSError as exc:
        raise ValidationError(
            "file_ref does not point to a readable local file.",
            details={"field": "file_ref", "file_ref": str(source_path)},
        ) from exc


def _normalize_metadata(
    metadata: Mapping[str, Any],
    source_path: Path,
) -> tuple[dict[str, Any], str]:
    if not isinstance(metadata, Mapping):
        raise ValidationError("metadata must be an object.", details={"field": "metadata"})
    try:
        normalized = json.loads(
            json.dumps(
                _json_value(metadata),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "metadata must contain JSON-compatible values.",
            details={"field": "metadata", "reason": str(exc)},
        ) from exc
    if not isinstance(normalized, dict):
        raise ValidationError("metadata must be an object.", details={"field": "metadata"})
    declared_mime_type = normalized.get("mime_type")
    if declared_mime_type is None:
        mime_type = _mime_type_from_filename(source_path)
        normalized["mime_type"] = mime_type
    elif isinstance(declared_mime_type, str) and declared_mime_type.strip():
        mime_type = declared_mime_type.strip().lower()
        normalized["mime_type"] = mime_type
    else:
        raise ValidationError(
            "metadata.mime_type must be a non-blank string when provided.",
            details={"field": "metadata.mime_type"},
        )
    return normalized, mime_type


def _mime_type_from_filename(source_path: Path) -> str:
    suffix = source_path.suffix.lower()
    if suffix == ".md":
        return "text/markdown"
    if suffix == ".txt":
        return "text/plain"
    guessed, _ = mimetypes.guess_type(source_path.name)
    if guessed is None:
        raise ValidationError(
            "metadata.mime_type is required for an unrecognised file extension.",
            details={"field": "metadata.mime_type", "file_ref": str(source_path)},
        )
    return guessed.lower()


def _safe_extraction_error(exc: Exception) -> str:
    if isinstance(exc, ApplicationError):
        message = exc.message.strip() or "Text extraction failed."
        return f"{exc.code}: {message}"[:1000]
    return "unexpected_extraction_error: Text extraction did not complete."


def _require_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            f"{field_name} must be a non-blank string.",
            details={"field": field_name},
        )
    return value.strip()


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value
