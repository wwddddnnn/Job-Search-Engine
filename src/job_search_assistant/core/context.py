"""Request-scoped execution context for correlation and auditability."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Iterator
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Identifies an operation's actor, source, and correlation lineage."""

    correlation_id: str
    actor_id: str
    source: str
    requested_at: datetime
    causation_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        actor_id: str = "local-user",
        source: str = "system",
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> "RequestContext":
        """Create a context with a generated correlation identifier when omitted."""
        normalized_actor = actor_id.strip()
        normalized_source = source.strip()
        if not normalized_actor:
            raise ValueError("actor_id must not be blank")
        if not normalized_source:
            raise ValueError("source must not be blank")
        return cls(
            correlation_id=correlation_id or str(uuid4()),
            actor_id=normalized_actor,
            source=normalized_source,
            requested_at=datetime.now(UTC),
            causation_id=causation_id,
        )

    def child(self, *, source: str | None = None) -> "RequestContext":
        """Create a child context preserving the current correlation lineage."""
        return replace(
            self,
            causation_id=self.correlation_id,
            source=source.strip() if source is not None else self.source,
            requested_at=datetime.now(UTC),
        )


_current_context: ContextVar[RequestContext | None] = ContextVar(
    "job_search_assistant_request_context",
    default=None,
)


def get_request_context() -> RequestContext | None:
    """Return the active context, if the current execution has one."""
    return _current_context.get()


def set_request_context(context: RequestContext) -> Token[RequestContext | None]:
    """Set the active context and return a token for explicit restoration."""
    return _current_context.set(context)


def reset_request_context(token: Token[RequestContext | None]) -> None:
    """Restore the prior context using the token returned by set_request_context."""
    _current_context.reset(token)


@contextmanager
def request_context(context: RequestContext) -> Iterator[RequestContext]:
    """Scope a request context to a synchronous unit of application work."""
    token = set_request_context(context)
    try:
        yield context
    finally:
        reset_request_context(token)
