"""Backend-only manual review commands. A successful return acknowledges durable saving."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Mapping
from uuid import uuid4

from job_search_assistant.career.review import (
    ReviewDraft, ReviewItem, ReviewItemKind, ReviewSource, identifier,
)
from job_search_assistant.career.review_publication import prepare_publication, seed_items
from job_search_assistant.career.review_store import ReviewStore, ReviewTransaction
from job_search_assistant.career.types import CareerProfile
from job_search_assistant.core.audit import AuditEvent
from job_search_assistant.core.context import RequestContext
from job_search_assistant.core.errors import (
    ApplicationError, ConflictError, NotFoundError, ValidationError,
)


@dataclass(slots=True)
class CareerReviewService:
    """All manual-review writes require context, idempotency and the last saved version.

    The adapter owns unsaved/saving/failed input state and retains input on errors.
    Only a successful command response is a saved acknowledgement. A replay returns
    the original version, so clients must not replace a newer view with an older ack.
    """

    store: ReviewStore

    def create_draft(
        self, *, profile_id: str, idempotency_key: str, context: RequestContext,
        display_name: str | None = None,
    ) -> ReviewDraft:
        """Open a persistent draft, optionally creating a profile with no published version."""
        profile_id = identifier(profile_id, "profile_id")
        if display_name is not None:
            display_name = identifier(display_name, "display_name")
        request = {"profile_id": profile_id, "display_name": display_name}

        def operation(tx: ReviewTransaction) -> Mapping[str, Any]:
            try:
                profile = tx.get_profile(profile_id)
            except NotFoundError:
                if display_name is None:
                    raise ValidationError("display_name is required when creating a profile.")
                now = datetime.now(UTC)
                profile = CareerProfile(profile_id, context.actor_id, display_name, now, now)
                tx.create_profile(profile)
            draft = tx.find_draft(profile_id)
            if draft is None:
                base = (tx.get_facts(profile.current_version_id)
                        if profile.current_version_id else None)
                draft = ReviewDraft(str(uuid4()), profile_id, 1, profile.current_version_id,
                                    seed_items(base) if base else ())
                tx.save_draft(draft, None)
                self._audit(tx, context, "create", None, draft)
            return draft.to_dict()

        return ReviewDraft.from_dict(self.store.execute_review_command(
            action="career.review.create", request=request, idempotency_key=idempotency_key,
            context=context, operation=operation,
        ))

    def get_draft(self, *, draft_id: str) -> ReviewDraft:
        return self.store.get_review_draft(identifier(draft_id, "draft_id"))

    def create_item(
        self, *, draft_id: str, expected_version: int, kind: ReviewItemKind,
        fields: Mapping[str, Any], idempotency_key: str, context: RequestContext,
        parent_id: str | None = None, sources: tuple[ReviewSource, ...] = (),
    ) -> ReviewDraft:
        # Validate before reservation; item IDs are generated only for the successful write.
        candidate = ReviewItem("pending", kind, fields, parent_id, sources)
        if any(not source.reference.source_locator for source in sources):
            raise ValidationError("New review sources require a span/offset locator.")
        return self._change(
            draft_id, expected_version, "create_item", idempotency_key, context,
            {"item": candidate.to_dict()},
            lambda draft: draft.with_item(ReviewItem(
                str(uuid4()), candidate.kind, candidate.fields, parent_id, sources,
            )),
        )

    def edit_item(
        self, *, draft_id: str, expected_version: int, item_id: str,
        changes: Mapping[str, Any], idempotency_key: str, context: RequestContext,
    ) -> ReviewDraft:
        return self._change(
            draft_id, expected_version, "edit", idempotency_key, context,
            {"item_id": item_id, "changes": dict(changes)},
            lambda draft: draft.with_item(self._item(draft, item_id).edit(changes)),
        )

    def decide_item(
        self, *, draft_id: str, expected_version: int, item_id: str, decision: str,
        idempotency_key: str, context: RequestContext,
    ) -> ReviewDraft:
        """confirm/reject/clarify/confirm_delete applies only to the named item."""
        return self._change(
            draft_id, expected_version, decision, idempotency_key, context,
            {"item_id": item_id},
            lambda draft: draft.with_item(
                self._item(draft, item_id).decide(decision, datetime.now(UTC)),
            ),
        )

    def delete_item(
        self, *, draft_id: str, expected_version: int, item_id: str,
        idempotency_key: str, context: RequestContext,
    ) -> ReviewDraft:
        return self._change(
            draft_id, expected_version, "delete", idempotency_key, context,
            {"item_id": item_id},
            lambda draft: draft.with_item(self._item(draft, item_id).delete()),
        )

    def restore_item(
        self, *, draft_id: str, expected_version: int, item_id: str,
        idempotency_key: str, context: RequestContext,
    ) -> ReviewDraft:
        return self._change(
            draft_id, expected_version, "restore", idempotency_key, context,
            {"item_id": item_id},
            lambda draft: draft.with_item(self._item(draft, item_id).restore()),
        )

    def preview_publication(
        self, *, draft_id: str, expected_version: int, base_version_id: str | None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Read-only summary bound to a saved draft/base; publication rechecks both."""
        try:
            draft = self.get_draft(draft_id=draft_id)
            draft.check_version(expected_version)
            profile = self.store.get_review_profile(draft.profile_id)
            self._check_base(draft, profile, base_version_id)
            base = self.store.get_review_base_facts(base_version_id) if base_version_id else None
            publication = prepare_publication(draft, base, allow_empty=True)
            return {"draft_id": draft_id, "draft_version": expected_version,
                    "base_version_id": base_version_id, "summary": publication.summary,
                    "content": [{"kind": item.kind.value, "fields": dict(item.fields)}
                                for item in seed_items(publication.facts)]}
        except ApplicationError as exc:
            if context is not None:
                exc.with_correlation_id(context.correlation_id)
            raise

    def publish(
        self, *, draft_id: str, expected_version: int, base_version_id: str | None,
        idempotency_key: str, context: RequestContext,
    ) -> dict[str, Any]:
        """Publish the exact saved view; return new profile/draft versions and deletion summary."""
        request = {"draft_id": draft_id, "expected_version": expected_version,
                   "base_version_id": base_version_id}

        def operation(tx: ReviewTransaction) -> Mapping[str, Any]:
            before = tx.get_draft(draft_id)
            before.check_version(expected_version)
            profile = tx.get_profile(before.profile_id)
            self._check_base(before, profile, base_version_id)
            base = tx.get_facts(base_version_id) if base_version_id else None
            publication = prepare_publication(before, base)
            tx.publish(profile, publication.facts)
            tx.save_draft(publication.draft, expected_version)
            self._audit(tx, context, "publish", before, publication.draft,
                        metadata=publication.summary)
            return {
                "draft": publication.draft.to_dict(),
                "profile_version_id": publication.facts.profile_version.id,
                "version": publication.facts.profile_version.version,
                "summary": publication.summary,
            }

        return self.store.execute_review_command(
            action="career.review.publish", request=request, idempotency_key=idempotency_key,
            context=context, operation=operation,
        )

    def _change(
        self, draft_id: str, expected_version: int, action: str, key: str,
        context: RequestContext, payload: Mapping[str, Any],
        change: Callable[[ReviewDraft], ReviewDraft],
    ) -> ReviewDraft:
        request = {"draft_id": draft_id, "expected_version": expected_version, **payload}

        def operation(tx: ReviewTransaction) -> Mapping[str, Any]:
            before = tx.get_draft(draft_id)
            before.check_version(expected_version)
            after = change(before)
            for item in after.items:
                for source in item.sources:
                    tx.require_document(source.document_id)
            tx.save_draft(after, expected_version)
            self._audit(tx, context, action, before, after)
            return after.to_dict()

        return ReviewDraft.from_dict(self.store.execute_review_command(
            action=f"career.review.{action}", request=request, idempotency_key=key,
            context=context, operation=operation,
        ))

    @staticmethod
    def _check_base(
        draft: ReviewDraft, profile: CareerProfile, base_version_id: str | None,
    ) -> None:
        if (base_version_id != profile.current_version_id
                or base_version_id != draft.base_version_id):
            raise ConflictError("Publication base is stale; reload the current profile version.")

    @staticmethod
    def _item(draft: ReviewDraft, item_id: str) -> ReviewItem:
        for item in draft.items:
            if item.id == item_id:
                return item
        raise NotFoundError("review_item", item_id)

    @staticmethod
    def _audit(
        tx: ReviewTransaction, context: RequestContext, action: str,
        before: ReviewDraft | None, after: ReviewDraft, metadata: Mapping[str, Any] | None = None,
    ) -> None:
        tx.audit(AuditEvent.create(
            context=context, action=f"career.review.{action}", target_type="review_draft",
            target_id=after.id, before=before.to_dict() if before else None,
            after=after.to_dict(), metadata=metadata,
        ))
