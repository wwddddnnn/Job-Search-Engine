"""Application service for the Phase 1 manual job-discovery workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from job_search_assistant.core.context import RequestContext
from job_search_assistant.core.errors import ApplicationError, InfrastructureError
from job_search_assistant.core.idempotency import IdempotencyReservationState
from job_search_assistant.discovery.normalizer import JobsPipeJobNormalizer
from job_search_assistant.discovery.provider import JobSearchProvider
from job_search_assistant.discovery.store import (
    DiscoveryStore,
    NormalizationOutcome,
    SearchRun,
    StoredSearchConfig,
)
from job_search_assistant.discovery.types import ProviderError, SearchConfig, SearchRunStatus


@dataclass(slots=True)
class SearchRunService:
    """Coordinates durable provider discovery without exposing persistence to adapters.

    A short database transaction reserves and creates the run, but provider I/O
    always happens outside a transaction.  The run's renewable execution lease
    makes a same-key retry resume durable checkpoints after an interrupted
    process instead of permanently blocking on an idempotency record.
    """

    store: DiscoveryStore
    provider: JobSearchProvider
    normalizer: JobsPipeJobNormalizer
    run_lease_seconds: int = 300

    def __post_init__(self) -> None:
        if self.run_lease_seconds < 0:
            raise ValueError("run_lease_seconds must not be negative")

    def save_search_config(
        self,
        *,
        config: SearchConfig,
        idempotency_key: str,
        context: RequestContext,
        config_id: str | None = None,
        expected_version: int | None = None,
    ) -> StoredSearchConfig:
        """Create or update a product-level configuration through the discovery store."""
        return self.store.save_search_config(
            config=config,
            config_id=config_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            context=context,
        )

    def start_search_run(
        self,
        *,
        search_config_id: str,
        expected_config_version: int,
        idempotency_key: str,
        context: RequestContext,
    ) -> SearchRun:
        """Run every available provider page and return its terminal/checkpoint state."""
        reservation = self.store.reserve_search_run(
            search_config_id=search_config_id,
            expected_config_version=expected_config_version,
            provider=self.provider.name,
            idempotency_key=idempotency_key,
            context=context,
        )
        if reservation.run is None:
            raise InfrastructureError(
                "Search run reservation did not return a durable run.",
                details={"idempotency_record_id": reservation.idempotency_record_id},
            )
        if reservation.state is IdempotencyReservationState.COMPLETED:
            return reservation.run

        claim = self.store.claim_run(
            run_id=reservation.run.id,
            lease_seconds=self.run_lease_seconds,
        )
        if not claim.claimed:
            return claim.run
        if claim.execution_token is None:
            raise InfrastructureError("Claimed search run did not receive an execution token.")

        return self._execute_claimed_run(
            run=claim.run,
            execution_token=claim.execution_token,
            idempotency_record_id=reservation.idempotency_record_id,
        )

    def _execute_claimed_run(
        self,
        *,
        run: SearchRun,
        execution_token: str,
        idempotency_record_id: str,
    ) -> SearchRun:
        resume = self.store.get_resume_state(
            run_id=run.id,
            execution_token=execution_token,
        )
        if resume.has_succeeded_request and resume.next_cursor is None:
            return self._finish_after_pages(
                run=run,
                execution_token=execution_token,
                idempotency_record_id=idempotency_record_id,
            )

        cursor = resume.next_cursor
        seen_cursors = set(resume.seen_cursors)
        latest_run = run
        while True:
            if cursor is not None and cursor in seen_cursors:
                return self._finish_after_provider_error(
                    run=latest_run,
                    execution_token=execution_token,
                    idempotency_record_id=idempotency_record_id,
                    cursor=cursor,
                    error={
                        "code": "provider_pagination_loop",
                        "message": "The provider repeated a pagination cursor.",
                        "provider": self.provider.name,
                        "retryable": False,
                    },
                )
            try:
                page = self.provider.search(latest_run.config, cursor=cursor)
            except ProviderError as exc:
                return self._finish_after_provider_error(
                    run=latest_run,
                    execution_token=execution_token,
                    idempotency_record_id=idempotency_record_id,
                    cursor=cursor,
                    error=exc.to_dict(),
                )

            outcomes = tuple(self._normalize_job(raw_job) for raw_job in page.jobs)
            latest_run = self.store.persist_page(
                run_id=latest_run.id,
                execution_token=execution_token,
                page=page,
                outcomes=outcomes,
                lease_seconds=self.run_lease_seconds,
            )
            if cursor is not None:
                seen_cursors.add(cursor)
            if page.next_cursor is None:
                return self._finish_after_pages(
                    run=latest_run,
                    execution_token=execution_token,
                    idempotency_record_id=idempotency_record_id,
                )
            cursor = page.next_cursor

    def _finish_after_pages(
        self,
        *,
        run: SearchRun,
        execution_token: str,
        idempotency_record_id: str,
    ) -> SearchRun:
        status = (
            SearchRunStatus.PARTIALLY_SUCCEEDED
            if run.error_summary
            else SearchRunStatus.SUCCEEDED
        )
        return self.store.finish_run(
            run_id=run.id,
            execution_token=execution_token,
            idempotency_record_id=idempotency_record_id,
            status=status,
        )

    def _finish_after_provider_error(
        self,
        *,
        run: SearchRun,
        execution_token: str,
        idempotency_record_id: str,
        cursor: str | None,
        error: Mapping[str, Any],
    ) -> SearchRun:
        provider_error = dict(error)
        provider_error["cursor"] = cursor
        provider_error["provider_query"] = {
            "search_config": run.config.to_dict(),
            "cursor": cursor,
        }
        status = (
            SearchRunStatus.PARTIALLY_SUCCEEDED
            if run.raw_result_count
            else SearchRunStatus.FAILED
        )
        return self.store.finish_run(
            run_id=run.id,
            execution_token=execution_token,
            idempotency_record_id=idempotency_record_id,
            status=status,
            provider_error=provider_error,
        )

    def _normalize_job(self, raw_job: Mapping[str, Any]) -> NormalizationOutcome:
        try:
            return NormalizationOutcome(
                raw_job=raw_job,
                normalized_job=self.normalizer.normalize(raw_job),
            )
        except ApplicationError as exc:
            return NormalizationOutcome(
                raw_job=raw_job,
                normalized_job=None,
                error=exc.to_dict(),
            )
