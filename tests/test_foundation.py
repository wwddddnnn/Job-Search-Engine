"""Regression tests for the Phase 0 shared foundation."""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from job_search_assistant.app_services import build_foundation
from job_search_assistant.core import (
    AuditOutcome,
    ConflictError,
    RequestContext,
    ValidationError,
)
from job_search_assistant.infrastructure.sqlite import SQLiteAuditSink, SQLiteDatabase


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_MIGRATIONS = PROJECT_ROOT / "migrations"


class FoundationTestCase(unittest.TestCase):
    """Create an isolated migration directory and database for every test."""

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self._temporary_directory.name)
        self.migrations_path = self.workspace / "migrations"
        shutil.copytree(SOURCE_MIGRATIONS, self.migrations_path)
        self.database_path = self.workspace / "data" / "assistant.sqlite"
        self.services = build_foundation(
            database_path=self.database_path,
            migrations_path=self.migrations_path,
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_migrations_are_idempotent_and_create_foundation_tables(self) -> None:
        self.services.database.migrate()
        table_rows = self.services.database.fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
        table_names = {str(row["name"]) for row in table_rows}
        self.assertTrue({"schema_migrations", "idempotency_records", "audit_events"}.issubset(table_names))
        migrations = self.services.database.fetch_all("SELECT version FROM schema_migrations")
        self.assertEqual(
            ["0001", "0002", "0003", "0004", "0005"],
            [str(row["version"]) for row in migrations],
        )

    def test_modified_applied_migration_is_rejected(self) -> None:
        migration_path = self.migrations_path / "0001_foundation.sql"
        migration_path.write_text(migration_path.read_text(encoding="utf-8") + "\n-- modified\n", encoding="utf-8")
        database = SQLiteDatabase(self.database_path, self.migrations_path)
        with self.assertRaisesRegex(Exception, "applied database migration was modified"):
            database.migrate()

    def test_idempotency_replays_completed_response_without_reexecuting(self) -> None:
        context = RequestContext.create(actor_id="user-1", source="test")
        call_count = 0

        def operation() -> dict[str, object]:
            nonlocal call_count
            call_count += 1
            return {"resource_id": "resource-123", "created": True}

        first = self.services.idempotency.execute(
            scope="application.create",
            idempotency_key="request-001",
            request={"job_id": "job-123"},
            context=context,
            operation=operation,
        )
        second = self.services.idempotency.execute(
            scope="application.create",
            idempotency_key="request-001",
            request={"job_id": "job-123"},
            context=context,
            operation=operation,
        )

        self.assertEqual(1, call_count)
        self.assertEqual(first, second)
        records = self.services.database.fetch_all(
            "SELECT status, correlation_id FROM idempotency_records WHERE scope = ?",
            ("application.create",),
        )
        self.assertEqual("completed", records[0]["status"])
        self.assertEqual(context.correlation_id, records[0]["correlation_id"])

    def test_idempotency_rejects_key_reuse_with_a_different_request(self) -> None:
        context = RequestContext.create(actor_id="user-1", source="test")
        self.services.idempotency.execute(
            scope="search.start",
            idempotency_key="request-002",
            request={"query": "backend engineer"},
            context=context,
            operation=lambda: {"search_run_id": "run-1"},
        )

        with self.assertRaises(ConflictError) as raised:
            self.services.idempotency.execute(
                scope="search.start",
                idempotency_key="request-002",
                request={"query": "data engineer"},
                context=context,
                operation=lambda: {"search_run_id": "run-2"},
            )
        self.assertEqual("conflict", raised.exception.code)
        self.assertEqual(context.correlation_id, raised.exception.correlation_id)

    def test_failed_idempotent_request_is_retained_and_requires_a_new_key(self) -> None:
        context = RequestContext.create(actor_id="user-1", source="test")
        with self.assertRaises(ValidationError):
            self.services.idempotency.execute(
                scope="profile.confirm",
                idempotency_key="request-003",
                request={"experience_id": "experience-1"},
                context=context,
                operation=lambda: _raise_validation_error(),
            )
        with self.assertRaises(ConflictError):
            self.services.idempotency.execute(
                scope="profile.confirm",
                idempotency_key="request-003",
                request={"experience_id": "experience-1"},
                context=context,
                operation=lambda: {"unexpected": "retry"},
            )

    def test_audit_events_preserve_context_and_json_snapshots(self) -> None:
        context = RequestContext.create(
            actor_id="user-1",
            source="web-ui",
            correlation_id="correlation-001",
            causation_id="parent-001",
        )
        event = self.services.audit.record(
            context=context,
            action="application.reviewed",
            target_type="application",
            target_id="application-1",
            outcome=AuditOutcome.SUCCEEDED,
            before={"status": "prepared"},
            after={"status": "awaiting_review"},
            metadata={"review_packet_hash": "sha256:example"},
        )

        events = SQLiteAuditSink(self.services.database).list_for_target(
            target_type="application",
            target_id="application-1",
        )
        self.assertEqual(1, len(events))
        persisted = events[0]
        self.assertEqual(event.id, persisted.id)
        self.assertEqual("correlation-001", persisted.correlation_id)
        self.assertEqual("parent-001", persisted.causation_id)
        self.assertEqual({"status": "prepared"}, persisted.before)
        self.assertEqual({"status": "awaiting_review"}, persisted.after)
        self.assertEqual({"review_packet_hash": "sha256:example"}, persisted.metadata)


def _raise_validation_error() -> dict[str, object]:
    raise ValidationError("The fact requires user confirmation.")


if __name__ == "__main__":
    unittest.main()
