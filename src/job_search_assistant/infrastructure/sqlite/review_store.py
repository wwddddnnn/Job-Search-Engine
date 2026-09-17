"""SQLite implementation of the atomic review command boundary."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Mapping

from job_search_assistant.career.review import FIELDS, ReviewDraft, ReviewItem, ReviewItemKind
from job_search_assistant.career.review_store import ReviewTransaction
from job_search_assistant.career.store import ProfileVersionFacts
from job_search_assistant.career.types import CareerProfile, require_verified_profile_facts
from job_search_assistant.core.audit import AuditEvent
from job_search_assistant.core.context import RequestContext
from job_search_assistant.core.errors import (
    ApplicationError, ConflictError, InfrastructureError, NotFoundError, ValidationError,
)
from job_search_assistant.core.idempotency import IdempotencyReservationState, hash_request
from job_search_assistant.infrastructure.sqlite.audit_sink import SQLiteAuditSink
from job_search_assistant.infrastructure.sqlite.career_store import SQLiteCareerStore
from job_search_assistant.infrastructure.sqlite.database import SQLiteDatabase
from job_search_assistant.infrastructure.sqlite.idempotency_store import SQLiteIdempotencyStore


_CONTENT_COLUMNS = tuple(name for fields in FIELDS.values() for name in fields)


class SQLiteReviewStore:
    """Internal persistence adapter; delivery adapters must use CareerReviewService."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database
        self._career = SQLiteCareerStore(database)
        self._audit = SQLiteAuditSink(database)
        self._idempotency = SQLiteIdempotencyStore(database)

    def get_review_draft(self, draft_id: str) -> ReviewDraft:
        with self._database.transaction() as connection:
            return _SQLiteReviewTransaction(self, connection).get_draft(draft_id)

    def get_review_profile(self, profile_id: str) -> CareerProfile:
        return self._career.get_career_profile(profile_id=profile_id)

    def get_review_base_facts(self, version_id: str) -> ProfileVersionFacts:
        return self._career.get_profile_version_facts(profile_version_id=version_id)

    def execute_review_command(
        self, *, action: str, request: Mapping[str, Any], idempotency_key: str,
        context: RequestContext,
        operation: Callable[[ReviewTransaction], Mapping[str, Any]],
    ) -> dict[str, Any]:
        try:
            if not isinstance(idempotency_key, str) or not idempotency_key.strip():
                raise ValidationError("idempotency_key must not be blank.")
            if not isinstance(context, RequestContext) or not all(
                isinstance(value, str) and value.strip()
                for value in (context.actor_id, context.correlation_id, context.source)
            ):
                raise ValidationError("Writes require an actor, correlation ID and source.")
            request_hash = hash_request({"actor_id": context.actor_id, **request})
            with self._database.transaction(immediate=True) as connection:
                reservation = self._idempotency.reserve_in_transaction(
                    connection, scope=action, idempotency_key=idempotency_key.strip(),
                    request_hash=request_hash, context=context,
                )
                if reservation.state is IdempotencyReservationState.COMPLETED:
                    if reservation.response is None:
                        raise InfrastructureError("Completed review command has no response.")
                    return reservation.response
                result = dict(operation(_SQLiteReviewTransaction(self, connection)))
                self._idempotency.complete_in_transaction(
                    connection, record_id=reservation.record_id, response=result,
                )
            return result
        except ApplicationError as exc:
            raise exc.with_correlation_id(getattr(context, "correlation_id", None))
        except sqlite3.Error as exc:
            raise InfrastructureError("Unable to persist review command.").with_correlation_id(
                context.correlation_id,
            ) from exc


class _SQLiteReviewTransaction:
    # SQLiteCareerStore private helpers are a same-layer internal contract, not a
    # delivery-adapter API. CareerStore internal refactors must update this reuse.
    def __init__(self, store: SQLiteReviewStore, connection: sqlite3.Connection) -> None:
        self.store = store
        self.connection = connection

    def get_draft(self, draft_id: str) -> ReviewDraft:
        row = self.connection.execute(
            "SELECT * FROM career_review_drafts WHERE id = ?", (draft_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("review_draft", draft_id)
        items = []
        for item in self.connection.execute(
            "SELECT * FROM career_review_items WHERE draft_id = ? ORDER BY rowid", (draft_id,),
        ):
            kind = ReviewItemKind(item["kind"])
            items.append(ReviewItem.from_dict({
                "id": item["id"], "kind": kind, "parent_id": item["parent_id"],
                "fields": {name: item[name] for name in FIELDS[kind]},
                "sources": json.loads(item["sources_json"]), "status": item["status"],
                "deleted": bool(item["deleted"]), "dirty": bool(item["dirty"]),
                "published_id": item["published_id"], "confirmed_at": item["confirmed_at"],
            }))
        return ReviewDraft(row["id"], row["profile_id"], row["version"], row["base_version_id"],
                           tuple(items))

    def find_draft(self, profile_id: str) -> ReviewDraft | None:
        row = self.connection.execute(
            "SELECT id FROM career_review_drafts WHERE profile_id = ?", (profile_id,),
        ).fetchone()
        return self.get_draft(row["id"]) if row else None

    def save_draft(self, draft: ReviewDraft, expected_version: int | None) -> None:
        if expected_version is None:
            self.connection.execute(
                "INSERT INTO career_review_drafts (id, profile_id, version, base_version_id) "
                "VALUES (?, ?, ?, ?)",
                (draft.id, draft.profile_id, draft.version, draft.base_version_id),
            )
        else:
            if draft.version != expected_version + 1:
                raise ValidationError("A saved draft version must increment by one.")
            cursor = self.connection.execute(
                "UPDATE career_review_drafts SET version = ?, base_version_id = ? "
                "WHERE id = ? AND profile_id = ? AND version = ?",
                (draft.version, draft.base_version_id, draft.id, draft.profile_id,
                 expected_version),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Review draft version has changed.")
        columns = ("id", "draft_id", "kind", "parent_id", *_CONTENT_COLUMNS, "sources_json",
                   "status", "deleted", "dirty", "published_id", "confirmed_at")
        placeholders = ", ".join("?" for _ in columns)
        assignments = ", ".join(f"{name} = excluded.{name}" for name in columns[4:])
        for item in draft.items:
            for source in item.sources:
                self.require_document(source.document_id)
            values = (
                item.id, draft.id, item.kind.value, item.parent_id,
                *(item.fields.get(name) for name in _CONTENT_COLUMNS),
                json.dumps([source.to_dict() for source in item.sources], ensure_ascii=False),
                item.status.value, int(item.deleted), int(item.dirty), item.published_id,
                item.confirmed_at.isoformat() if item.confirmed_at else None,
            )
            self.connection.execute(
                f"INSERT INTO career_review_items ({', '.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {assignments}", values,
            )

    def get_profile(self, profile_id: str) -> CareerProfile:
        return self.store._career._load_career_profile(self.connection, profile_id)

    def create_profile(self, profile: CareerProfile) -> None:
        self.connection.execute(
            "INSERT INTO career_profiles "
            "(id, owner_id, display_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (profile.id, profile.owner_id, profile.display_name, profile.created_at.isoformat(),
             profile.updated_at.isoformat()),
        )

    def get_facts(self, version_id: str) -> ProfileVersionFacts:
        # Immutable facts can be read on the existing Career read port. The command
        # transaction holds the writer lock and has checked the current pointer.
        # WAL permits this separate reader of committed, immutable version facts.
        # This path must remain read-only: no writes or dependency on uncommitted data.
        return self.store._career.get_profile_version_facts(profile_version_id=version_id)

    def require_document(self, document_id: str) -> None:
        self.store._career._load_resume_document(self.connection, document_id)

    def publish(self, profile: CareerProfile, facts: ProfileVersionFacts) -> None:
        current = self.get_profile(profile.id)
        if current != profile:
            raise ConflictError("Profile publication base changed.")
        previous = self.store._career._load_profile_version(
            self.connection, profile.current_version_id,
        ) if profile.current_version_id else None
        after = profile.with_published_version(
            profile_version=facts.profile_version, previous_version=previous,
        )
        require_verified_profile_facts(
            experiences=facts.experiences, achievements=facts.achievements,
            experience_skills=facts.experience_skills, evidence=facts.evidence,
        )
        self.store._career._insert_profile_version(self.connection, facts.profile_version)
        self.store._career._insert_profile_facts(
            self.connection, profile_version=facts.profile_version, experiences=facts.experiences,
            achievements=facts.achievements, skills=facts.skills,
            experience_skills=facts.experience_skills, evidence=facts.evidence,
        )
        self.store._career._write_career_profile(
            self.connection, after, expected_current_version_id=profile.current_version_id,
        )

    def audit(self, event: AuditEvent) -> None:
        self.store._audit.append_in_transaction(self.connection, event)
