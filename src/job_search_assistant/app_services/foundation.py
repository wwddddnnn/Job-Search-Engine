"""Composition root for Phase 0 shared services.

Domain modules must receive these services through application-service wiring;
they must not construct SQLite adapters directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from job_search_assistant.core.audit import AuditService
from job_search_assistant.core.idempotency import IdempotencyService
from job_search_assistant.infrastructure.sqlite import (
    SQLiteAuditSink,
    SQLiteDatabase,
    SQLiteIdempotencyStore,
)


@dataclass(frozen=True, slots=True)
class FoundationServices:
    """The shared Phase 0 service bundle for future application use cases."""

    database: SQLiteDatabase
    audit: AuditService
    idempotency: IdempotencyService


def build_foundation(
    *,
    database_path: Path | str,
    migrations_path: Path | str,
) -> FoundationServices:
    """Migrate a local database and compose the shared application services."""
    database = SQLiteDatabase(database_path=database_path, migrations_path=migrations_path)
    database.migrate()
    return FoundationServices(
        database=database,
        audit=AuditService(SQLiteAuditSink(database)),
        idempotency=IdempotencyService(SQLiteIdempotencyStore(database)),
    )
