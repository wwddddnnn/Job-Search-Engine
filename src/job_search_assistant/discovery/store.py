"""Persistence-facing contracts for the Job Discovery application service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence

from job_search_assistant.core.context import RequestContext
from job_search_assistant.core.idempotency import IdempotencyReservationState
from job_search_assistant.discovery.types import NormalizedJob, ProviderSearchPage, SearchConfig, SearchRunStatus


@dataclass(frozen=True, slots=True)
class StoredSearchConfig:
    """A versioned, persisted product-level search configuration."""

    id: str
    version: int
    config: SearchConfig
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "config": self.config.to_dict(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class SearchRun:
    """Durable state for one manually triggered provider search."""

    id: str
    search_config_id: str
    search_config_version: int
    config: SearchConfig
    provider: str
    status: SearchRunStatus
    started_at: datetime | None
    completed_at: datetime | None
    request_count: int
    raw_result_count: int
    result_count: int
    error_summary: Sequence[Mapping[str, Any]]
    correlation_id: str
    actor_id: str
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "search_config_id": self.search_config_id,
            "search_config_version": self.search_config_version,
            "provider": self.provider,
            "status": self.status.value,
            "started_at": _serialize_datetime(self.started_at),
            "completed_at": _serialize_datetime(self.completed_at),
            "request_count": self.request_count,
            "raw_result_count": self.raw_result_count,
            "result_count": self.result_count,
            "error_summary": [dict(error) for error in self.error_summary],
            "correlation_id": self.correlation_id,
            "actor_id": self.actor_id,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class SearchRunReservation:
    """A start-command replay, a new run, or a resumable interrupted run."""

    state: IdempotencyReservationState
    idempotency_record_id: str
    run: SearchRun | None = None
    response: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RunClaim:
    """Exclusive, renewable ownership of a search run's external provider work."""

    run: SearchRun
    execution_token: str | None
    claimed: bool


@dataclass(frozen=True, slots=True)
class RunResumeState:
    """Pagination checkpoint reconstructed from durable provider requests."""

    has_succeeded_request: bool
    next_cursor: str | None
    seen_cursors: frozenset[str]


@dataclass(frozen=True, slots=True)
class NormalizationOutcome:
    """The result of normalizing one raw provider job object."""

    raw_job: Mapping[str, Any]
    normalized_job: NormalizedJob | None
    error: Mapping[str, Any] | None = None


class DiscoveryStore(Protocol):
    """Port that keeps discovery writes transactional and adapter-independent."""

    def save_search_config(
        self,
        *,
        config: SearchConfig,
        config_id: str | None,
        expected_version: int | None,
        idempotency_key: str,
        context: RequestContext,
    ) -> StoredSearchConfig:
        """Create or update one search configuration and its audit record atomically."""

    def get_search_config(self, config_id: str) -> StoredSearchConfig:
        """Return a persisted configuration or raise a not-found error."""

    def reserve_search_run(
        self,
        *,
        search_config_id: str,
        expected_config_version: int,
        provider: str,
        idempotency_key: str,
        context: RequestContext,
    ) -> SearchRunReservation:
        """Atomically reserve a command and create/find its durable search run."""

    def claim_run(self, *, run_id: str, lease_seconds: int) -> RunClaim:
        """Claim queued or expired work without holding a database transaction during I/O."""

    def get_resume_state(self, *, run_id: str, execution_token: str) -> RunResumeState:
        """Return the next page checkpoint for an actively claimed run."""

    def persist_page(
        self,
        *,
        run_id: str,
        execution_token: str,
        page: ProviderSearchPage,
        outcomes: Sequence[NormalizationOutcome],
        lease_seconds: int,
    ) -> SearchRun:
        """Persist raw evidence, normalized jobs, and source/snapshot updates atomically."""

    def finish_run(
        self,
        *,
        run_id: str,
        execution_token: str,
        idempotency_record_id: str,
        status: SearchRunStatus,
        provider_error: Mapping[str, Any] | None = None,
    ) -> SearchRun:
        """Write terminal state, audit, and idempotency response atomically."""


def _serialize_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
