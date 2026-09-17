"""Manual review aggregates, independent of extraction runs and published facts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
import math
from types import MappingProxyType
from typing import Any, Mapping

from job_search_assistant.career.extraction import (
    DraftEvidence, ExperienceDraft, ExperienceAchievementDraft, ExperienceSkillDraft,
)
from job_search_assistant.career.types import VerificationStatus
from job_search_assistant.core.errors import ConflictError, InvalidStateError, ValidationError


class ReviewItemKind(StrEnum):
    EXPERIENCE = "experience"
    ACHIEVEMENT = "achievement"
    SKILL = "skill"


FIELDS = {
    ReviewItemKind.EXPERIENCE: ("organization", "role", "date_range", "summary"),
    ReviewItemKind.ACHIEVEMENT: ("action_text", "outcome_text", "metric_value", "metric_unit"),
    ReviewItemKind.SKILL: ("raw_skill_name", "canonical_name", "proficiency"),
}


def identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} must be a non-blank string.")
    return value.strip()


def require_version(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError("expected_version must be a positive integer.")


@dataclass(frozen=True, slots=True)
class ReviewSource:
    """Document binding for the existing DraftEvidence reference value.

    Pending references live inline on the review item, just as extraction draft
    references do. Publication materializes the existing ExperienceEvidence model;
    there is no second evidence repository or evidence table.
    """

    document_id: str
    reference: DraftEvidence

    def __post_init__(self) -> None:
        object.__setattr__(self, "document_id", identifier(self.document_id, "document_id"))
        if not isinstance(self.reference, DraftEvidence):
            raise ValidationError("Review sources require a DraftEvidence reference.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "source_excerpt": self.reference.source_excerpt,
            "source_locator": self.reference.source_locator,
            "confidence": self.reference.confidence,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReviewSource:
        values = dict(value)
        document_id = values.pop("document_id")
        return cls(document_id, DraftEvidence(**values))


@dataclass(frozen=True, slots=True)
class ReviewItem:
    id: str
    kind: ReviewItemKind
    fields: Mapping[str, Any]
    parent_id: str | None = None
    sources: tuple[ReviewSource, ...] = ()
    status: VerificationStatus = VerificationStatus.DRAFT
    deleted: bool = False
    dirty: bool = True
    published_id: str | None = None
    confirmed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", identifier(self.id, "item_id"))
        try:
            kind = ReviewItemKind(self.kind)
            status = VerificationStatus(self.status)
        except (ValueError, TypeError) as exc:
            raise ValidationError("Invalid review item kind or status.") from exc
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "status", status)
        if (kind is ReviewItemKind.EXPERIENCE) != (self.parent_id is None):
            raise ValidationError("Only experiences may have no parent experience.")
        if not isinstance(self.fields, Mapping) or set(self.fields) - set(FIELDS[kind]):
            raise ValidationError("Unknown fields for review item kind.")
        model = {
            ReviewItemKind.EXPERIENCE: ExperienceDraft,
            ReviewItemKind.ACHIEVEMENT: ExperienceAchievementDraft,
            ReviewItemKind.SKILL: ExperienceSkillDraft,
        }[kind]
        try:
            validated = model(**self.fields)
        except TypeError as exc:
            raise ValidationError("Missing required review fields.") from exc
        fields = {name: getattr(validated, name) for name in FIELDS[kind]}
        metric = fields.get("metric_value")
        if metric is not None and not math.isfinite(metric):
            raise ValidationError("metric_value must be finite.")
        object.__setattr__(self, "fields", MappingProxyType(fields))
        if not isinstance(self.sources, tuple) or not all(
            isinstance(source, ReviewSource) for source in self.sources
        ):
            raise ValidationError("sources must be a tuple of ReviewSource references.")
        if not isinstance(self.deleted, bool) or not isinstance(self.dirty, bool):
            raise ValidationError("Review flags must be booleans.")
        if self.confirmed_at is not None and not isinstance(self.confirmed_at, datetime):
            raise ValidationError("confirmed_at must be a datetime.")
        if (status is VerificationStatus.VERIFIED) != (self.confirmed_at is not None):
            raise ValidationError("Only an explicitly confirmed item has a confirmation time.")

    def edit(self, changes: Mapping[str, Any]) -> ReviewItem:
        self._require_active("edit")
        if not isinstance(changes, Mapping) or not changes:
            raise ValidationError("An edit requires changed fields.")
        return replace(
            self, fields={**self.fields, **changes}, status=VerificationStatus.DRAFT,
            confirmed_at=None, dirty=True,
        )

    def decide(self, decision: str, now: datetime) -> ReviewItem:
        if decision == "confirm_delete":
            if not self.deleted:
                self._invalid(decision)
            return replace(self, status=VerificationStatus.VERIFIED, confirmed_at=now, dirty=True)
        self._require_active(decision)
        statuses = {
            "confirm": VerificationStatus.VERIFIED,
            "reject": VerificationStatus.REJECTED,
            "clarify": VerificationStatus.NEEDS_CLARIFICATION,
        }
        if decision not in statuses:
            raise ValidationError("Unknown review decision.")
        return replace(
            self, status=statuses[decision], dirty=True,
            confirmed_at=now if decision == "confirm" else None,
        )

    def delete(self) -> ReviewItem:
        self._require_active("delete")
        return replace(
            self, deleted=True, status=VerificationStatus.DRAFT, confirmed_at=None, dirty=True,
        )

    def restore(self) -> ReviewItem:
        if not self.deleted:
            self._invalid("restore")
        return replace(
            self, deleted=False, status=VerificationStatus.DRAFT, confirmed_at=None, dirty=True,
        )

    def _require_active(self, action: str) -> None:
        if self.deleted:
            self._invalid(action)

    def _invalid(self, action: str) -> None:
        raise InvalidStateError("review_item", self.id, "deleted" if self.deleted else "active",
                                action)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind.value, "fields": dict(self.fields),
            "parent_id": self.parent_id, "sources": [source.to_dict() for source in self.sources],
            "status": self.status.value, "deleted": self.deleted, "dirty": self.dirty,
            "published_id": self.published_id,
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReviewItem:
        values = dict(value)
        values["sources"] = tuple(ReviewSource.from_dict(s) for s in values["sources"])
        values["confirmed_at"] = (
            datetime.fromisoformat(values["confirmed_at"]) if values["confirmed_at"] else None
        )
        return cls(**values)


@dataclass(frozen=True, slots=True)
class ReviewDraft:
    id: str
    profile_id: str
    version: int
    base_version_id: str | None
    items: tuple[ReviewItem, ...] = ()

    def check_version(self, expected_version: int) -> None:
        require_version(expected_version)
        if expected_version != self.version:
            raise ConflictError("Review draft has changed; reload before saving or publishing.",
                                details={"expected": expected_version, "actual": self.version})

    def with_item(self, item: ReviewItem) -> ReviewDraft:
        items = {value.id: value for value in self.items}
        if item.parent_id is not None:
            parent = items.get(item.parent_id)
            if parent is None or parent.kind is not ReviewItemKind.EXPERIENCE or parent.deleted:
                raise ValidationError("The parent must be an active experience in this draft.")
        items[item.id] = item
        return replace(self, version=self.version + 1, items=tuple(items.values()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "profile_id": self.profile_id, "version": self.version,
            "base_version_id": self.base_version_id,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReviewDraft:
        values = dict(value)
        values["items"] = tuple(ReviewItem.from_dict(item) for item in values["items"])
        return cls(**values)
