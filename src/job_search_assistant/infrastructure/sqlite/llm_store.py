"""Transactional non-secret configuration persistence."""

from contextlib import contextmanager
import sqlite3

from job_search_assistant.core.audit import AuditEvent
from job_search_assistant.core.errors import InfrastructureError, NotFoundError
from job_search_assistant.core.idempotency import IdempotencyReservationState
from job_search_assistant.infrastructure.sqlite.audit_sink import SQLiteAuditSink
from job_search_assistant.infrastructure.sqlite.idempotency_store import SQLiteIdempotencyStore


class SQLiteLLMStore:
    def __init__(self, database):
        self.database = database

    @contextmanager
    def transaction(self):
        connection = self.database.connect()
        tx = LLMTransaction(self.database, connection)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield tx
            connection.commit()
        except Exception as exc:
            try:
                # Restore external state while still holding the write transaction lock.
                if tx.undo_secret:
                    tx.undo_secret()
            finally:
                connection.rollback()
            if isinstance(exc, sqlite3.Error):
                raise InfrastructureError("Unable to persist LLM configuration.") from None
            raise
        finally:
            connection.close()


class LLMTransaction:
    def __init__(self, database, connection):
        self.connection = connection
        self.undo_secret = None
        self.idem = SQLiteIdempotencyStore(database)
        self.audit = SQLiteAuditSink(database)

    def configs(self):
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM llm_configs ORDER BY created_at, id",
        )]

    def get(self, identifier):
        row = self.connection.execute(
            "SELECT * FROM llm_configs WHERE id = ?", (identifier,),
        ).fetchone()
        if row is None:
            raise NotFoundError("llm_config", identifier)
        return dict(row)

    def selection(self):
        return dict(self.connection.execute(
            "SELECT config_id FROM llm_selection WHERE id = 1",
        ).fetchone())

    def select(self, identifier):
        if identifier is not None:
            self.get(identifier)
        self.connection.execute(
            "UPDATE llm_selection SET config_id = ? WHERE id = 1", (identifier,),
        )

    def save(self, config):
        self.connection.execute(
            "INSERT INTO llm_configs (id, name, api_url, model, created_at, updated_at) "
            "VALUES (:id, :name, :api_url, :model, :created_at, :updated_at) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, api_url=excluded.api_url, "
            "model=excluded.model, updated_at=excluded.updated_at", config,
        )

    def delete(self, identifier):
        self.connection.execute("DELETE FROM llm_configs WHERE id = ?", (identifier,))

    def reserve(self, action, key, digest, context):
        reservation = self.idem.reserve_in_transaction(
            self.connection, scope="llm." + action, idempotency_key=key,
            request_hash=digest, context=context,
        )
        return reservation, reservation.state is IdempotencyReservationState.COMPLETED

    def complete(self, reservation, action, identifier, before, result, context):
        self.audit.append_in_transaction(self.connection, AuditEvent.create(
            context=context, action="llm." + action, target_type="llm_config",
            target_id=identifier or "selection", before=before, after=result,
        ))
        self.idem.complete_in_transaction(
            self.connection, record_id=reservation.record_id, response=result,
        )
