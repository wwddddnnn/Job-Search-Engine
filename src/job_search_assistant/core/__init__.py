"""Shared, framework-independent foundations for every business module."""

from job_search_assistant.core.audit import AuditEvent, AuditOutcome, AuditService, AuditSink
from job_search_assistant.core.context import RequestContext, get_request_context, request_context
from job_search_assistant.core.errors import (
    ApplicationError,
    AuthorizationError,
    ConflictError,
    InfrastructureError,
    InvalidStateError,
    NotFoundError,
    ValidationError,
)
from job_search_assistant.core.idempotency import (
    IdempotencyReservation,
    IdempotencyReservationState,
    IdempotencyService,
    IdempotencyStatus,
    IdempotencyStore,
    hash_request,
)

__all__ = [
    "ApplicationError",
    "AuditEvent",
    "AuditOutcome",
    "AuditService",
    "AuditSink",
    "AuthorizationError",
    "ConflictError",
    "IdempotencyReservation",
    "IdempotencyReservationState",
    "IdempotencyService",
    "IdempotencyStatus",
    "IdempotencyStore",
    "InfrastructureError",
    "InvalidStateError",
    "NotFoundError",
    "RequestContext",
    "ValidationError",
    "get_request_context",
    "hash_request",
    "request_context",
]
