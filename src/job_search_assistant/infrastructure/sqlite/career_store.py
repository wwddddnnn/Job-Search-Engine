"""SQLite persistence for the S2 Career resume-import workflow."""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3
from typing import Any, Mapping, Sequence

from job_search_assistant.career.store import ExtractionRunReservation, ResumeImportReservation
from job_search_assistant.career.types import (
    DocumentStatus,
    ExtractionRun,
    ExtractionRunStatus,
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
from job_search_assistant.core.idempotency import (
    IdempotencyReservationState,
    IdempotencyStatus,
    hash_request,
)
from job_search_assistant.infrastructure.sqlite.audit_sink import SQLiteAuditSink
from job_search_assistant.infrastructure.sqlite.database import SQLiteDatabase
from job_search_assistant.infrastructure.sqlite.idempotency_store import SQLiteIdempotencyStore


_IMPORT_RESUME_SCOPE = "career.resume_document.import"
_START_EXTRACTION_SCOPE = "career.extraction_run.start"


class SQLiteCareerStore:
    """Own Career document/run persistence without holding locks during file I/O.

    ``reserve_resume_import`` writes the durable ``Imported`` checkpoint before
    parser work.  ``finalize_resume_import`` later appends text (when present),
    advances the document to its terminal parser state, records audit history,
    and completes the idempotency record in one SQLite transaction.
    """

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database
        self._audit = SQLiteAuditSink(database)
        self._idempotency = SQLiteIdempotencyStore(database)

    def peek_resume_import(
        self,
        *,
        idempotency_key: str,
        request: Mapping[str, Any],
        context: RequestContext,
    ) -> ResumeImportReservation | None:
        """Read an existing import checkpoint without reserving or mutating anything."""
        with self._database.connect() as connection:
            record = self._peek_idempotency_record(
                connection,
                scope=_IMPORT_RESUME_SCOPE,
                idempotency_key=idempotency_key,
                request=request,
                context=context,
            )
            if record is None:
                return None
            state, record_id, response = record
            return ResumeImportReservation(
                state=state,
                idempotency_record_id=record_id,
                document=self._load_resume_document(
                    connection,
                    _response_identifier(response, "document_id", record_id),
                ),
            )

    def reserve_resume_import(
        self,
        *,
        document: ResumeDocument,
        idempotency_key: str,
        request: Mapping[str, Any],
        context: RequestContext,
    ) -> ResumeImportReservation:
        """Reserve one import key and atomically save its original-file checkpoint.

        When the key is already ``IN_PROGRESS``, the original imported
        document is returned.  This deliberately avoids a second document row
        and lets a retry continue from the same controlled-file reference.
        """
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
                allow_in_progress=True,
            )
            if reservation.state is IdempotencyReservationState.COMPLETED:
                return ResumeImportReservation(
                    state=reservation.state,
                    idempotency_record_id=reservation.record_id,
                    document=self._load_resume_document(
                        connection,
                        _response_identifier(
                            reservation.response,
                            "document_id",
                            reservation.record_id,
                        ),
                    ),
                )
            if reservation.state is IdempotencyReservationState.IN_PROGRESS:
                return ResumeImportReservation(
                    state=reservation.state,
                    idempotency_record_id=reservation.record_id,
                    document=self._load_resume_document(
                        connection,
                        _response_identifier(
                            self._load_in_progress_checkpoint(connection, reservation.record_id),
                            "document_id",
                            reservation.record_id,
                        ),
                    ),
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
            self._store_in_progress_checkpoint(
                connection,
                record_id=reservation.record_id,
                response={"document_id": document.id},
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
                response=_document_result_response(document),
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

    def peek_extraction_run(
        self,
        *,
        idempotency_key: str,
        request: Mapping[str, Any],
        context: RequestContext,
    ) -> ExtractionRunReservation | None:
        """Read an existing extraction checkpoint without reserving or mutating it."""
        with self._database.connect() as connection:
            record = self._peek_idempotency_record(
                connection,
                scope=_START_EXTRACTION_SCOPE,
                idempotency_key=idempotency_key,
                request=request,
                context=context,
            )
            if record is None:
                return None
            state, record_id, response = record
            return ExtractionRunReservation(
                state=state,
                idempotency_record_id=record_id,
                run=self._load_extraction_run(
                    connection,
                    _response_identifier(response, "run_id", record_id),
                ),
            )

    def reserve_extraction_run(
        self,
        *,
        run: ExtractionRun,
        idempotency_key: str,
        request: Mapping[str, Any],
        context: RequestContext,
    ) -> ExtractionRunReservation:
        """Atomically append one run or resume the run checkpointed for an in-progress key."""
        _require_run_storage_consistency(run)
        if run.status is not ExtractionRunStatus.DRAFT_EXTRACTING:
            raise ValidationError(
                "A new extraction run must begin in draft_extracting status.",
                details={"run_id": run.id, "status": run.status.value},
            )
        request_hash = hash_request(request)
        normalized_key = _require_identifier(idempotency_key, "idempotency_key")
        with self._database.transaction(immediate=True) as connection:
            reservation = self._idempotency.reserve_in_transaction(
                connection,
                scope=_START_EXTRACTION_SCOPE,
                idempotency_key=normalized_key,
                request_hash=request_hash,
                context=context,
                allow_in_progress=True,
            )
            if reservation.state is IdempotencyReservationState.COMPLETED:
                return ExtractionRunReservation(
                    state=reservation.state,
                    idempotency_record_id=reservation.record_id,
                    run=self._load_extraction_run(
                        connection,
                        _response_identifier(reservation.response, "run_id", reservation.record_id),
                    ),
                )
            if reservation.state is IdempotencyReservationState.IN_PROGRESS:
                return ExtractionRunReservation(
                    state=reservation.state,
                    idempotency_record_id=reservation.record_id,
                    run=self._load_extraction_run(
                        connection,
                        _response_identifier(
                            self._load_in_progress_checkpoint(connection, reservation.record_id),
                            "run_id",
                            reservation.record_id,
                        ),
                    ),
                )

            self._load_resume_document(connection, run.document_id)
            self._insert_extraction_run(connection, run)
            self._audit.append_in_transaction(
                connection,
                AuditEvent.create(
                    context=context,
                    action="career.extraction_run.started",
                    target_type="llm_extraction_run",
                    target_id=run.id,
                    after=_safe_extraction_run(run),
                ),
            )
            self._store_in_progress_checkpoint(
                connection,
                record_id=reservation.record_id,
                response={"run_id": run.id},
            )
            return ExtractionRunReservation(
                state=reservation.state,
                idempotency_record_id=reservation.record_id,
                run=run,
            )

    def finalize_extraction_run(
        self,
        *,
        run: ExtractionRun,
        idempotency_record_id: str,
        context: RequestContext,
    ) -> ExtractionRun:
        """Persist one draft-ready or draft-failed terminal processing outcome."""
        _require_run_storage_consistency(run)
        if run.status not in {ExtractionRunStatus.DRAFT_READY, ExtractionRunStatus.DRAFT_FAILED}:
            raise ValidationError(
                "An extraction run can only be finalized as draft_ready or draft_failed.",
                details={"run_id": run.id, "status": run.status.value},
            )
        with self._database.transaction(immediate=True) as connection:
            before = self._load_extraction_run(connection, run.id)
            self._require_valid_extraction_run_transition(before=before, after=run)
            self._write_extraction_run(connection, run=run, previous_status=before.status)
            action = (
                "career.extraction_run.draft_ready"
                if run.status is ExtractionRunStatus.DRAFT_READY
                else "career.extraction_run.draft_failed"
            )
            self._audit.append_in_transaction(
                connection,
                AuditEvent.create(
                    context=context,
                    action=action,
                    target_type="llm_extraction_run",
                    target_id=run.id,
                    before=_safe_extraction_run(before),
                    after=_safe_extraction_run(run),
                ),
            )
            self._idempotency.complete_in_transaction(
                connection,
                record_id=_require_identifier(idempotency_record_id, "idempotency_record_id"),
                response=_extraction_run_result_response(run),
            )
        return run

    def create_extraction_run(self, *, run: ExtractionRun) -> ExtractionRun:
        """Append a standalone draft-extracting run without replacing earlier rows."""
        _require_run_storage_consistency(run)
        if run.status is not ExtractionRunStatus.DRAFT_EXTRACTING:
            raise ValidationError(
                "A new extraction run must begin in draft_extracting status.",
                details={"run_id": run.id, "status": run.status.value},
            )
        with self._database.transaction(immediate=True) as connection:
            self._load_resume_document(connection, run.document_id)
            self._insert_extraction_run(connection, run)
        return run

    def get_extraction_run(self, *, run_id: str) -> ExtractionRun:
        """Load one append-only extraction run."""
        with self._database.connect() as connection:
            return self._load_extraction_run(connection, _require_identifier(run_id, "run_id"))

    def update_extraction_run_status(self, *, run: ExtractionRun) -> ExtractionRun:
        """Persist an explicitly valid extraction-run state-machine transition."""
        _require_run_storage_consistency(run)
        with self._database.transaction(immediate=True) as connection:
            before = self._load_extraction_run(connection, run.id)
            self._require_valid_extraction_run_transition(before=before, after=run)
            self._write_extraction_run(connection, run=run, previous_status=before.status)
        return run

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
    def _insert_extraction_run(connection: sqlite3.Connection, run: ExtractionRun) -> None:
        try:
            connection.execute(
                """
                INSERT INTO llm_extraction_runs (
                    id, document_id, input_hash, model, prompt_version, schema_version,
                    status, output_ref, error_summary_json, published_profile_version_id,
                    started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.document_id,
                    run.input_hash,
                    run.model,
                    run.prompt_version,
                    run.schema_version,
                    run.status.value,
                    run.output_ref,
                    (
                        None
                        if run.error_summary is None
                        else _encode_mapping(run.error_summary, "error_summary")
                    ),
                    run.published_profile_version_id,
                    run.started_at.isoformat(),
                    None if run.completed_at is None else run.completed_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "An extraction run with this identifier already exists.",
                details={"run_id": run.id},
            ) from exc

    @staticmethod
    def _write_extraction_run(
        connection: sqlite3.Connection,
        *,
        run: ExtractionRun,
        previous_status: ExtractionRunStatus,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE llm_extraction_runs
            SET status = ?, output_ref = ?, error_summary_json = ?,
                published_profile_version_id = ?, completed_at = ?
            WHERE id = ? AND status = ?
            """,
            (
                run.status.value,
                run.output_ref,
                (
                    None
                    if run.error_summary is None
                    else _encode_mapping(run.error_summary, "error_summary")
                ),
                run.published_profile_version_id,
                None if run.completed_at is None else run.completed_at.isoformat(),
                run.id,
                previous_status.value,
            ),
        )
        if cursor.rowcount != 1:
            raise InfrastructureError(
                "Extraction run state could not be updated.",
                details={"run_id": run.id},
            )

    def _require_valid_extraction_run_transition(
        self,
        *,
        before: ExtractionRun,
        after: ExtractionRun,
    ) -> None:
        _require_same_extraction_run_identity(before, after)
        _require_run_storage_consistency(after)
        if after.status is ExtractionRunStatus.DRAFT_READY:
            expected = (
                before.return_to_draft()
                if before.status is ExtractionRunStatus.UNDER_REVIEW
                else before.mark_draft_ready(output_ref=after.output_ref or "")
            )
        elif after.status is ExtractionRunStatus.DRAFT_FAILED:
            expected = before.mark_draft_failed(
                error_summary=after.error_summary or {},
                completed_at=after.completed_at,
            )
        elif after.status is ExtractionRunStatus.UNDER_REVIEW:
            expected = before.begin_review()
        elif after.status is ExtractionRunStatus.PROFILE_VERSION_PUBLISHED:
            expected = before.publish_profile_version(
                profile_version_id=after.published_profile_version_id or "",
                completed_at=after.completed_at,
            )
        elif after.status is ExtractionRunStatus.DRAFT_EXTRACTING:
            # ``return_to_draft`` is the only legal way to return to ready; a
            # fresh extracting state always requires a new appended run.
            raise InvalidStateError(
                "extraction_run",
                before.id,
                before.status.value,
                "persist draft_extracting status",
            )
        else:
            raise ValidationError(
                "Extraction run status is unsupported.",
                details={"run_id": after.id, "status": after.status.value},
            )
        if expected != after:
            raise ValidationError(
                "Extraction run transition changed immutable fields or inconsistent "
                "terminal fields.",
                details={"run_id": after.id},
            )

    def _peek_idempotency_record(
        self,
        connection: sqlite3.Connection,
        *,
        scope: str,
        idempotency_key: str,
        request: Mapping[str, Any],
        context: RequestContext,
    ) -> tuple[IdempotencyReservationState, str, Mapping[str, Any] | None] | None:
        normalized_key = _require_identifier(idempotency_key, "idempotency_key")
        request_hash = hash_request(request)
        row = connection.execute(
            """
            SELECT id, request_hash, status, response_json, error_code, correlation_id
            FROM idempotency_records
            WHERE scope = ? AND idempotency_key = ?
            """,
            (scope, normalized_key),
        ).fetchone()
        if row is None:
            return None
        if str(row["request_hash"]) != request_hash:
            raise ConflictError(
                "An idempotency key was reused with a different request payload.",
                details={
                    "scope": scope,
                    "idempotency_key": normalized_key,
                    "existing_correlation_id": str(row["correlation_id"]),
                },
            ).with_correlation_id(context.correlation_id)
        status = IdempotencyStatus(str(row["status"]))
        record_id = str(row["id"])
        if status is IdempotencyStatus.COMPLETED:
            return (
                IdempotencyReservationState.COMPLETED,
                record_id,
                _decode_idempotency_response(row["response_json"], record_id),
            )
        if status is IdempotencyStatus.IN_PROGRESS:
            return (
                IdempotencyReservationState.IN_PROGRESS,
                record_id,
                _decode_idempotency_response(row["response_json"], record_id),
            )
        raise ConflictError(
            "The prior request for this idempotency key failed; use a new key to retry.",
            details={
                "scope": scope,
                "idempotency_key": normalized_key,
                "record_id": record_id,
                "error_code": row["error_code"],
            },
        ).with_correlation_id(context.correlation_id)

    @staticmethod
    def _load_in_progress_checkpoint(
        connection: sqlite3.Connection,
        record_id: str,
    ) -> Mapping[str, Any]:
        row = connection.execute(
            "SELECT response_json FROM idempotency_records WHERE id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            raise InfrastructureError(
                "In-progress idempotency record could not be read.",
                details={"record_id": record_id},
            )
        return _decode_idempotency_response(row["response_json"], record_id)

    @staticmethod
    def _store_in_progress_checkpoint(
        connection: sqlite3.Connection,
        *,
        record_id: str,
        response: Mapping[str, Any],
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE idempotency_records
            SET response_json = ?
            WHERE id = ? AND status = ?
            """,
            (
                _encode_mapping(response, "idempotency_checkpoint"),
                record_id,
                IdempotencyStatus.IN_PROGRESS.value,
            ),
        )
        if cursor.rowcount != 1:
            raise InfrastructureError(
                "Unable to store an in-progress idempotency checkpoint.",
                details={"record_id": record_id},
            )

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

    @staticmethod
    def _load_extraction_run(connection: sqlite3.Connection, run_id: str) -> ExtractionRun:
        row = connection.execute(
            """
            SELECT id, document_id, input_hash, model, prompt_version, schema_version,
                   status, output_ref, error_summary_json, published_profile_version_id,
                   started_at, completed_at
            FROM llm_extraction_runs
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("extraction_run", run_id)
        return _row_to_extraction_run(row)


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


def _row_to_extraction_run(row: sqlite3.Row) -> ExtractionRun:
    error_summary = (
        None
        if row["error_summary_json"] is None
        else _decode_mapping(row["error_summary_json"], "error_summary_json")
    )
    return ExtractionRun(
        id=str(row["id"]),
        document_id=str(row["document_id"]),
        input_hash=str(row["input_hash"]),
        model=str(row["model"]),
        prompt_version=str(row["prompt_version"]),
        schema_version=str(row["schema_version"]),
        status=ExtractionRunStatus(str(row["status"])),
        output_ref=_optional_text(row["output_ref"]),
        error_summary=error_summary,
        published_profile_version_id=_optional_text(row["published_profile_version_id"]),
        started_at=_parse_datetime(row["started_at"], "started_at"),
        completed_at=(
            None
            if row["completed_at"] is None
            else _parse_datetime(row["completed_at"], "completed_at")
        ),
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


def _safe_extraction_run(run: ExtractionRun) -> dict[str, Any]:
    """Return auditable run metadata without embedding raw draft output or resume text."""
    return {
        "run_id": run.id,
        "document_id": run.document_id,
        "input_hash": run.input_hash,
        "model": run.model,
        "prompt_version": run.prompt_version,
        "schema_version": run.schema_version,
        "status": run.status.value,
        "has_output": run.output_ref is not None,
        "error_code": None if run.error_summary is None else run.error_summary.get("code"),
    }


def _document_result_response(document: ResumeDocument) -> dict[str, str]:
    """Return the single durable idempotency response shape for an import result."""
    return {"document_id": document.id, "status": document.status.value}


def _extraction_run_result_response(run: ExtractionRun) -> dict[str, str]:
    """Return the durable idempotency response shape for an extraction result."""
    return {"run_id": run.id, "status": run.status.value}


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


def _require_same_extraction_run_identity(before: ExtractionRun, after: ExtractionRun) -> None:
    immutable = (
        "id",
        "document_id",
        "input_hash",
        "model",
        "prompt_version",
        "schema_version",
        "started_at",
    )
    changed = [field for field in immutable if getattr(before, field) != getattr(after, field)]
    if changed:
        raise ValidationError(
            "An extraction run's immutable fields cannot change.",
            details={"run_id": before.id, "fields": changed},
        )


def _require_run_storage_consistency(run: ExtractionRun) -> None:
    """Defend the schema's status/terminal-field invariant at the store boundary.

    SQLite 0004 intentionally has no equivalent CHECK, so this guard is kept
    explicit even though :class:`ExtractionRun` validates the normal domain
    construction path.
    """
    status = run.status
    output_required = status in {
        ExtractionRunStatus.DRAFT_READY,
        ExtractionRunStatus.UNDER_REVIEW,
        ExtractionRunStatus.PROFILE_VERSION_PUBLISHED,
    }
    completed_required = status in {
        ExtractionRunStatus.DRAFT_FAILED,
        ExtractionRunStatus.PROFILE_VERSION_PUBLISHED,
    }
    error_required = status is ExtractionRunStatus.DRAFT_FAILED
    published_required = status is ExtractionRunStatus.PROFILE_VERSION_PUBLISHED
    invalid = (
        (output_required != (run.output_ref is not None))
        or (completed_required != (run.completed_at is not None))
        or (error_required != (run.error_summary is not None))
        or (published_required != (run.published_profile_version_id is not None))
    )
    if invalid:
        raise ValidationError(
            "Extraction run status is inconsistent with its output, completion, error, "
            "or publication fields.",
            details={"run_id": run.id, "status": status.value},
        )


def _response_identifier(
    response: Mapping[str, Any] | None,
    field_name: str,
    record_id: str,
) -> str:
    value = None if response is None else response.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise InfrastructureError(
            "Idempotency record did not contain its durable work identifier.",
            details={"record_id": record_id, "field": field_name},
        )
    return value.strip()


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


def _decode_idempotency_response(value: Any, record_id: str) -> Mapping[str, Any]:
    if value is None:
        raise InfrastructureError(
            "Idempotency record did not contain its durable work checkpoint.",
            details={"record_id": record_id},
        )
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise InfrastructureError(
            "Idempotency work checkpoint is invalid JSON.",
            details={"record_id": record_id},
        ) from exc
    if not isinstance(decoded, Mapping):
        raise InfrastructureError(
            "Idempotency work checkpoint is not an object.",
            details={"record_id": record_id},
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
