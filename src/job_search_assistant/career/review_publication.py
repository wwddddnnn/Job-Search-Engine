"""Pure selection and immutable fact construction for partial publication."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from job_search_assistant.career.extraction import DraftEvidence
from job_search_assistant.career.review import (
    FIELDS, ReviewDraft, ReviewItem, ReviewItemKind, ReviewSource,
)
from job_search_assistant.career.store import ProfileVersionFacts
from job_search_assistant.career.types import (
    EvidenceSourceType, Experience, ExperienceAchievement, ExperienceEvidence,
    ExperienceSkill, ProfileVersion, Skill, VerificationStatus, require_verified_profile_facts,
)
from job_search_assistant.core.errors import ConflictError, InfrastructureError, ValidationError


def item_evidence(item: ReviewItem, facts: ProfileVersionFacts) -> tuple[ExperienceEvidence, ...]:
    if item.kind is ReviewItemKind.ACHIEVEMENT:
        return tuple(e for e in facts.evidence if e.experience_achievement_id == item.published_id)
    if item.kind is ReviewItemKind.SKILL:
        specific = tuple(e for e in facts.evidence if e.experience_skill_id == item.published_id)
        if specific:
            return specific
        association = next(
            (s for s in facts.experience_skills if s.id == item.published_id), None,
        )
        if association is None:
            raise ConflictError("The published skill association is missing from the base.")
        experience_id = association.experience_id
    else:
        experience_id = item.published_id
    return tuple(e for e in facts.evidence if e.experience_id == experience_id
                 and e.experience_achievement_id is None and e.experience_skill_id is None)


def seed_items(facts: ProfileVersionFacts) -> tuple[ReviewItem, ...]:
    """Create editable identities for an existing version without changing its facts."""
    require_verified_profile_facts(
        experiences=facts.experiences, achievements=facts.achievements,
        experience_skills=facts.experience_skills, evidence=facts.evidence,
    )
    result = []
    parent_ids = {e.id: str(uuid4()) for e in facts.experiences}
    skills = {s.id: s for s in facts.skills}
    groups = (
        (ReviewItemKind.EXPERIENCE, facts.experiences),
        (ReviewItemKind.ACHIEVEMENT, facts.achievements),
        (ReviewItemKind.SKILL, facts.experience_skills),
    )
    for kind, values in groups:
        for fact in values:
            fields = {name: getattr(fact, name) for name in FIELDS[kind]
                      if name != "canonical_name"}
            if kind is ReviewItemKind.SKILL:
                skill = skills.get(fact.skill_id)
                if skill is None:
                    raise InfrastructureError("A published skill association references no skill.")
                fields["canonical_name"] = skill.canonical_name
            if kind is not ReviewItemKind.EXPERIENCE and fact.experience_id not in parent_ids:
                raise InfrastructureError("A published child references no experience.")
            item = ReviewItem(
                id=parent_ids[fact.id] if kind is ReviewItemKind.EXPERIENCE else str(uuid4()),
                kind=kind, fields=fields,
                parent_id=None if kind is ReviewItemKind.EXPERIENCE
                else parent_ids[fact.experience_id],
                published_id=fact.id, dirty=False, status=VerificationStatus.VERIFIED,
                confirmed_at=fact.created_at,
            )
            evidence = item_evidence(item, facts)
            sources = tuple(
                ReviewSource(e.source_document_id, DraftEvidence(
                    source_excerpt=e.source_excerpt, source_locator=e.source_locator,
                    confidence=e.confidence,
                )) for e in evidence
                if e.source_type is EvidenceSourceType.RESUME_DOCUMENT
            )
            result.append(replace(item, sources=sources))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ReviewPublication:
    draft: ReviewDraft
    facts: ProfileVersionFacts
    summary: dict[str, Any]


def prepare_publication(
    draft: ReviewDraft, base: ProfileVersionFacts | None, *, allow_empty: bool = False,
) -> ReviewPublication:
    """Overlay confirmed changes only; unconfirmed modifications retain baseline facts."""
    if (base.profile_version.id if base else None) != draft.base_version_id:
        raise ConflictError("Draft base version does not match the published profile.")
    baseline = {item.published_id: item for item in seed_items(base)} if base else {}
    chosen: dict[str, ReviewItem] = {}
    applied: set[str] = set()
    removed: set[str] = set()
    changes: dict[str, list[dict[str, Any]]] = {"added": [], "modified": [], "deleted": []}
    deleted_parents = {
        item.id for item in draft.items if item.kind is ReviewItemKind.EXPERIENCE
        and item.dirty and item.deleted and item.status is VerificationStatus.VERIFIED
    }
    for item in draft.items:
        old = baseline.get(item.published_id)
        if item.published_id is not None and (old is None or old.kind is not item.kind):
            raise ConflictError("A draft item no longer belongs to its base version.")
        confirmed = item.dirty and item.status is VerificationStatus.VERIFIED
        if item.id in deleted_parents or item.parent_id in deleted_parents:
            if old:
                removed.add(item.id)
                changes["deleted"].append(_summary(item, old))
            if item.id in deleted_parents:
                applied.add(item.id)
            continue
        if confirmed and item.deleted:
            applied.add(item.id)
            if old:
                removed.add(item.id)
                changes["deleted"].append(_summary(item, old))
            continue
        if confirmed:
            chosen[item.id] = item
            applied.add(item.id)
            changes["modified" if old else "added"].append(_summary(item, item))
        elif old:
            chosen[item.id] = replace(old, id=item.id, parent_id=item.parent_id)
    # A confirmed child of a still-unpublished experience remains pending.
    blocked = {key for key, item in chosen.items()
               if item.parent_id is not None and item.parent_id not in chosen}
    for key in blocked:
        del chosen[key]
        applied.discard(key)
    for category in ("added", "modified"):
        changes[category] = [
            entry for entry in changes[category] if entry["item_id"] not in blocked
        ]
    if not allow_empty and not any(changes.values()):
        raise ValidationError("There are no confirmed publishable changes.")
    now = datetime.now(UTC)
    version = ProfileVersion(
        id=str(uuid4()), profile_id=draft.profile_id,
        version=base.profile_version.version + 1 if base else 1, created_at=now,
        source_summary={"review_draft_id": draft.id, "review_version": draft.version, **changes},
    )
    ids = {key: str(uuid4()) for key in chosen}
    experiences, achievements, skills, associations, evidence = [], [], [], [], []
    old_experiences = {e.id: e for e in base.experiences} if base else {}
    old_achievements = {a.id: a for a in base.achievements} if base else {}
    old_associations = {s.id: s for s in base.experience_skills} if base else {}
    old_skills = {s.id: s for s in base.skills} if base else {}
    for key, item in chosen.items():
        fact_id = ids[key]
        parent_id = ids[item.parent_id] if item.parent_id else fact_id
        common = dict(id=fact_id, verification_status=VerificationStatus.VERIFIED, created_at=now)
        if key not in applied:
            if item.kind is ReviewItemKind.EXPERIENCE:
                old = old_experiences.get(item.published_id)
                if old is None:
                    raise ConflictError("The published experience is missing from the base.")
                experiences.append(replace(old, id=fact_id, profile_version_id=version.id))
            elif item.kind is ReviewItemKind.ACHIEVEMENT:
                old = old_achievements.get(item.published_id)
                if old is None:
                    raise ConflictError("The published achievement is missing from the base.")
                achievements.append(replace(old, id=fact_id, experience_id=parent_id))
            else:
                old = old_associations.get(item.published_id)
                if old is None:
                    raise ConflictError("The published skill association is missing from the base.")
                skill = old_skills.get(old.skill_id)
                if skill is None:
                    raise InfrastructureError("A published skill association references no skill.")
                skills.append(skill)
                associations.append(replace(old, id=fact_id, experience_id=parent_id))
        elif item.kind is ReviewItemKind.EXPERIENCE:
            experiences.append(Experience(**common, profile_version_id=version.id, **item.fields))
        elif item.kind is ReviewItemKind.ACHIEVEMENT:
            achievements.append(ExperienceAchievement(
                **common, experience_id=parent_id, **item.fields,
                metric_user_confirmed_at=item.confirmed_at if item.fields["metric_value"]
                is not None else None,
            ))
        else:
            skill = Skill(id=str(uuid4()), created_at=now,
                          canonical_name=item.fields["canonical_name"]
                          or item.fields["raw_skill_name"])
            skills.append(skill)
            associations.append(ExperienceSkill(
                **common, experience_id=parent_id, skill_id=skill.id,
                raw_skill_name=item.fields["raw_skill_name"],
                proficiency=item.fields["proficiency"],
            ))
        target = dict(
            experience_id=parent_id,
            experience_achievement_id=fact_id if item.kind is ReviewItemKind.ACHIEVEMENT else None,
            experience_skill_id=fact_id if item.kind is ReviewItemKind.SKILL else None,
        )
        if key not in applied and base:
            evidence.extend(
                replace(e, id=str(uuid4()), **target) for e in item_evidence(item, base)
            )
        elif item.sources:
            for source in item.sources:
                evidence.append(ExperienceEvidence(
                    id=str(uuid4()), **target, source_type=EvidenceSourceType.RESUME_DOCUMENT,
                    source_document_id=source.document_id,
                    source_excerpt=source.reference.source_excerpt,
                    source_locator=source.reference.source_locator,
                    confidence=source.reference.confidence,
                    verification_status=VerificationStatus.VERIFIED, user_verified=True,
                    verified_at=item.confirmed_at, created_at=now,
                ))
        else:
            evidence.append(ExperienceEvidence(
                id=str(uuid4()), **target, source_type=EvidenceSourceType.USER_ASSERTION,
                source_excerpt="; ".join(str(v) for v in item.fields.values() if v is not None),
                verification_status=VerificationStatus.VERIFIED, user_verified=True,
                verified_at=item.confirmed_at, created_at=now,
            ))
    facts = ProfileVersionFacts(version, tuple(experiences), tuple(achievements), tuple(skills),
                                tuple(associations), tuple(evidence))
    require_verified_profile_facts(
        experiences=facts.experiences, achievements=facts.achievements,
        experience_skills=facts.experience_skills, evidence=facts.evidence,
    )
    updated = []
    for item in draft.items:
        published_id = ids.get(item.id)
        if item.id in removed:
            published_id = None
        updated.append(replace(item, published_id=published_id,
                               dirty=False if item.id in applied else item.dirty))
    return ReviewPublication(
        replace(draft, version=draft.version + 1, base_version_id=version.id, items=tuple(updated)),
        facts, changes,
    )


def _summary(identity: ReviewItem, content: ReviewItem) -> dict[str, Any]:
    return {"item_id": identity.id, "kind": identity.kind.value, "fields": dict(content.fields)}
