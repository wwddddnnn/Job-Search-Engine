"""Idempotent command execution abstractions independent of SQLite."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any, Callable, Mapping, Protocol

from job_search_assistant.core.context import RequestContext
from job_search_assistant.core.errors import ApplicationError, ConflictError, InfrastructureError, ValidationError


JsonMapping = Mapping[str, Any]


class IdempotencyStatus(StrEnum):
    """Lifecycle status of a persisted idempotency record."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class IdempotencyReservationState(StrEnum):
    """Outcome of attempting to reserve an idempotency key."""

    ACQUIRED = "acquired"
    COMPLETED = "completed"
    IN_PROGRESS = "in_progress"


@dataclass(frozen=True, slots=True)
class IdempotencyReservation:
    """A newly acquired reservation or a replayable completed response."""

    state: IdempotencyReservationState
    record_id: str
    response: dict[str, Any] | None = None


class IdempotencyStore(Protocol):
    """Persistence contract implemented by the infrastructure layer."""

    def reserve(
        self,
        *,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        context: RequestContext,
    ) -> IdempotencyReservation:
        """Create a reservation or return a matching completed result."""

    def complete(self, *, record_id: str, response: JsonMapping) -> None:
        """Persist an operation response and mark a reservation complete."""

    def fail(self, *, record_id: str, error_code: str) -> None:
        """Mark a reservation failed without removing its request history."""


class IdempotencyService:
    """Ensures a write command executes at most once per scope/key/payload."""

    def __init__(self, store: IdempotencyStore) -> None:
        self._store = store

    def execute(
        self,
        *,
        scope: str,
        idempotency_key: str,
        request: JsonMapping,
        context: RequestContext,
        operation: Callable[[], JsonMapping],
    ) -> dict[str, Any]:
        """Execute once, replay a completed response, or reject a conflicting reuse."""
        normalized_scope = _require_identifier(scope, "scope")
        normalized_key = _require_identifier(idempotency_key, "idempotency_key")
        request_hash = hash_request(request)
        reservation = self._store.reserve(
            scope=normalized_scope,
            idempotency_key=normalized_key,
            request_hash=request_hash,
            context=context,
        )
        if reservation.state is IdempotencyReservationState.COMPLETED:
            if reservation.response is None:
                raise InfrastructureError(
                    "Completed idempotency record did not contain a response.",
                    details={"record_id": reservation.record_id},
                ).with_correlation_id(context.correlation_id)
            return reservation.response
        if reservation.state is IdempotencyReservationState.IN_PROGRESS:
            raise ConflictError(
                "The same idempotent request is already in progress.",
                details={"scope": normalized_scope, "idempotency_key": normalized_key},
            ).with_correlation_id(context.correlation_id)

        try:
            response = dict(operation())
        except ApplicationError as exc:
            self._store.fail(record_id=reservation.record_id, error_code=exc.code)
            raise exc.with_correlation_id(context.correlation_id)
        except Exception as exc:
            self._store.fail(record_id=reservation.record_id, error_code="unexpected_error")
            raise InfrastructureError(
                "An idempotent operation failed unexpectedly.",
                details={"scope": normalized_scope, "record_id": reservation.record_id},
            ).with_correlation_id(context.correlation_id) from exc

        self._store.complete(record_id=reservation.record_id, response=response)
        return response


def hash_request(request: JsonMapping) -> str:
    """Hash a JSON-compatible request using deterministic key ordering."""
    try:
        serialized = json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "Idempotent requests must be JSON serializable.",
            details={"reason": str(exc)},
        ) from exc
    return sha256(serialized.encode("utf-8")).hexdigest()


def _require_identifier(value: str, field_name: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValidationError(f"{field_name} must not be blank.", details={"field": field_name})
    return normalized_value
