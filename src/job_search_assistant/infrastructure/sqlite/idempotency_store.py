"""SQLite persistence for idempotent application commands."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import sqlite3
from typing import Any, Mapping
from uuid import uuid4

from job_search_assistant.core.context import RequestContext
from job_search_assistant.core.errors import ConflictError, InfrastructureError
from job_search_assistant.core.idempotency import (
    IdempotencyReservation,
    IdempotencyReservationState,
    IdempotencyStatus,
)
from job_search_assistant.infrastructure.sqlite.database import SQLiteDatabase


class SQLiteIdempotencyStore:
    """Implements atomic idempotency-key reservation in a local SQLite database."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def reserve(
        self,
        *,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        context: RequestContext,
    ) -> IdempotencyReservation:
        """Reserve a key or deterministically replay an existing completed response."""
        with self._database.transaction(immediate=True) as connection:
            return self.reserve_in_transaction(
                connection,
                scope=scope,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                context=context,
            )

    def reserve_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        context: RequestContext,
        allow_in_progress: bool = False,
    ) -> IdempotencyReservation:
        """Reserve a key using a caller-owned transaction.

        Long-running workflows can atomically create their durable work record with
        the reservation, then safely resume that record when the same request is
        retried after an interruption.
        """
        try:
            record_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO idempotency_records (
                    id, scope, idempotency_key, request_hash, status, response_json,
                    error_code, correlation_id, actor_id, created_at, completed_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, NULL, NULL)
                """,
                (
                    record_id,
                    scope,
                    idempotency_key,
                    request_hash,
                    IdempotencyStatus.IN_PROGRESS.value,
                    context.correlation_id,
                    context.actor_id,
                    datetime.now(UTC).isoformat(),
                ),
            )
            return IdempotencyReservation(
                state=IdempotencyReservationState.ACQUIRED,
                record_id=record_id,
            )
        except sqlite3.IntegrityError as exc:
            if not _is_unique_constraint_error(exc):
                raise InfrastructureError(
                    "Unable to reserve an idempotency key.",
                    details={
                        "scope": scope,
                        "idempotency_key": idempotency_key,
                        "reason": str(exc),
                    },
                ).with_correlation_id(context.correlation_id) from exc

        row = connection.execute(
                """
                SELECT id, request_hash, status, response_json, error_code, correlation_id
                FROM idempotency_records
                WHERE scope = ? AND idempotency_key = ?
                """,
                (scope, idempotency_key),
            ).fetchone()
        if row is None:
            raise InfrastructureError(
                "The idempotency reservation could not be read after a uniqueness conflict.",
                details={"scope": scope, "idempotency_key": idempotency_key},
            ).with_correlation_id(context.correlation_id)
        if str(row["request_hash"]) != request_hash:
            raise ConflictError(
                "An idempotency key was reused with a different request payload.",
                details={
                    "scope": scope,
                    "idempotency_key": idempotency_key,
                    "existing_correlation_id": str(row["correlation_id"]),
                },
            ).with_correlation_id(context.correlation_id)

        status = IdempotencyStatus(str(row["status"]))
        if status is IdempotencyStatus.COMPLETED:
            return IdempotencyReservation(
                state=IdempotencyReservationState.COMPLETED,
                record_id=str(row["id"]),
                response=_decode_response(row["response_json"], str(row["id"])),
            )
        if status is IdempotencyStatus.IN_PROGRESS:
            if allow_in_progress:
                return IdempotencyReservation(
                    state=IdempotencyReservationState.IN_PROGRESS,
                    record_id=str(row["id"]),
                )
            raise ConflictError(
                "The same idempotent request is already in progress.",
                details={
                    "scope": scope,
                    "idempotency_key": idempotency_key,
                    "record_id": str(row["id"]),
                },
            ).with_correlation_id(context.correlation_id)
        raise ConflictError(
            "The prior request for this idempotency key failed; use a new key to retry.",
            details={
                "scope": scope,
                "idempotency_key": idempotency_key,
                "record_id": str(row["id"]),
                "error_code": row["error_code"],
            },
        ).with_correlation_id(context.correlation_id)

    def complete(self, *, record_id: str, response: Mapping[str, Any]) -> None:
        """Persist an operation response exactly once."""
        serialized_response = _encode_response(response)
        with self._database.transaction(immediate=True) as connection:
            self.complete_in_transaction(
                connection,
                record_id=record_id,
                serialized_response=serialized_response,
            )

    def complete_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        record_id: str,
        response: Mapping[str, Any] | None = None,
        serialized_response: str | None = None,
    ) -> None:
        """Complete a reservation using the caller-owned transaction."""
        if serialized_response is None:
            if response is None:
                raise ValueError("response or serialized_response is required")
            serialized_response = _encode_response(response)
        cursor = connection.execute(
                """
                UPDATE idempotency_records
                SET status = ?, response_json = ?, completed_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    IdempotencyStatus.COMPLETED.value,
                    serialized_response,
                    datetime.now(UTC).isoformat(),
                    record_id,
                    IdempotencyStatus.IN_PROGRESS.value,
                ),
            )
        if cursor.rowcount != 1:
            raise InfrastructureError(
                "Unable to complete an idempotency record in its current state.",
                details={"record_id": record_id},
            )

    def fail(self, *, record_id: str, error_code: str) -> None:
        """Persist failure state while preserving the idempotency request history."""
        with self._database.transaction(immediate=True) as connection:
            self.fail_in_transaction(connection, record_id=record_id, error_code=error_code)

    def fail_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        record_id: str,
        error_code: str,
    ) -> None:
        """Mark a reservation failed using the caller-owned transaction."""
        cursor = connection.execute(
                """
                UPDATE idempotency_records
                SET status = ?, error_code = ?, completed_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    IdempotencyStatus.FAILED.value,
                    error_code,
                    datetime.now(UTC).isoformat(),
                    record_id,
                    IdempotencyStatus.IN_PROGRESS.value,
                ),
            )
        if cursor.rowcount != 1:
            raise InfrastructureError(
                "Unable to fail an idempotency record in its current state.",
                details={"record_id": record_id},
            )


def _encode_response(response: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            response,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise InfrastructureError(
            "Idempotency responses must be JSON serializable.",
            details={"reason": str(exc)},
        ) from exc


def _decode_response(raw_response: Any, record_id: str) -> dict[str, Any]:
    if raw_response is None:
        raise InfrastructureError(
            "Completed idempotency record has no stored response.",
            details={"record_id": record_id},
        )
    try:
        decoded = json.loads(str(raw_response))
    except json.JSONDecodeError as exc:
        raise InfrastructureError(
            "Completed idempotency response is invalid JSON.",
            details={"record_id": record_id},
        ) from exc
    if not isinstance(decoded, dict):
        raise InfrastructureError(
            "Completed idempotency response must be a JSON object.",
            details={"record_id": record_id},
        )
    return decoded


def _is_unique_constraint_error(exc: Exception) -> bool:
    return (
        "UNIQUE constraint failed: idempotency_records.scope, "
        "idempotency_records.idempotency_key" in str(exc)
    )
