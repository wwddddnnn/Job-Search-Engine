"""Local UI read queries and atomic preference persistence, used by application services."""

import sqlite3

from job_search_assistant.core.audit import AuditEvent
from job_search_assistant.core.errors import InfrastructureError
from job_search_assistant.core.idempotency import IdempotencyReservationState, hash_request
from job_search_assistant.infrastructure.sqlite.audit_sink import SQLiteAuditSink
from job_search_assistant.infrastructure.sqlite.idempotency_store import SQLiteIdempotencyStore


class SQLiteUIStore:
    def __init__(self, database):
        self.database = database

    def document_ids(self):
        return [row["id"] for row in self.database.fetch_all(
            "SELECT id FROM resume_documents ORDER BY uploaded_at, id",
        )]

    def profile_id(self):
        # Single-user UI opens the oldest existing profile; never silently creates a second.
        rows = self.database.fetch_all("SELECT id FROM career_profiles ORDER BY created_at, id")
        return rows[0]["id"] if rows else None

    def draft_id(self, profile_id):
        rows = self.database.fetch_all(
            "SELECT id FROM career_review_drafts WHERE profile_id = ?", (profile_id,),
        )
        return rows[0]["id"] if rows else None

    def language(self):
        rows = self.database.fetch_all("SELECT language FROM ui_settings WHERE id = 1")
        if not rows or rows[0]["language"] not in ("zh", "en"):
            raise InfrastructureError("UI settings are unavailable.")
        return rows[0]["language"]

    def save_language(self, *, language, context, idempotency_key):
        idem = SQLiteIdempotencyStore(self.database)
        try:
            with self.database.transaction(immediate=True) as connection:
                reservation = idem.reserve_in_transaction(
                    connection, scope="ui.settings.update", idempotency_key=idempotency_key,
                    request_hash=hash_request({"language": language, "actor": context.actor_id}),
                    context=context,
                )
                if reservation.state is IdempotencyReservationState.COMPLETED:
                    return reservation.response
                before = connection.execute(
                    "SELECT language FROM ui_settings WHERE id = 1",
                ).fetchone()
                if before is None:
                    raise InfrastructureError("UI settings are unavailable.")
                result = {"language": language}
                connection.execute("UPDATE ui_settings SET language = ? WHERE id = 1", (language,))
                SQLiteAuditSink(self.database).append_in_transaction(connection, AuditEvent.create(
                    context=context, action="ui.settings.update", target_type="ui_settings",
                    target_id="1", before={"language": before["language"]}, after=result,
                ))
                idem.complete_in_transaction(
                    connection, record_id=reservation.record_id, response=result,
                )
            return result
        except sqlite3.Error as exc:
            raise InfrastructureError("Unable to persist UI settings.") from exc
