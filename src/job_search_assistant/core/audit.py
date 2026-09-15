"""Audit event abstractions used by commands that alter state or access sensitive data."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping, Protocol
from uuid import uuid4

from job_search_assistant.core.context import RequestContext


class AuditOutcome(StrEnum):
    """Outcome of an auditable action."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """An immutable, structured record of a material system action."""

    id: str
    occurred_at: datetime
    correlation_id: str
    causation_id: str | None
    actor_id: str
    source: str
    action: str
    target_type: str
    target_id: str
    outcome: AuditOutcome
    before: Mapping[str, Any] | None = None
    after: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        context: RequestContext,
        action: str,
        target_type: str,
        target_id: str,
        outcome: AuditOutcome = AuditOutcome.SUCCEEDED,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "AuditEvent":
        """Build a valid audit event for the current application command."""
        return cls(
            id=str(uuid4()),
            occurred_at=datetime.now(UTC),
            correlation_id=context.correlation_id,
            causation_id=context.causation_id,
            actor_id=_require_value(context.actor_id, "actor_id"),
            source=_require_value(context.source, "source"),
            action=_require_value(action, "action"),
            target_type=_require_value(target_type, "target_type"),
            target_id=_require_value(target_id, "target_id"),
            outcome=outcome,
            before=before,
            after=after,
            metadata=metadata or {},
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation for adapters and diagnostics."""
        event = asdict(self)
        event["occurred_at"] = self.occurred_at.isoformat()
        event["outcome"] = self.outcome.value
        return event


class AuditSink(Protocol):
    """Persistence contract for immutable audit events."""

    def append(self, event: AuditEvent) -> None:
        """Persist an audit event without mutation or deletion."""


class AuditService:
    """Creates and persists audit events through an infrastructure-agnostic sink."""

    def __init__(self, sink: AuditSink) -> None:
        self._sink = sink

    def record(
        self,
        *,
        context: RequestContext,
        action: str,
        target_type: str,
        target_id: str,
        outcome: AuditOutcome = AuditOutcome.SUCCEEDED,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuditEvent:
        """Create and append one immutable audit event."""
        event = AuditEvent.create(
            context=context,
            action=action,
            target_type=target_type,
            target_id=target_id,
            outcome=outcome,
            before=before,
            after=after,
            metadata=metadata,
        )
        self._sink.append(event)
        return event


def _require_value(value: str, field_name: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(f"{field_name} must not be blank")
    return normalized_value
