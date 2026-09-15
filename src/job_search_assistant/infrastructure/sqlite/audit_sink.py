"""SQLite implementation for immutable audit event persistence."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from job_search_assistant.core.audit import AuditEvent
from job_search_assistant.core.errors import InfrastructureError
from job_search_assistant.infrastructure.sqlite.database import SQLiteDatabase


class SQLiteAuditSink:
    """Append-only audit event storage backed by SQLite."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def append(self, event: AuditEvent) -> None:
        """Persist an immutable event in its own short transaction."""
        with self._database.transaction(immediate=True) as connection:
            self.append_in_transaction(connection, event)

    def append_in_transaction(self, connection: sqlite3.Connection, event: AuditEvent) -> None:
        """Append an audit event as part of a caller-owned atomic write."""
        try:
            connection.execute(
                """
                INSERT INTO audit_events (
                    id, occurred_at, correlation_id, causation_id, actor_id, source,
                    action, target_type, target_id, outcome, before_json, after_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.occurred_at.isoformat(),
                    event.correlation_id,
                    event.causation_id,
                    event.actor_id,
                    event.source,
                    event.action,
                    event.target_type,
                    event.target_id,
                    event.outcome.value,
                    _encode_optional(event.before),
                    _encode_optional(event.after),
                    _encode_required(event.metadata),
                ),
            )
        except Exception as exc:
            if isinstance(exc, InfrastructureError):
                raise
            raise InfrastructureError(
                "Unable to persist an audit event.",
                details={"event_id": event.id, "reason": str(exc)},
            ).with_correlation_id(event.correlation_id) from exc

    def list_for_target(self, *, target_type: str, target_id: str) -> list[AuditEvent]:
        """Return audit history ordered by occurrence time and insertion order."""
        rows = self._database.fetch_all(
            """
            SELECT id, occurred_at, correlation_id, causation_id, actor_id, source,
                   action, target_type, target_id, outcome, before_json, after_json, metadata_json
            FROM audit_events
            WHERE target_type = ? AND target_id = ?
            ORDER BY occurred_at ASC, id ASC
            """,
            (target_type, target_id),
        )
        return [
            AuditEvent(
                id=str(row["id"]),
                occurred_at=_parse_timestamp(str(row["occurred_at"])),
                correlation_id=str(row["correlation_id"]),
                causation_id=row["causation_id"],
                actor_id=str(row["actor_id"]),
                source=str(row["source"]),
                action=str(row["action"]),
                target_type=str(row["target_type"]),
                target_id=str(row["target_id"]),
                outcome=_parse_outcome(str(row["outcome"])),
                before=_decode_optional(row["before_json"]),
                after=_decode_optional(row["after_json"]),
                metadata=_decode_required(row["metadata_json"]),
            )
            for row in rows
        ]


def _encode_optional(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    return _encode_required(value)


def _encode_required(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InfrastructureError(
            "Audit event payload must be JSON serializable.",
            details={"reason": str(exc)},
        ) from exc


def _decode_optional(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return _decode_required(value)


def _decode_required(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise InfrastructureError("Stored audit event payload is invalid JSON.") from exc
    if not isinstance(decoded, dict):
        raise InfrastructureError("Stored audit event payload must be a JSON object.")
    return decoded


def _parse_timestamp(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)


def _parse_outcome(value: str):
    from job_search_assistant.core.audit import AuditOutcome

    return AuditOutcome(value)
