"""Integration tests for the Phase 1 durable Job Discovery workflow."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import shutil
import tempfile
import unittest
from typing import Any

from job_search_assistant.app_services import SearchRunService, build_foundation
from job_search_assistant.core import RequestContext
from job_search_assistant.discovery import (
    JobsPipeJobNormalizer,
    ProviderError,
    ProviderSearchPage,
    SearchConfig,
    SearchRunStatus,
)
from job_search_assistant.infrastructure.sqlite import SQLiteDiscoveryStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_MIGRATIONS = PROJECT_ROOT / "migrations"


class FakeProvider:
    """Deterministic provider fixture that never opens a network connection."""

    name = "jobspipe"

    def __init__(self, pages: Mapping[str | None, ProviderSearchPage | BaseException]) -> None:
        self._pages = dict(pages)
        self.calls: list[str | None] = []

    def search(self, config: SearchConfig, *, cursor: str | None = None) -> ProviderSearchPage:
        self.calls.append(cursor)
        result = self._pages[cursor]
        if isinstance(result, BaseException):
            raise result
        query = dict(result.query)
        if cursor is not None:
            query["cursor"] = cursor
        return ProviderSearchPage(
            provider=result.provider,
            query=query,
            raw_response=result.raw_response,
            jobs=result.jobs,
            metadata=result.metadata,
            next_cursor=result.next_cursor,
            http_status=result.http_status,
        )


class DiscoveryIntegrationTestCase(unittest.TestCase):
    """Use the actual SQLite adapters with an in-memory-style temporary database."""

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self._temporary_directory.name)
        migrations = self.workspace / "migrations"
        shutil.copytree(SOURCE_MIGRATIONS, migrations)
        self.services = build_foundation(
            database_path=self.workspace / "data" / "assistant.sqlite",
            migrations_path=migrations,
        )
        self.store = SQLiteDiscoveryStore(self.services.database)
        self.context = RequestContext.create(actor_id="user-1", source="test")
        self.config = SearchConfig(name="Backend roles", job_titles=("Backend Engineer",), limit=10)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_paginates_persists_raw_evidence_deduplicates_and_replays(self) -> None:
        provider = FakeProvider(
            {
                None: _page(
                    [_job("job-1", "Backend Engineer", last_seen="2026-09-01T10:00:00Z")],
                    "next-1",
                ),
                "next-1": _page([_job("job-2", "Platform Engineer")]),
            }
        )
        service = self._service(provider)
        saved = service.save_search_config(
            config=self.config,
            idempotency_key="config-create-1",
            context=self.context,
        )

        completed = service.start_search_run(
            search_config_id=saved.id,
            expected_config_version=saved.version,
            idempotency_key="run-1",
            context=self.context,
        )

        self.assertEqual(SearchRunStatus.SUCCEEDED, completed.status)
        self.assertEqual(2, completed.request_count)
        self.assertEqual(2, completed.raw_result_count)
        self.assertEqual(2, completed.result_count)
        self.assertEqual([None, "next-1"], provider.calls)
        self.assertEqual(2, self._count("raw_provider_responses"))
        self.assertEqual(2, self._count("raw_job_payloads"))
        self.assertEqual(2, self._count("canonical_jobs"))
        self.assertEqual(2, self._count("job_sources"))
        self.assertEqual(2, self._count("job_snapshots"))

        replayed = service.start_search_run(
            search_config_id=saved.id,
            expected_config_version=saved.version,
            idempotency_key="run-1",
            context=self.context,
        )
        self.assertEqual(completed.id, replayed.id)
        self.assertEqual([None, "next-1"], provider.calls)

        second_service = self._service(
            FakeProvider(
                {
                    None: _page(
                        [_job("job-1", "Backend Engineer", last_seen="2026-09-02T10:00:00Z")]
                    )
                }
            )
        )
        second_service.start_search_run(
            search_config_id=saved.id,
            expected_config_version=saved.version,
            idempotency_key="run-2",
            context=self.context,
        )
        self.assertEqual(3, self._count("raw_job_payloads"))
        self.assertEqual(2, self._count("job_sources"))
        self.assertEqual(2, self._count("job_snapshots"))

        actions = self.services.database.fetch_all(
            "SELECT action FROM audit_events WHERE target_type = 'search_run' "
            "ORDER BY occurred_at, id"
        )
        self.assertIn("discovery.search_run.queued", {str(row["action"]) for row in actions})
        self.assertIn("discovery.search_run.succeeded", {str(row["action"]) for row in actions})

    def test_invalid_job_becomes_partial_success_after_raw_payload_is_retained(self) -> None:
        service = self._service(
            FakeProvider({None: _page([_job("job-1", "Backend Engineer"), {"id": "invalid-job"}])})
        )
        saved = service.save_search_config(
            config=self.config,
            idempotency_key="config-create-2",
            context=self.context,
        )

        completed = service.start_search_run(
            search_config_id=saved.id,
            expected_config_version=saved.version,
            idempotency_key="run-partial-normalization",
            context=self.context,
        )

        self.assertEqual(SearchRunStatus.PARTIALLY_SUCCEEDED, completed.status)
        self.assertEqual(2, completed.raw_result_count)
        self.assertEqual(1, completed.result_count)
        self.assertEqual(2, self._count("raw_job_payloads"))
        # 同一页的两个 payload 共享 received_at，id 是 uuid4()，用它当次序键会导致
        # 断言的列表顺序随机（实测约 40% 概率失败）。rowid 才是真正的写入顺序。
        statuses = self.services.database.fetch_all(
            "SELECT normalization_status FROM raw_job_payloads ORDER BY received_at, rowid"
        )
        self.assertEqual(
            ["succeeded", "failed"],
            [str(row["normalization_status"]) for row in statuses],
        )
        self.assertEqual("validation_error", completed.error_summary[0]["code"])

    def test_provider_failure_after_a_page_is_durable_partial_success(self) -> None:
        service = self._service(
            FakeProvider(
                {
                    None: _page([_job("job-1", "Backend Engineer")], "next-1"),
                    "next-1": ProviderError(
                        "JobsPipe is temporarily unavailable.",
                        provider="jobspipe",
                        retryable=True,
                        http_status=429,
                    ),
                }
            )
        )
        saved = service.save_search_config(
            config=self.config,
            idempotency_key="config-create-3",
            context=self.context,
        )

        completed = service.start_search_run(
            search_config_id=saved.id,
            expected_config_version=saved.version,
            idempotency_key="run-partial-provider",
            context=self.context,
        )

        self.assertEqual(SearchRunStatus.PARTIALLY_SUCCEEDED, completed.status)
        self.assertEqual(2, completed.request_count)
        requests = self.services.database.fetch_all(
            "SELECT status, http_status FROM provider_requests "
            "WHERE search_run_id = ? ORDER BY request_sequence",
            (completed.id,),
        )
        self.assertEqual(["succeeded", "failed"], [str(row["status"]) for row in requests])
        self.assertEqual(429, requests[1]["http_status"])

    def test_retry_recovers_an_interrupted_run_with_the_same_idempotency_key(self) -> None:
        interrupted_service = self._service(
            FakeProvider(
                {
                    None: _page([_job("job-1", "Backend Engineer")], "next-1"),
                    "next-1": KeyboardInterrupt(),
                }
            ),
            lease_seconds=0,
        )
        saved = interrupted_service.save_search_config(
            config=self.config,
            idempotency_key="config-create-4",
            context=self.context,
        )

        with self.assertRaises(KeyboardInterrupt):
            interrupted_service.start_search_run(
                search_config_id=saved.id,
                expected_config_version=saved.version,
                idempotency_key="run-recover",
                context=self.context,
            )

        recovered_provider = FakeProvider({"next-1": _page([_job("job-2", "Platform Engineer")])})
        recovered_service = self._service(recovered_provider, lease_seconds=0)
        completed = recovered_service.start_search_run(
            search_config_id=saved.id,
            expected_config_version=saved.version,
            idempotency_key="run-recover",
            context=self.context,
        )

        self.assertEqual(SearchRunStatus.SUCCEEDED, completed.status)
        self.assertEqual(["next-1"], recovered_provider.calls)
        self.assertEqual(2, completed.raw_result_count)
        record = self.services.database.fetch_all(
            "SELECT status FROM idempotency_records WHERE scope = ? AND idempotency_key = ?",
            ("discovery.search_run.start", "run-recover"),
        )[0]
        self.assertEqual("completed", record["status"])

    def _service(self, provider: FakeProvider, *, lease_seconds: int = 300) -> SearchRunService:
        return SearchRunService(
            store=self.store,
            provider=provider,
            normalizer=JobsPipeJobNormalizer(),
            run_lease_seconds=lease_seconds,
        )

    def _count(self, table_name: str) -> int:
        rows = self.services.database.fetch_all(f"SELECT COUNT(*) AS count FROM {table_name}")
        return int(rows[0]["count"])


def _page(jobs: list[dict[str, Any]], next_cursor: str | None = None) -> ProviderSearchPage:
    response: dict[str, Any] = {"data": jobs, "metadata": {"next_cursor": next_cursor}}
    return ProviderSearchPage(
        provider="jobspipe",
        query={"job_title_or": ["Backend Engineer"]},
        raw_response=response,
        jobs=jobs,
        metadata=response["metadata"],
        next_cursor=next_cursor,
        http_status=200,
    )


def _job(job_id: str, title: str, *, last_seen: str | None = None) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": job_id,
        "job_title": title,
        "company": "Example Corp",
        "location": "Toronto, ON",
        "url": f"https://jobs.example.test/{job_id}?utm_source=test",
        "description": f"{title} role.",
        "date_posted": "2026-09-01",
    }
    if last_seen is not None:
        job["last_seen_at"] = last_seen
    return job


if __name__ == "__main__":
    unittest.main()
