"""Career document import and draft-extraction application services."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import mimetypes
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from job_search_assistant.career.extraction import (
    EXTRACTION_DRAFT_SCHEMA_VERSION,
    ExtractionDraft,
    validate_extraction_draft,
)
from job_search_assistant.career.ports import (
    CareerExtractionPort,
    DocumentStoragePort,
    ResumeTextExtractionError,
    ResumeTextExtractorPort,
)
from job_search_assistant.career.store import (
    CareerStore,
    ExtractionRunReservation,
    ResumeImportReservation,
)
from job_search_assistant.career.types import (
    DocumentStatus,
    ExtractionRun,
    ExtractionRunStatus,
    ResumeDocument,
    ResumeText,
)
from job_search_assistant.core.context import RequestContext
from job_search_assistant.core.errors import ApplicationError, InfrastructureError, ValidationError
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

        request = {
            "file_ref": str(source_path),
            "metadata": normalized_metadata,
        }
        replay = self.store.peek_resume_import(
            idempotency_key=normalized_key,
            request=request,
            context=request_context,
        )
        if replay is not None:
            if replay.state is IdempotencyReservationState.COMPLETED:
                return ImportResumeDocumentResult.from_document(replay.document)
            return self._complete_import(reservation=replay, context=request_context)

        # This local source read intentionally precedes any database transaction
        # and occurs only after a completed replay has returned above.
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
        reservation = self.store.reserve_resume_import(
            document=document,
            idempotency_key=normalized_key,
            request=request,
            context=request_context,
        )
        if reservation.state is IdempotencyReservationState.COMPLETED:
            return ImportResumeDocumentResult.from_document(reservation.document)
        return self._complete_import(reservation=reservation, context=request_context)

    def _complete_import(
        self,
        *,
        reservation: ResumeImportReservation,
        context: RequestContext,
    ) -> ImportResumeDocumentResult:
        """Resume parser work from the durable document checkpoint for a key."""
        imported = reservation.document
        terminal_document, extracted_text = self._extract_and_store(imported)
        persisted = self.store.finalize_resume_import(
            document=terminal_document,
            text=extracted_text,
            idempotency_record_id=reservation.idempotency_record_id,
            context=context,
        )
        return ImportResumeDocumentResult.from_document(persisted)

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
        except (ResumeTextExtractionError, UnicodeError, ValidationError) as exc:
            return document.mark_extraction_failed(error=_safe_extraction_error(exc)), None


@dataclass(frozen=True, slots=True)
class StartExtractionRunResult:
    """Safe result returned by a draft extraction command or idempotent replay."""

    run_id: str
    status: ExtractionRunStatus

    @classmethod
    def from_run(cls, run: ExtractionRun) -> "StartExtractionRunResult":
        """Build the externally safe representation without exposing output references."""
        return cls(run_id=run.id, status=run.status)


@dataclass(slots=True)
class StartExtractionRun:
    """Start one append-only, schema-validated Career extraction draft.

    The durable ``draft_extracting`` run and its idempotency checkpoint are
    committed before calling the provider.  Loading controlled text, invoking
    the provider, and writing the immutable draft artifact all occur outside a
    SQLite transaction; a short final transaction records the terminal run
    state, audit event, and replay response.
    """

    store: CareerStore
    storage: DocumentStoragePort
    extraction_provider: CareerExtractionPort
    model: str = "deterministic-career-extraction-v1"
    prompt_version: str = "career-extraction-prompt-v1"
    schema_version: str = EXTRACTION_DRAFT_SCHEMA_VERSION

    def execute(
        self,
        *,
        document_id: str,
        idempotency_key: str,
        context: RequestContext | None = None,
    ) -> StartExtractionRunResult:
        """Run one idempotent draft extraction without granting verified status."""
        request_context = context or RequestContext.create(source="career-extraction")
        normalized_document_id = _require_identifier(document_id, "document_id")
        normalized_key = _require_identifier(idempotency_key, "idempotency_key")
        model = _require_identifier(self.model, "model")
        prompt_version = _require_identifier(self.prompt_version, "prompt_version")
        schema_version = _require_identifier(self.schema_version, "schema_version")
        request = {
            "document_id": normalized_document_id,
            "model": model,
            "prompt_version": prompt_version,
            "schema_version": schema_version,
        }

        replay = self.store.peek_extraction_run(
            idempotency_key=normalized_key,
            request=request,
            context=request_context,
        )
        if replay is not None and replay.state is IdempotencyReservationState.COMPLETED:
            return StartExtractionRunResult.from_run(replay.run)

        extracted_text: str | None = None
        if replay is None:
            document, extracted_text = self._load_extraction_input(normalized_document_id)
            run = document.start_extraction_run(
                run_id=str(uuid4()),
                input_hash=sha256(extracted_text.encode("utf-8")).hexdigest(),
                model=model,
                prompt_version=prompt_version,
                schema_version=schema_version,
                started_at=datetime.now(UTC),
            )
            reservation = self.store.reserve_extraction_run(
                run=run,
                idempotency_key=normalized_key,
                request=request,
                context=request_context,
            )
            if reservation.state is IdempotencyReservationState.COMPLETED:
                return StartExtractionRunResult.from_run(reservation.run)
        else:
            reservation = replay

        run = reservation.run
        if run.document_id != normalized_document_id:
            raise InfrastructureError(
                "Extraction idempotency checkpoint belongs to a different document.",
                details={"run_id": run.id, "document_id": run.document_id},
            ).with_correlation_id(request_context.correlation_id)
        if extracted_text is None:
            _, extracted_text = self._load_extraction_input(normalized_document_id)
        if sha256(extracted_text.encode("utf-8")).hexdigest() != run.input_hash:
            raise InfrastructureError(
                "Stored extraction input no longer matches the durable run input hash.",
                details={"run_id": run.id, "document_id": run.document_id},
            ).with_correlation_id(request_context.correlation_id)
        return self._extract_validate_store_and_finalize(
            run=run,
            extracted_text=extracted_text,
            idempotency_record_id=reservation.idempotency_record_id,
            context=request_context,
        )

    def _load_extraction_input(self, document_id: str) -> tuple[ResumeDocument, str]:
        """Read the durable text artifact without holding a SQLite transaction open."""
        document = self.store.get_resume_document(document_id=document_id)
        if (
            document.status is not DocumentStatus.TEXT_EXTRACTED
            or document.extracted_text_ref is None
        ):
            raise ValidationError(
                "A resume document must have extracted text before starting an extraction run.",
                details={"document_id": document.id, "status": document.status.value},
            )
        text = next(
            (
                item
                for item in self.store.list_resume_texts(document_id=document.id)
                if item.text_ref == document.extracted_text_ref
            ),
            None,
        )
        if text is None:
            raise InfrastructureError(
                "Resume document points to text that has no durable text record.",
                details={"document_id": document.id, "text_ref": document.extracted_text_ref},
            )
        return document, self.storage.load_text(text_ref=text.text_ref)

    def _extract_validate_store_and_finalize(
        self,
        *,
        run: ExtractionRun,
        extracted_text: str,
        idempotency_record_id: str,
        context: RequestContext,
    ) -> StartExtractionRunResult:
        """Call the provider outside transactions and durably record either outcome."""
        try:
            payload = self.extraction_provider.extract(extracted_text=extracted_text, run=run)
            draft = validate_extraction_draft(
                payload,
                expected_schema_version=run.schema_version,
            )
            _require_draft_locators(draft)
            output_ref = self.storage.store_extraction_draft(draft=_draft_to_payload(draft))
            completed = run.mark_draft_ready(output_ref=output_ref)
        except Exception as exc:
            failed = run.mark_draft_failed(
                error_summary=_extraction_error_summary(exc),
                completed_at=datetime.now(UTC),
            )
            self.store.finalize_extraction_run(
                run=failed,
                idempotency_record_id=idempotency_record_id,
                context=context,
            )
            if isinstance(exc, ApplicationError):
                raise exc.with_correlation_id(context.correlation_id)
            raise InfrastructureError(
                "Career extraction did not complete.",
                details={"run_id": run.id},
            ).with_correlation_id(context.correlation_id) from exc
        persisted = self.store.finalize_extraction_run(
            run=completed,
            idempotency_record_id=idempotency_record_id,
            context=context,
        )
        return StartExtractionRunResult.from_run(persisted)


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


def _require_draft_locators(draft: ExtractionDraft) -> None:
    """Require every provider-proposed fact to retain a source locator.

    The base draft schema allows an unsupported fact to be marked
    ``needs_clarification``.  The CareerExtractionPort contract for this slice
    is stricter: every fact it emits must still point back into the imported
    text so later user review can inspect the proposal's provenance.
    """
    for experience_index, experience in enumerate(draft.experiences):
        _require_item_locator(experience.evidence, f"experiences[{experience_index}]")
        for achievement_index, achievement in enumerate(experience.achievements):
            _require_item_locator(
                achievement.evidence,
                f"experiences[{experience_index}].achievements[{achievement_index}]",
            )
        for skill_index, skill in enumerate(experience.skills):
            _require_item_locator(
                skill.evidence,
                f"experiences[{experience_index}].skills[{skill_index}]",
            )


def _require_item_locator(evidence: tuple[Any, ...], location: str) -> None:
    if not any(getattr(item, "source_locator", None) for item in evidence):
        raise ValidationError(
            "Every extraction candidate requires a source locator.",
            details={"location": location, "field": "evidence.source_locator"},
        )


def _draft_to_payload(draft: ExtractionDraft) -> dict[str, Any]:
    """Serialize typed draft values, retaining only their unverified schema fields."""
    return asdict(draft)


def _extraction_error_summary(exc: Exception) -> dict[str, str]:
    """Store a bounded, adapter-safe reason while preserving the original exception to callers."""
    if isinstance(exc, ApplicationError):
        return {
            "code": exc.code,
            "message": (exc.message.strip() or "Career extraction failed.")[:1000],
        }
    return {
        "code": "unexpected_extraction_error",
        "message": "Career extraction did not complete.",
    }


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
