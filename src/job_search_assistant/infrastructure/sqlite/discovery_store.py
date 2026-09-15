"""SQLite persistence for the Phase 1 Job Discovery workflow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import sqlite3
from typing import Any, Mapping, Sequence
from uuid import uuid4

from job_search_assistant.core.audit import AuditEvent, AuditOutcome
from job_search_assistant.core.context import RequestContext
from job_search_assistant.core.errors import (
    ConflictError,
    InfrastructureError,
    InvalidStateError,
    NotFoundError,
    ValidationError,
)
from job_search_assistant.core.idempotency import IdempotencyReservationState, hash_request
from job_search_assistant.discovery.store import (
    NormalizationOutcome,
    RunClaim,
    RunResumeState,
    SearchRun,
    SearchRunReservation,
    StoredSearchConfig,
)
from job_search_assistant.discovery.types import ProviderSearchPage, SearchConfig, SearchRunStatus
from job_search_assistant.infrastructure.sqlite.audit_sink import SQLiteAuditSink
from job_search_assistant.infrastructure.sqlite.database import SQLiteDatabase
from job_search_assistant.infrastructure.sqlite.idempotency_store import SQLiteIdempotencyStore


_SAVE_CONFIG_SCOPE = "discovery.search_config.save"
_START_SEARCH_SCOPE = "discovery.search_run.start"
_TERMINAL_STATUSES = {
    SearchRunStatus.SUCCEEDED,
    SearchRunStatus.PARTIALLY_SUCCEEDED,
    SearchRunStatus.FAILED,
    SearchRunStatus.CANCELLED,
}


class SQLiteDiscoveryStore:
    """Owns transactional persistence for discovery commands and checkpoints."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database
        self._idempotency = SQLiteIdempotencyStore(database)
        self._audit = SQLiteAuditSink(database)

    def save_search_config(
        self,
        *,
        config: SearchConfig,
        config_id: str | None,
        expected_version: int | None,
        idempotency_key: str,
        context: RequestContext,
    ) -> StoredSearchConfig:
        """Create/update a config, audit it, and complete its command atomically."""
        if expected_version is not None and expected_version < 1:
            raise ValidationError(
                "expected_version must be positive.",
                details={"field": "expected_version"},
            )
        normalized_config_id = _optional_identifier(config_id, "config_id")
        request = {
            "config_id": normalized_config_id,
            "expected_version": expected_version,
            "config": config.to_dict(),
        }
        request_hash = hash_request(request)
        with self._database.transaction(immediate=True) as connection:
            reservation = self._idempotency.reserve_in_transaction(
                connection,
                scope=_SAVE_CONFIG_SCOPE,
                idempotency_key=_require_identifier(idempotency_key, "idempotency_key"),
                request_hash=request_hash,
                context=context,
            )
            if reservation.state is IdempotencyReservationState.COMPLETED:
                response = reservation.response or {}
                completed_id = response.get("search_config_id")
                if not isinstance(completed_id, str):
                    raise InfrastructureError(
                        "Completed search configuration command did not contain an identifier.",
                        details={"record_id": reservation.record_id},
                    )
                return self._load_search_config(connection, completed_id)

            now = _utc_now()
            if normalized_config_id is None:
                if expected_version is not None:
                    raise ValidationError(
                        "expected_version is only valid when updating a search configuration.",
                        details={"field": "expected_version"},
                    )
                saved = StoredSearchConfig(
                    id=str(uuid4()),
                    version=1,
                    config=config,
                    created_at=now,
                    updated_at=now,
                )
                connection.execute(
                    """
                    INSERT INTO search_configs (
                        id, version, name, config_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        saved.id,
                        saved.version,
                        saved.config.name,
                        _encode_json(saved.config.to_dict()),
                        saved.created_at.isoformat(),
                        saved.updated_at.isoformat(),
                    ),
                )
                action = "discovery.search_config.created"
                before: Mapping[str, Any] | None = None
            else:
                existing = self._load_search_config(connection, normalized_config_id)
                if expected_version is None:
                    raise ValidationError(
                        "expected_version is required when updating a search configuration.",
                        details={"field": "expected_version"},
                    )
                if existing.version != expected_version:
                    raise ConflictError(
                        "The search configuration changed before this update was applied.",
                        details={
                            "search_config_id": normalized_config_id,
                            "expected_version": expected_version,
                            "actual_version": existing.version,
                        },
                    )
                saved = StoredSearchConfig(
                    id=existing.id,
                    version=existing.version + 1,
                    config=config,
                    created_at=existing.created_at,
                    updated_at=now,
                )
                connection.execute(
                    """
                    UPDATE search_configs
                    SET version = ?, name = ?, config_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        saved.version,
                        saved.config.name,
                        _encode_json(saved.config.to_dict()),
                        saved.updated_at.isoformat(),
                        saved.id,
                    ),
                )
                action = "discovery.search_config.updated"
                before = existing.to_dict()

            self._audit.append_in_transaction(
                connection,
                AuditEvent.create(
                    context=context,
                    action=action,
                    target_type="search_config",
                    target_id=saved.id,
                    before=before,
                    after=saved.to_dict(),
                ),
            )
            self._idempotency.complete_in_transaction(
                connection,
                record_id=reservation.record_id,
                response={"search_config_id": saved.id, "version": saved.version},
            )
            return saved

    def get_search_config(self, config_id: str) -> StoredSearchConfig:
        """Read one config using a short-lived connection."""
        normalized_config_id = _require_identifier(config_id, "config_id")
        with self._database.connect() as connection:
            return self._load_search_config(connection, normalized_config_id)

    def reserve_search_run(
        self,
        *,
        search_config_id: str,
        expected_config_version: int,
        provider: str,
        idempotency_key: str,
        context: RequestContext,
    ) -> SearchRunReservation:
        """Atomically reserve a StartSearchRun request and its durable run record."""
        normalized_config_id = _require_identifier(search_config_id, "search_config_id")
        normalized_provider = _require_identifier(provider, "provider")
        normalized_key = _require_identifier(idempotency_key, "idempotency_key")
        if expected_config_version < 1:
            raise ValidationError(
                "expected_config_version must be positive.",
                details={"field": "expected_config_version"},
            )
        request_hash = hash_request(
            {
                "search_config_id": normalized_config_id,
                "expected_config_version": expected_config_version,
                "provider": normalized_provider,
                "trigger_type": "manual",
            }
        )
        with self._database.transaction(immediate=True) as connection:
            reservation = self._idempotency.reserve_in_transaction(
                connection,
                scope=_START_SEARCH_SCOPE,
                idempotency_key=normalized_key,
                request_hash=request_hash,
                context=context,
                allow_in_progress=True,
            )
            if reservation.state is IdempotencyReservationState.COMPLETED:
                response = reservation.response or {}
                run_id = response.get("search_run_id")
                if not isinstance(run_id, str):
                    raise InfrastructureError(
                        "Completed search run command did not contain a run identifier.",
                        details={"record_id": reservation.record_id},
                    )
                return SearchRunReservation(
                    state=reservation.state,
                    idempotency_record_id=reservation.record_id,
                    run=self._load_search_run(connection, run_id),
                    response=response,
                )
            if reservation.state is IdempotencyReservationState.IN_PROGRESS:
                row = connection.execute(
                    """
                    SELECT id FROM search_runs
                    WHERE idempotency_scope = ? AND idempotency_key = ?
                    """,
                    (_START_SEARCH_SCOPE, normalized_key),
                ).fetchone()
                if row is None:
                    raise InfrastructureError(
                        "An in-progress search command has no durable search run.",
                        details={"idempotency_record_id": reservation.record_id},
                    )
                return SearchRunReservation(
                    state=reservation.state,
                    idempotency_record_id=reservation.record_id,
                    run=self._load_search_run(connection, str(row["id"])),
                )

            config = self._load_search_config(connection, normalized_config_id)
            if config.version != expected_config_version:
                raise ConflictError(
                    "The search configuration changed before the run started.",
                    details={
                        "search_config_id": config.id,
                        "expected_version": expected_config_version,
                        "actual_version": config.version,
                    },
                )
            now = _utc_now()
            run = SearchRun(
                id=str(uuid4()),
                search_config_id=config.id,
                search_config_version=config.version,
                config=config.config,
                provider=normalized_provider,
                status=SearchRunStatus.QUEUED,
                started_at=None,
                completed_at=None,
                request_count=0,
                raw_result_count=0,
                result_count=0,
                error_summary=(),
                correlation_id=context.correlation_id,
                actor_id=context.actor_id,
                created_at=now,
            )
            connection.execute(
                """
                INSERT INTO search_runs (
                    id, search_config_id, search_config_version, query_snapshot_json, trigger_type,
                    provider, status, started_at, completed_at, request_count, raw_result_count,
                    result_count, error_summary_json, correlation_id, actor_id, created_at,
                    idempotency_scope, idempotency_key, execution_token, execution_lease_expires_at
                ) VALUES (
                    ?, ?, ?, ?, 'manual', ?, ?, NULL, NULL, 0, 0, 0, NULL, ?, ?, ?, ?, ?, NULL, NULL
                )
                """,
                (
                    run.id,
                    run.search_config_id,
                    run.search_config_version,
                    _encode_json(run.config.to_dict()),
                    run.provider,
                    run.status.value,
                    run.correlation_id,
                    run.actor_id,
                    run.created_at.isoformat(),
                    _START_SEARCH_SCOPE,
                    normalized_key,
                ),
            )
            self._audit.append_in_transaction(
                connection,
                AuditEvent.create(
                    context=context,
                    action="discovery.search_run.queued",
                    target_type="search_run",
                    target_id=run.id,
                    after=run.to_dict(),
                    metadata={"provider": run.provider, "trigger_type": "manual"},
                ),
            )
            return SearchRunReservation(
                state=reservation.state,
                idempotency_record_id=reservation.record_id,
                run=run,
            )

    def claim_run(self, *, run_id: str, lease_seconds: int) -> RunClaim:
        """Claim a queued/expired run while retaining an explicit recovery lease."""
        if lease_seconds < 0:
            raise ValidationError(
                "lease_seconds must not be negative.",
                details={"field": "lease_seconds"},
            )
        normalized_run_id = _require_identifier(run_id, "run_id")
        with self._database.transaction(immediate=True) as connection:
            row = self._load_search_run_row(connection, normalized_run_id)
            run = _row_to_search_run(row)
            if run.status in _TERMINAL_STATUSES:
                return RunClaim(run=run, execution_token=None, claimed=False)

            now = _utc_now()
            lease_expires_at = _parse_optional_datetime(row["execution_lease_expires_at"])
            reclaimable = run.status is SearchRunStatus.QUEUED or (
                run.status is SearchRunStatus.RUNNING
                and (lease_expires_at is None or lease_expires_at <= now)
            )
            if not reclaimable:
                return RunClaim(run=run, execution_token=None, claimed=False)

            execution_token = str(uuid4())
            expiry = now + timedelta(seconds=lease_seconds)
            connection.execute(
                """
                UPDATE search_runs
                SET status = ?, started_at = COALESCE(started_at, ?), execution_token = ?,
                    execution_lease_expires_at = ?
                WHERE id = ?
                """,
                (
                    SearchRunStatus.RUNNING.value,
                    now.isoformat(),
                    execution_token,
                    expiry.isoformat(),
                    run.id,
                ),
            )
            claimed = self._load_search_run(connection, run.id)
            event_context = RequestContext.create(
                actor_id=run.actor_id,
                source="discovery-service",
                correlation_id=run.correlation_id,
            )
            self._audit.append_in_transaction(
                connection,
                AuditEvent.create(
                    context=event_context,
                    action=(
                        "discovery.search_run.started"
                        if run.status is SearchRunStatus.QUEUED
                        else "discovery.search_run.resumed"
                    ),
                    target_type="search_run",
                    target_id=run.id,
                    before={"status": run.status.value},
                    after={"status": claimed.status.value},
                    metadata={"lease_expires_at": expiry.isoformat()},
                ),
            )
            return RunClaim(run=claimed, execution_token=execution_token, claimed=True)

    def get_resume_state(self, *, run_id: str, execution_token: str) -> RunResumeState:
        """Read the latest persisted page checkpoint for a claimed run."""
        with self._database.connect() as connection:
            self._require_active_claim(
                connection,
                run_id=run_id,
                execution_token=execution_token,
            )
            rows = connection.execute(
                """
                SELECT cursor_value, next_cursor FROM provider_requests
                WHERE search_run_id = ? AND status = 'succeeded'
                ORDER BY request_sequence ASC
                """,
                (run_id,),
            ).fetchall()
        if not rows:
            return RunResumeState(
                has_succeeded_request=False,
                next_cursor=None,
                seen_cursors=frozenset(),
            )
        seen_cursors = frozenset(
            str(row["cursor_value"])
            for row in rows
            if row["cursor_value"] is not None
        )
        return RunResumeState(
            has_succeeded_request=True,
            next_cursor=_optional_text(rows[-1]["next_cursor"]),
            seen_cursors=seen_cursors,
        )

    def persist_page(
        self,
        *,
        run_id: str,
        execution_token: str,
        page: ProviderSearchPage,
        outcomes: Sequence[NormalizationOutcome],
        lease_seconds: int,
    ) -> SearchRun:
        """Save raw and normalized provider data before exposing it to consumers."""
        if len(page.jobs) != len(outcomes):
            raise ValidationError(
                "Each provider job must have exactly one normalization outcome.",
                details={"jobs": len(page.jobs), "outcomes": len(outcomes)},
            )
        with self._database.transaction(immediate=True) as connection:
            run = self._require_active_claim(
                connection,
                run_id=run_id,
                execution_token=execution_token,
            )
            if page.provider != run.provider:
                raise ValidationError(
                    "Provider page does not belong to this search run's provider.",
                    details={"run_provider": run.provider, "page_provider": page.provider},
                )
            now = _utc_now()
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(request_sequence), 0) + 1 AS next_sequence "
                    "FROM provider_requests WHERE search_run_id = ?",
                    (run.id,),
                ).fetchone()["next_sequence"]
            )
            provider_request_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO provider_requests (
                    id, search_run_id, request_sequence, provider, provider_query_json,
                    cursor_value, requested_at, completed_at, status, http_status,
                    result_count, next_cursor, error_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'succeeded', ?, ?, ?, NULL)
                """,
                (
                    provider_request_id,
                    run.id,
                    sequence,
                    page.provider,
                    _encode_json(page.query),
                    _optional_text(page.query.get("cursor")),
                    now.isoformat(),
                    now.isoformat(),
                    page.http_status,
                    len(page.jobs),
                    page.next_cursor,
                ),
            )
            raw_response_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO raw_provider_responses (
                    id, provider_request_id, search_run_id, provider, content_hash, raw_json,
                    response_metadata_json, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    raw_response_id,
                    provider_request_id,
                    run.id,
                    page.provider,
                    _hash_json(page.raw_response),
                    _encode_json(page.raw_response),
                    _encode_json(page.metadata),
                    now.isoformat(),
                ),
            )

            errors = [dict(error) for error in run.error_summary]
            normalized_count = 0
            for outcome in outcomes:
                raw_job = dict(outcome.raw_job)
                raw_payload_id = str(uuid4())
                external_id = _raw_external_id(raw_job)
                connection.execute(
                    """
                    INSERT INTO raw_job_payloads (
                        id, raw_provider_response_id, provider, external_id, content_hash, raw_json,
                        normalization_status, normalization_error_json, job_source_id, received_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?)
                    """,
                    (
                        raw_payload_id,
                        raw_response_id,
                        page.provider,
                        external_id,
                        _hash_json(raw_job),
                        _encode_json(raw_job),
                        now.isoformat(),
                    ),
                )
                if outcome.normalized_job is None:
                    error = dict(
                        outcome.error
                        or {"code": "normalization_error", "message": "Unknown error."}
                    )
                    errors.append(error)
                    connection.execute(
                        """
                        UPDATE raw_job_payloads
                        SET normalization_status = 'failed', normalization_error_json = ?
                        WHERE id = ?
                        """,
                        (_encode_json(error), raw_payload_id),
                    )
                    continue

                source_id = self._upsert_normalized_job(
                    connection,
                    normalized_job=outcome.normalized_job,
                    raw_payload_id=raw_payload_id,
                    observed_at=now,
                )
                connection.execute(
                    """
                    UPDATE raw_job_payloads
                    SET normalization_status = 'succeeded', job_source_id = ?
                    WHERE id = ?
                    """,
                    (source_id, raw_payload_id),
                )
                normalized_count += 1

            expiry = now + timedelta(seconds=lease_seconds)
            connection.execute(
                """
                UPDATE search_runs
                SET request_count = request_count + 1,
                    raw_result_count = raw_result_count + ?,
                    result_count = result_count + ?,
                    error_summary_json = ?,
                    execution_lease_expires_at = ?
                WHERE id = ?
                """,
                (
                    len(page.jobs),
                    normalized_count,
                    _encode_json(errors) if errors else None,
                    expiry.isoformat(),
                    run.id,
                ),
            )
            return self._load_search_run(connection, run.id)

    def finish_run(
        self,
        *,
        run_id: str,
        execution_token: str,
        idempotency_record_id: str,
        status: SearchRunStatus,
        provider_error: Mapping[str, Any] | None = None,
    ) -> SearchRun:
        """Finish a run and its StartSearchRun command atomically."""
        if status not in _TERMINAL_STATUSES:
            raise ValidationError(
                "Search run can only finish in a terminal state.",
                details={"status": status.value},
            )
        with self._database.transaction(immediate=True) as connection:
            run = self._require_active_claim(
                connection,
                run_id=run_id,
                execution_token=execution_token,
            )
            now = _utc_now()
            errors = [dict(error) for error in run.error_summary]
            request_increment = 0
            if provider_error is not None:
                error = dict(provider_error)
                errors.append(error)
                request_increment = 1
                sequence = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(request_sequence), 0) + 1 AS next_sequence "
                        "FROM provider_requests WHERE search_run_id = ?",
                        (run.id,),
                    ).fetchone()["next_sequence"]
                )
                connection.execute(
                    """
                    INSERT INTO provider_requests (
                        id, search_run_id, request_sequence, provider, provider_query_json,
                        cursor_value, requested_at, completed_at, status, http_status,
                        result_count, next_cursor, error_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'failed', ?, 0, NULL, ?)
                    """,
                    (
                        str(uuid4()),
                        run.id,
                        sequence,
                        run.provider,
                        _encode_json(_as_mapping(error.get("provider_query"))),
                        _optional_text(error.get("cursor")),
                        now.isoformat(),
                        now.isoformat(),
                        _as_optional_int(error.get("http_status"))
                        or _as_optional_int(
                            _as_mapping(error.get("details")).get("http_status")
                        ),
                        _encode_json(error),
                    ),
                )

            connection.execute(
                """
                UPDATE search_runs
                SET status = ?, completed_at = ?, request_count = request_count + ?,
                    error_summary_json = ?, execution_token = NULL,
                    execution_lease_expires_at = NULL
                WHERE id = ?
                """,
                (
                    status.value,
                    now.isoformat(),
                    request_increment,
                    _encode_json(errors) if errors else None,
                    run.id,
                ),
            )
            finished = self._load_search_run(connection, run.id)
            event_context = RequestContext.create(
                actor_id=run.actor_id,
                source="discovery-service",
                correlation_id=run.correlation_id,
            )
            self._audit.append_in_transaction(
                connection,
                AuditEvent.create(
                    context=event_context,
                    action=f"discovery.search_run.{status.value}",
                    target_type="search_run",
                    target_id=run.id,
                    before={"status": run.status.value},
                    after=finished.to_dict(),
                    outcome=(
                        AuditOutcome.SUCCEEDED
                        if status is SearchRunStatus.SUCCEEDED
                        else AuditOutcome.FAILED
                    ),
                ),
            )
            self._idempotency.complete_in_transaction(
                connection,
                record_id=idempotency_record_id,
                response={"search_run_id": finished.id, "status": finished.status.value},
            )
            return finished

    def _upsert_normalized_job(
        self,
        connection: sqlite3.Connection,
        *,
        normalized_job,
        raw_payload_id: str,
        observed_at: datetime,
    ) -> str:
        source = connection.execute(
            """
            SELECT id, canonical_job_id, content_hash
            FROM job_sources
            WHERE provider = ? AND external_id = ?
            """,
            ("jobspipe", normalized_job.external_id),
        ).fetchone()
        snapshot_json = _encode_json(normalized_job.to_dict())
        if source is None:
            canonical_job_id = str(uuid4())
            source_id = str(uuid4())
            snapshot_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO canonical_jobs (
                    id, company_name, company_domain, title, normalized_title, location_text,
                    country_code, canonical_url, current_snapshot_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'active', ?, ?)
                """,
                (
                    canonical_job_id,
                    normalized_job.company_name,
                    normalized_job.company_domain,
                    normalized_job.title,
                    normalized_job.normalized_title,
                    normalized_job.location_text,
                    normalized_job.country_code,
                    normalized_job.canonical_url,
                    observed_at.isoformat(),
                    observed_at.isoformat(),
                ),
            )
            self._insert_or_update_job_source(
                connection,
                source_id=source_id,
                canonical_job_id=canonical_job_id,
                normalized_job=normalized_job,
                raw_payload_id=raw_payload_id,
                observed_at=observed_at,
                is_new=True,
            )
            self._insert_snapshot(
                connection,
                snapshot_id=snapshot_id,
                canonical_job_id=canonical_job_id,
                source_id=source_id,
                normalized_job=normalized_job,
                snapshot_json=snapshot_json,
                created_at=observed_at,
            )
            connection.execute(
                "UPDATE canonical_jobs SET current_snapshot_id = ? WHERE id = ?",
                (snapshot_id, canonical_job_id),
            )
            return source_id

        source_id = str(source["id"])
        canonical_job_id = str(source["canonical_job_id"])
        content_changed = str(source["content_hash"]) != normalized_job.content_hash
        self._insert_or_update_job_source(
            connection,
            source_id=source_id,
            canonical_job_id=canonical_job_id,
            normalized_job=normalized_job,
            raw_payload_id=raw_payload_id,
            observed_at=observed_at,
            is_new=False,
        )
        connection.execute(
            """
            UPDATE canonical_jobs
            SET company_name = ?, company_domain = ?, title = ?, normalized_title = ?,
                location_text = ?, country_code = ?, canonical_url = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                normalized_job.company_name,
                normalized_job.company_domain,
                normalized_job.title,
                normalized_job.normalized_title,
                normalized_job.location_text,
                normalized_job.country_code,
                normalized_job.canonical_url,
                observed_at.isoformat(),
                canonical_job_id,
            ),
        )
        if content_changed:
            snapshot_id = str(uuid4())
            self._insert_snapshot(
                connection,
                snapshot_id=snapshot_id,
                canonical_job_id=canonical_job_id,
                source_id=source_id,
                normalized_job=normalized_job,
                snapshot_json=snapshot_json,
                created_at=observed_at,
            )
            connection.execute(
                "UPDATE canonical_jobs SET current_snapshot_id = ? WHERE id = ?",
                (snapshot_id, canonical_job_id),
            )
        return source_id

    @staticmethod
    def _insert_or_update_job_source(
        connection: sqlite3.Connection,
        *,
        source_id: str,
        canonical_job_id: str,
        normalized_job,
        raw_payload_id: str,
        observed_at: datetime,
        is_new: bool,
    ) -> None:
        values = (
            normalized_job.source_url,
            normalized_job.canonical_url,
            _encode_json(normalized_job.provider_sources),
            normalized_job.provider_posted_at_raw,
            _serialize_datetime(normalized_job.provider_posted_at),
            normalized_job.provider_last_seen_at_raw,
            _serialize_datetime(normalized_job.provider_last_seen_at),
            normalized_job.provider_verified_at_raw,
            _serialize_datetime(normalized_job.provider_verified_at),
            normalized_job.content_hash,
            observed_at.isoformat(),
            raw_payload_id,
            observed_at.isoformat(),
        )
        if is_new:
            connection.execute(
                """
                INSERT INTO job_sources (
                    id, canonical_job_id, provider, external_id, source_url, canonical_url,
                    provider_sources_json, provider_posted_at_raw, provider_posted_at,
                    provider_last_seen_at_raw, provider_last_seen_at, provider_verified_at_raw,
                    provider_verified_at, content_hash, first_seen_at, last_seen_at,
                    latest_raw_job_payload_id, created_at, updated_at
                ) VALUES (?, ?, 'jobspipe', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    canonical_job_id,
                    normalized_job.external_id,
                    *values[:10],
                    observed_at.isoformat(),
                    values[10],
                    values[11],
                    values[12],
                    values[12],
                ),
            )
            return
        connection.execute(
            """
            UPDATE job_sources
            SET source_url = ?, canonical_url = ?, provider_sources_json = ?,
                provider_posted_at_raw = ?, provider_posted_at = ?,
                provider_last_seen_at_raw = ?, provider_last_seen_at = ?,
                provider_verified_at_raw = ?, provider_verified_at = ?, content_hash = ?,
                last_seen_at = ?, latest_raw_job_payload_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (*values, source_id),
        )

    @staticmethod
    def _insert_snapshot(
        connection: sqlite3.Connection,
        *,
        snapshot_id: str,
        canonical_job_id: str,
        source_id: str,
        normalized_job,
        snapshot_json: str,
        created_at: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO job_snapshots (
                id, canonical_job_id, job_source_id, content_hash, normalizer_version,
                normalized_job_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                canonical_job_id,
                source_id,
                normalized_job.content_hash,
                normalized_job.normalizer_version,
                snapshot_json,
                created_at.isoformat(),
            ),
        )

    def _load_search_config(
        self,
        connection: sqlite3.Connection,
        config_id: str,
    ) -> StoredSearchConfig:
        row = connection.execute(
            "SELECT id, version, config_json, created_at, updated_at "
            "FROM search_configs WHERE id = ?",
            (config_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("search_config", config_id)
        return _row_to_search_config(row)

    def _load_search_run_row(self, connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT id, search_config_id, search_config_version, query_snapshot_json,
                   provider, status,
                   started_at, completed_at, request_count, raw_result_count, result_count,
                   error_summary_json, correlation_id, actor_id, created_at,
                   execution_token, execution_lease_expires_at
            FROM search_runs WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("search_run", run_id)
        return row

    def _load_search_run(self, connection: sqlite3.Connection, run_id: str) -> SearchRun:
        return _row_to_search_run(self._load_search_run_row(connection, run_id))

    def _require_active_claim(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        execution_token: str,
    ) -> SearchRun:
        row = self._load_search_run_row(connection, _require_identifier(run_id, "run_id"))
        run = _row_to_search_run(row)
        if run.status is not SearchRunStatus.RUNNING or row["execution_token"] != execution_token:
            raise InvalidStateError(
                "search_run",
                run.id,
                run.status.value,
                "persist discovery work",
            )
        return run


def _row_to_search_config(row: sqlite3.Row) -> StoredSearchConfig:
    return StoredSearchConfig(
        id=str(row["id"]),
        version=int(row["version"]),
        config=SearchConfig.from_dict(_decode_mapping(row["config_json"], "search config")),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _row_to_search_run(row: sqlite3.Row) -> SearchRun:
    return SearchRun(
        id=str(row["id"]),
        search_config_id=str(row["search_config_id"]),
        search_config_version=int(row["search_config_version"]),
        config=SearchConfig.from_dict(
            _decode_mapping(row["query_snapshot_json"], "search run query snapshot")
        ),
        provider=str(row["provider"]),
        status=SearchRunStatus(str(row["status"])),
        started_at=_parse_optional_datetime(row["started_at"]),
        completed_at=_parse_optional_datetime(row["completed_at"]),
        request_count=int(row["request_count"]),
        raw_result_count=int(row["raw_result_count"]),
        result_count=int(row["result_count"]),
        error_summary=_decode_error_summary(row["error_summary_json"]),
        correlation_id=str(row["correlation_id"]),
        actor_id=str(row["actor_id"]),
        created_at=_parse_datetime(row["created_at"]),
    )


def _decode_error_summary(value: object) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise InfrastructureError("Stored search run error summary is invalid JSON.") from exc
    if not isinstance(decoded, list) or not all(isinstance(item, Mapping) for item in decoded):
        raise InfrastructureError("Stored search run error summary must be an array of objects.")
    return tuple(dict(item) for item in decoded)


def _decode_mapping(value: object, description: str) -> Mapping[str, Any]:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise InfrastructureError(f"Stored {description} is invalid JSON.") from exc
    if not isinstance(decoded, Mapping):
        raise InfrastructureError(f"Stored {description} must be a JSON object.")
    return decoded


def _encode_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise InfrastructureError(
            "Discovery data must be JSON serializable.",
            details={"reason": str(exc)},
        ) from exc


def _hash_json(value: Any) -> str:
    from hashlib import sha256

    return sha256(_encode_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_datetime(value: object) -> datetime:
    parsed = _parse_optional_datetime(value)
    if parsed is None:
        raise InfrastructureError("Stored timestamp is unexpectedly null.")
    return parsed


def _parse_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise InfrastructureError(
            "Stored timestamp is invalid.",
            details={"value": str(value)},
        ) from exc


def _serialize_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _optional_identifier(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_identifier(value, field_name)


def _require_identifier(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValidationError(f"{field_name} must not be blank.", details={"field": field_name})
    return normalized


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _raw_external_id(raw_job: Mapping[str, Any]) -> str | None:
    value = raw_job.get("id")
    if value is None or isinstance(value, bool):
        return None
    return _optional_text(str(value))


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
