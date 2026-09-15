"""SQLite persistence for the S2 Career resume-import workflow."""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3
from typing import Any, Mapping, Sequence

from job_search_assistant.career.store import ResumeImportReservation
from job_search_assistant.career.types import (
    DocumentStatus,
    ResumeDocument,
    ResumeText,
    Skill,
)
from job_search_assistant.core.audit import AuditEvent
from job_search_assistant.core.context import RequestContext
from job_search_assistant.core.errors import (
    ConflictError,
    InfrastructureError,
    InvalidStateError,
    NotFoundError,
    ValidationError,
)
from job_search_assistant.core.idempotency import IdempotencyReservationState, hash_request
from job_search_assistant.infrastructure.sqlite.audit_sink import SQLiteAuditSink
from job_search_assistant.infrastructure.sqlite.database import SQLiteDatabase
from job_search_assistant.infrastructure.sqlite.idempotency_store import SQLiteIdempotencyStore


_IMPORT_RESUME_SCOPE = "career.resume_document.import"


class SQLiteCareerStore:
    """Own Career document/text persistence without holding locks during file I/O.

    ``reserve_resume_import`` writes the durable ``Imported`` checkpoint before
    parser work.  ``finalize_resume_import`` later appends text (when present),
    advances the document to its terminal parser state, records audit history,
    and completes the idempotency record in one SQLite transaction.
    """

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database
        self._audit = SQLiteAuditSink(database)
        self._idempotency = SQLiteIdempotencyStore(database)

    def reserve_resume_import(
        self,
        *,
        document: ResumeDocument,
        idempotency_key: str,
        request: Mapping[str, Any],
        context: RequestContext,
    ) -> ResumeImportReservation:
        """Reserve one import key and atomically save its original-file checkpoint."""
        if document.status is not DocumentStatus.IMPORTED:
            raise ValidationError(
                "A new resume import must begin in imported status.",
                details={"document_id": document.id, "status": document.status.value},
            )
        request_hash = hash_request(request)
        normalized_key = _require_identifier(idempotency_key, "idempotency_key")
        with self._database.transaction(immediate=True) as connection:
            reservation = self._idempotency.reserve_in_transaction(
                connection,
                scope=_IMPORT_RESUME_SCOPE,
                idempotency_key=normalized_key,
                request_hash=request_hash,
                context=context,
            )
            if reservation.state is IdempotencyReservationState.COMPLETED:
                response = reservation.response or {}
                document_id = response.get("document_id")
                if not isinstance(document_id, str):
                    raise InfrastructureError(
                        "Completed resume import did not contain a document identifier.",
                        details={"record_id": reservation.record_id},
                    )
                return ResumeImportReservation(
                    state=reservation.state,
                    idempotency_record_id=reservation.record_id,
                    document=self._load_resume_document(connection, document_id),
                )

            self._insert_resume_document(connection, document)
            self._audit.append_in_transaction(
                connection,
                AuditEvent.create(
                    context=context,
                    action="career.resume_document.imported",
                    target_type="resume_document",
                    target_id=document.id,
                    after=_safe_document(document),
                ),
            )
            return ResumeImportReservation(
                state=reservation.state,
                idempotency_record_id=reservation.record_id,
                document=document,
            )

    def finalize_resume_import(
        self,
        *,
        document: ResumeDocument,
        text: ResumeText | None,
        idempotency_record_id: str,
        context: RequestContext,
    ) -> ResumeDocument:
        """Commit the terminal parser result, its audit event, and idempotent response."""
        if document.status is DocumentStatus.TEXT_EXTRACTED:
            if text is None:
                raise ValidationError(
                    "A text-extracted document requires a ResumeText result.",
                    details={"document_id": document.id},
                )
            if text.document_id != document.id or text.text_ref != document.extracted_text_ref:
                raise ValidationError(
                    "Resume text must belong to the document and match its stored reference.",
                    details={"document_id": document.id, "text_id": text.id},
                )
        elif document.status is DocumentStatus.EXTRACTION_FAILED:
            if text is not None:
                raise ValidationError(
                    "A failed extraction cannot persist text output.",
                    details={"document_id": document.id},
                )
        else:
            raise ValidationError(
                "Resume import can only be finalized in a terminal parser state.",
                details={"document_id": document.id, "status": document.status.value},
            )

        with self._database.transaction(immediate=True) as connection:
            before = self._load_resume_document(connection, document.id)
            if before.status is not DocumentStatus.IMPORTED:
                raise InvalidStateError(
                    "resume_document",
                    document.id,
                    before.status.value,
                    "finalize resume import",
                )
            if text is not None:
                self._insert_resume_text(connection, text)
            cursor = connection.execute(
                """
                UPDATE resume_documents
                SET status = ?, parser_version = ?, extracted_text_ref = ?, extraction_error = ?
                WHERE id = ? AND status = ?
                """,
                (
                    document.status.value,
                    document.parser_version,
                    document.extracted_text_ref,
                    document.extraction_error,
                    document.id,
                    DocumentStatus.IMPORTED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise InfrastructureError(
                    "Resume document parser state could not be updated.",
                    details={"document_id": document.id},
                )
            action = (
                "career.resume_document.text_extracted"
                if document.status is DocumentStatus.TEXT_EXTRACTED
                else "career.resume_document.extraction_failed"
            )
            self._audit.append_in_transaction(
                connection,
                AuditEvent.create(
                    context=context,
                    action=action,
                    target_type="resume_document",
                    target_id=document.id,
                    before=_safe_document(before),
                    after=_safe_document(document),
                ),
            )
            self._idempotency.complete_in_transaction(
                connection,
                record_id=_require_identifier(idempotency_record_id, "idempotency_record_id"),
                response={"document_id": document.id, "status": document.status.value},
            )
        return document

    def create_resume_document(self, *, document: ResumeDocument) -> ResumeDocument:
        """Persist a document directly for lower-level repository use."""
        with self._database.transaction(immediate=True) as connection:
            self._insert_resume_document(connection, document)
        return document

    def get_resume_document(self, *, document_id: str) -> ResumeDocument:
        """Load one persisted resume document."""
        with self._database.connect() as connection:
            return self._load_resume_document(
                connection,
                _require_identifier(document_id, "document_id"),
            )

    def update_resume_document_status(self, *, document: ResumeDocument) -> ResumeDocument:
        """Persist only a legal one-way parser transition from ``Imported``."""
        with self._database.transaction(immediate=True) as connection:
            before = self._load_resume_document(connection, document.id)
            _require_same_document_identity(before, document)
            if document.status is DocumentStatus.TEXT_EXTRACTED:
                expected = before.mark_text_extracted(
                    text_ref=document.extracted_text_ref or "",
                    extractor_version=document.parser_version or "",
                )
            elif document.status is DocumentStatus.EXTRACTION_FAILED:
                expected = before.mark_extraction_failed(error=document.extraction_error or "")
            else:
                raise InvalidStateError(
                    "resume_document",
                    before.id,
                    before.status.value,
                    "persist imported status",
                )
            if expected != document:
                raise ValidationError(
                    "Resume document status transition changed immutable fields.",
                    details={"document_id": document.id},
                )
            cursor = connection.execute(
                """
                UPDATE resume_documents
                SET status = ?, parser_version = ?, extracted_text_ref = ?, extraction_error = ?
                WHERE id = ? AND status = ?
                """,
                (
                    document.status.value,
                    document.parser_version,
                    document.extracted_text_ref,
                    document.extraction_error,
                    document.id,
                    DocumentStatus.IMPORTED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise InfrastructureError(
                    "Resume document parser state could not be updated.",
                    details={"document_id": document.id},
                )
        return document

    def append_resume_text(self, *, text: ResumeText) -> ResumeText:
        """Append one immutable text extraction without replacing prior rows."""
        with self._database.transaction(immediate=True) as connection:
            self._load_resume_document(connection, text.document_id)
            self._insert_resume_text(connection, text)
        return text

    def list_resume_texts(self, *, document_id: str) -> Sequence[ResumeText]:
        """List one document's retained text results in creation order."""
        normalized_document_id = _require_identifier(document_id, "document_id")
        with self._database.connect() as connection:
            self._load_resume_document(connection, normalized_document_id)
            rows = connection.execute(
                """
                SELECT id, document_id, text_ref, content_hash, extractor_version,
                       locator_map_ref, created_at
                FROM document_texts
                WHERE document_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (normalized_document_id,),
            ).fetchall()
        return [_row_to_resume_text(row) for row in rows]

    def create_skill(self, *, skill: Skill) -> Skill:
        """Persist a skill, explicitly mapping legacy ``None`` taxonomy values to ``''``."""
        taxonomy_ref = "" if skill.taxonomy_ref is None else skill.taxonomy_ref
        with self._database.transaction(immediate=True) as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO skills (id, canonical_name, taxonomy_ref, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (skill.id, skill.canonical_name, taxonomy_ref, skill.created_at.isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    "A normalized skill with this canonical name and taxonomy already exists.",
                    details={
                        "canonical_name": skill.canonical_name,
                        "taxonomy_ref": taxonomy_ref,
                    },
                ) from exc
        return skill

    def _insert_resume_document(
        self,
        connection: sqlite3.Connection,
        document: ResumeDocument,
    ) -> None:
        try:
            connection.execute(
                """
                INSERT INTO resume_documents (
                    id, file_ref, mime_type, content_hash, status, metadata_json, uploaded_at,
                    parser_version, extracted_text_ref, extraction_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document.id,
                    document.file_ref,
                    document.mime_type,
                    document.content_hash,
                    document.status.value,
                    _encode_mapping(document.metadata, "metadata"),
                    document.uploaded_at.isoformat(),
                    document.parser_version,
                    document.extracted_text_ref,
                    document.extraction_error,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "A resume document with this identifier already exists.",
                details={"document_id": document.id},
            ) from exc

    @staticmethod
    def _insert_resume_text(connection: sqlite3.Connection, text: ResumeText) -> None:
        try:
            connection.execute(
                """
                INSERT INTO document_texts (
                    id, document_id, text_ref, content_hash, extractor_version,
                    locator_map_ref, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    text.id,
                    text.document_id,
                    text.text_ref,
                    text.content_hash,
                    text.extractor_version,
                    text.locator_map_ref,
                    text.created_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "A resume text result with this identifier already exists.",
                details={"resume_text_id": text.id},
            ) from exc

    @staticmethod
    def _load_resume_document(connection: sqlite3.Connection, document_id: str) -> ResumeDocument:
        row = connection.execute(
            """
            SELECT id, file_ref, mime_type, content_hash, status, metadata_json, uploaded_at,
                   parser_version, extracted_text_ref, extraction_error
            FROM resume_documents
            WHERE id = ?
            """,
            (document_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("resume_document", document_id)
        return _row_to_resume_document(row)


def _row_to_resume_document(row: sqlite3.Row) -> ResumeDocument:
    return ResumeDocument(
        id=str(row["id"]),
        file_ref=str(row["file_ref"]),
        mime_type=str(row["mime_type"]),
        content_hash=str(row["content_hash"]),
        status=DocumentStatus(str(row["status"])),
        metadata=_decode_mapping(row["metadata_json"], "metadata_json"),
        uploaded_at=_parse_datetime(row["uploaded_at"], "uploaded_at"),
        parser_version=_optional_text(row["parser_version"]),
        extracted_text_ref=_optional_text(row["extracted_text_ref"]),
        extraction_error=_optional_text(row["extraction_error"]),
    )


def _row_to_resume_text(row: sqlite3.Row) -> ResumeText:
    return ResumeText(
        id=str(row["id"]),
        document_id=str(row["document_id"]),
        text_ref=str(row["text_ref"]),
        content_hash=str(row["content_hash"]),
        extractor_version=str(row["extractor_version"]),
        locator_map_ref=_optional_text(row["locator_map_ref"]),
        created_at=_parse_datetime(row["created_at"], "created_at"),
    )


def _safe_document(document: ResumeDocument) -> dict[str, Any]:
    """Return auditable metadata without embedding raw document bytes or text."""
    return {
        "document_id": document.id,
        "content_hash": document.content_hash,
        "mime_type": document.mime_type,
        "status": document.status.value,
        "parser_version": document.parser_version,
        "has_extracted_text": document.extracted_text_ref is not None,
        "extraction_error": document.extraction_error,
    }


def _require_same_document_identity(before: ResumeDocument, after: ResumeDocument) -> None:
    immutable = (
        "id",
        "file_ref",
        "mime_type",
        "content_hash",
        "uploaded_at",
        "metadata",
    )
    changed = [field for field in immutable if getattr(before, field) != getattr(after, field)]
    if changed:
        raise ValidationError(
            "A resume document's immutable import fields cannot change.",
            details={"document_id": before.id, "fields": changed},
        )


def _require_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            f"{field_name} must be a non-blank string.",
            details={"field": field_name},
        )
    return value.strip()


def _encode_mapping(value: Mapping[str, Any], field_name: str) -> str:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field_name} must be an object.", details={"field": field_name})
    try:
        return json.dumps(
            _json_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"{field_name} must contain JSON-compatible values.",
            details={"field": field_name, "reason": str(exc)},
        ) from exc


def _decode_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise InfrastructureError(
            "Stored resume document metadata is invalid JSON.",
            details={"field": field_name},
        ) from exc
    if not isinstance(decoded, Mapping):
        raise InfrastructureError(
            "Stored resume document metadata is not an object.",
            details={"field": field_name},
        )
    return decoded


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _parse_datetime(value: Any, field_name: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise InfrastructureError(
            "Stored Career timestamp is invalid.",
            details={"field": field_name, "value": str(value)},
        ) from exc


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)
