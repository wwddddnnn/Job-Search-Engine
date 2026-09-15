"""Schema-bound types for untrusted career-extraction drafts.

LLM output is intentionally parsed into a separate draft representation.  It
cannot create verified domain facts: a later confirmation use case is the only
place permitted to construct a published profile version.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from job_search_assistant.career.types import VerificationStatus
from job_search_assistant.core.errors import ValidationError


EXTRACTION_DRAFT_SCHEMA_VERSION = "career-extraction-v1"

_DRAFT_STATUSES = frozenset(
    {VerificationStatus.DRAFT, VerificationStatus.NEEDS_CLARIFICATION}
)


@dataclass(frozen=True, slots=True)
class DraftEvidence:
    """A text span or location proposed by the extractor as supporting context."""

    source_excerpt: str | None = None
    source_locator: str | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_excerpt", _optional_text(self.source_excerpt, "source_excerpt"))
        object.__setattr__(self, "source_locator", _optional_text(self.source_locator, "source_locator"))
        if self.source_excerpt is None and self.source_locator is None:
            raise ValidationError(
                "Draft evidence requires a source excerpt or source locator.",
                details={"item": "evidence"},
            )
        if self.confidence is not None:
            if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
                raise ValidationError("confidence must be numeric.", details={"field": "confidence"})
            confidence = float(self.confidence)
            if not 0.0 <= confidence <= 1.0:
                raise ValidationError(
                    "confidence must be between 0 and 1.",
                    details={"field": "confidence", "value": confidence},
                )
            object.__setattr__(self, "confidence", confidence)


@dataclass(frozen=True, slots=True)
class ExperienceAchievementDraft:
    """An unverified achievement proposed for one extracted experience."""

    action_text: str
    verification_status: VerificationStatus = VerificationStatus.NEEDS_CLARIFICATION
    outcome_text: str | None = None
    metric_value: float | None = None
    metric_unit: str | None = None
    evidence: tuple[DraftEvidence, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_text", _require_text(self.action_text, "action_text"))
        object.__setattr__(self, "outcome_text", _optional_text(self.outcome_text, "outcome_text"))
        object.__setattr__(self, "metric_unit", _optional_text(self.metric_unit, "metric_unit"))
        object.__setattr__(self, "verification_status", _as_draft_status(self.verification_status))
        object.__setattr__(self, "evidence", _as_evidence_tuple(self.evidence, "evidence"))
        if self.metric_value is not None:
            if isinstance(self.metric_value, bool) or not isinstance(self.metric_value, (int, float)):
                raise ValidationError("metric_value must be numeric.", details={"field": "metric_value"})
            object.__setattr__(self, "metric_value", float(self.metric_value))
        if self.metric_unit is not None and self.metric_value is None:
            raise ValidationError("metric_unit requires metric_value.", details={"field": "metric_unit"})
        _require_clarification_when_unsupported(
            self.verification_status,
            self.evidence,
            item_type="experience achievement",
        )


@dataclass(frozen=True, slots=True)
class ExperienceSkillDraft:
    """An unverified skill preserving extractor-provided wording."""

    raw_skill_name: str
    verification_status: VerificationStatus = VerificationStatus.NEEDS_CLARIFICATION
    canonical_name: str | None = None
    proficiency: str | None = None
    evidence: tuple[DraftEvidence, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_skill_name", _require_text(self.raw_skill_name, "raw_skill_name"))
        object.__setattr__(self, "canonical_name", _optional_text(self.canonical_name, "canonical_name"))
        object.__setattr__(self, "proficiency", _optional_text(self.proficiency, "proficiency"))
        object.__setattr__(self, "verification_status", _as_draft_status(self.verification_status))
        object.__setattr__(self, "evidence", _as_evidence_tuple(self.evidence, "evidence"))
        _require_clarification_when_unsupported(
            self.verification_status,
            self.evidence,
            item_type="experience skill",
        )


@dataclass(frozen=True, slots=True)
class ExperienceDraft:
    """An unverified experience and its proposed facts from one model output."""

    organization: str
    role: str
    verification_status: VerificationStatus = VerificationStatus.NEEDS_CLARIFICATION
    date_range: str | None = None
    summary: str | None = None
    achievements: tuple[ExperienceAchievementDraft, ...] = ()
    skills: tuple[ExperienceSkillDraft, ...] = ()
    evidence: tuple[DraftEvidence, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "organization", _require_text(self.organization, "organization"))
        object.__setattr__(self, "role", _require_text(self.role, "role"))
        object.__setattr__(self, "date_range", _optional_text(self.date_range, "date_range"))
        object.__setattr__(self, "summary", _optional_text(self.summary, "summary"))
        object.__setattr__(self, "verification_status", _as_draft_status(self.verification_status))
        object.__setattr__(self, "achievements", _as_achievement_tuple(self.achievements))
        object.__setattr__(self, "skills", _as_skill_tuple(self.skills))
        object.__setattr__(self, "evidence", _as_evidence_tuple(self.evidence, "evidence"))
        _require_clarification_when_unsupported(
            self.verification_status,
            self.evidence,
            item_type="experience",
        )


@dataclass(frozen=True, slots=True)
class ExtractionDraft:
    """A complete, schema-validated extraction result that remains unverified."""

    schema_version: str
    experiences: tuple[ExperienceDraft, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, "schema_version"))
        object.__setattr__(self, "experiences", _as_experience_tuple(self.experiences))

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        expected_schema_version: str = EXTRACTION_DRAFT_SCHEMA_VERSION,
    ) -> "ExtractionDraft":
        """Validate model JSON against the stable draft schema and return typed data."""
        return validate_extraction_draft(payload, expected_schema_version=expected_schema_version)


def validate_extraction_draft(
    payload: Mapping[str, Any],
    *,
    expected_schema_version: str = EXTRACTION_DRAFT_SCHEMA_VERSION,
) -> ExtractionDraft:
    """Parse an LLM payload without granting it verified-fact authority.

    The validator is deliberately strict about unknown keys and types.  A
    prompt/schema revision must use a new schema version instead of silently
    changing the interpretation of historical extraction outputs.
    """
    root = _require_mapping(payload, "draft")
    _validate_keys(root, required={"schema_version", "experiences"}, optional=set(), location="draft")
    schema_version = _require_text(root["schema_version"], "schema_version")
    if schema_version != expected_schema_version:
        raise ValidationError(
            "Extraction draft schema version is unsupported.",
            details={"expected_schema_version": expected_schema_version, "actual_schema_version": schema_version},
        )
    return ExtractionDraft(
        schema_version=schema_version,
        experiences=tuple(
            _parse_experience(item, index=index)
            for index, item in enumerate(_require_sequence(root["experiences"], "experiences"))
        ),
    )


def validate_draft_schema(
    payload: Mapping[str, Any],
    *,
    expected_schema_version: str = EXTRACTION_DRAFT_SCHEMA_VERSION,
) -> ExtractionDraft:
    """Compatibility-friendly name for :func:`validate_extraction_draft`."""
    return validate_extraction_draft(payload, expected_schema_version=expected_schema_version)


def _parse_experience(value: object, *, index: int) -> ExperienceDraft:
    location = f"experiences[{index}]"
    item = _require_mapping(value, location)
    _validate_keys(
        item,
        required={"organization", "role"},
        optional={"date_range", "summary", "verification_status", "achievements", "skills", "evidence"},
        location=location,
    )
    return ExperienceDraft(
        organization=_require_text(item["organization"], f"{location}.organization"),
        role=_require_text(item["role"], f"{location}.role"),
        date_range=_optional_payload_text(item.get("date_range"), f"{location}.date_range"),
        summary=_optional_payload_text(item.get("summary"), f"{location}.summary"),
        verification_status=_parse_draft_status(item.get("verification_status"), location),
        achievements=tuple(
            _parse_achievement(child, experience_index=index, achievement_index=child_index)
            for child_index, child in enumerate(
                _require_sequence(item.get("achievements", ()), f"{location}.achievements")
            )
        ),
        skills=tuple(
            _parse_skill(child, experience_index=index, skill_index=child_index)
            for child_index, child in enumerate(_require_sequence(item.get("skills", ()), f"{location}.skills"))
        ),
        evidence=tuple(
            _parse_evidence(child, location=f"{location}.evidence[{child_index}]")
            for child_index, child in enumerate(_require_sequence(item.get("evidence", ()), f"{location}.evidence"))
        ),
    )


def _parse_achievement(
    value: object,
    *,
    experience_index: int,
    achievement_index: int,
) -> ExperienceAchievementDraft:
    location = f"experiences[{experience_index}].achievements[{achievement_index}]"
    item = _require_mapping(value, location)
    _validate_keys(
        item,
        required={"action_text"},
        optional={"outcome_text", "metric_value", "metric_unit", "verification_status", "evidence"},
        location=location,
    )
    return ExperienceAchievementDraft(
        action_text=_require_text(item["action_text"], f"{location}.action_text"),
        outcome_text=_optional_payload_text(item.get("outcome_text"), f"{location}.outcome_text"),
        metric_value=_optional_number(item.get("metric_value"), f"{location}.metric_value"),
        metric_unit=_optional_payload_text(item.get("metric_unit"), f"{location}.metric_unit"),
        verification_status=_parse_draft_status(item.get("verification_status"), location),
        evidence=tuple(
            _parse_evidence(child, location=f"{location}.evidence[{child_index}]")
            for child_index, child in enumerate(_require_sequence(item.get("evidence", ()), f"{location}.evidence"))
        ),
    )


def _parse_skill(value: object, *, experience_index: int, skill_index: int) -> ExperienceSkillDraft:
    location = f"experiences[{experience_index}].skills[{skill_index}]"
    item = _require_mapping(value, location)
    _validate_keys(
        item,
        required={"raw_skill_name"},
        optional={"canonical_name", "proficiency", "verification_status", "evidence"},
        location=location,
    )
    return ExperienceSkillDraft(
        raw_skill_name=_require_text(item["raw_skill_name"], f"{location}.raw_skill_name"),
        canonical_name=_optional_payload_text(item.get("canonical_name"), f"{location}.canonical_name"),
        proficiency=_optional_payload_text(item.get("proficiency"), f"{location}.proficiency"),
        verification_status=_parse_draft_status(item.get("verification_status"), location),
        evidence=tuple(
            _parse_evidence(child, location=f"{location}.evidence[{child_index}]")
            for child_index, child in enumerate(_require_sequence(item.get("evidence", ()), f"{location}.evidence"))
        ),
    )


def _parse_evidence(value: object, *, location: str) -> DraftEvidence:
    item = _require_mapping(value, location)
    _validate_keys(item, required=set(), optional={"source_excerpt", "source_locator", "confidence"}, location=location)
    return DraftEvidence(
        source_excerpt=_optional_payload_text(item.get("source_excerpt"), f"{location}.source_excerpt"),
        source_locator=_optional_payload_text(item.get("source_locator"), f"{location}.source_locator"),
        confidence=_optional_number(item.get("confidence"), f"{location}.confidence"),
    )


def _parse_draft_status(value: object, location: str) -> VerificationStatus:
    if value is None:
        return VerificationStatus.NEEDS_CLARIFICATION
    if not isinstance(value, str):
        raise ValidationError(
            "Draft verification_status must be a string.",
            details={"field": f"{location}.verification_status"},
        )
    try:
        return _as_draft_status(VerificationStatus(value))
    except ValueError as exc:
        raise ValidationError(
            "Draft verification_status is invalid.",
            details={"field": f"{location}.verification_status", "value": value},
        ) from exc


def _as_draft_status(value: VerificationStatus | str) -> VerificationStatus:
    try:
        status = value if isinstance(value, VerificationStatus) else VerificationStatus(str(value))
    except ValueError as exc:
        raise ValidationError("Invalid draft verification_status.", details={"status": str(value)}) from exc
    if status not in _DRAFT_STATUSES:
        raise ValidationError(
            "Extraction drafts cannot mark facts as verified or rejected.",
            details={"status": status.value},
        )
    return status


def _require_clarification_when_unsupported(
    status: VerificationStatus,
    evidence: tuple[DraftEvidence, ...],
    *,
    item_type: str,
) -> None:
    if not evidence and status is not VerificationStatus.NEEDS_CLARIFICATION:
        raise ValidationError(
            "An extraction item without source evidence must need clarification.",
            details={"item_type": item_type, "verification_status": status.value},
        )


def _as_evidence_tuple(value: object, field_name: str) -> tuple[DraftEvidence, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValidationError(f"{field_name} must be an array.", details={"field": field_name})
    if not all(isinstance(item, DraftEvidence) for item in value):
        raise ValidationError(
            f"{field_name} must contain DraftEvidence values.",
            details={"field": field_name},
        )
    return tuple(value)


def _as_achievement_tuple(value: object) -> tuple[ExperienceAchievementDraft, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValidationError("achievements must be an array.", details={"field": "achievements"})
    if not all(isinstance(item, ExperienceAchievementDraft) for item in value):
        raise ValidationError(
            "achievements must contain ExperienceAchievementDraft values.",
            details={"field": "achievements"},
        )
    return tuple(value)


def _as_skill_tuple(value: object) -> tuple[ExperienceSkillDraft, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValidationError("skills must be an array.", details={"field": "skills"})
    if not all(isinstance(item, ExperienceSkillDraft) for item in value):
        raise ValidationError(
            "skills must contain ExperienceSkillDraft values.",
            details={"field": "skills"},
        )
    return tuple(value)


def _as_experience_tuple(value: object) -> tuple[ExperienceDraft, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValidationError("experiences must be an array.", details={"field": "experiences"})
    if not all(isinstance(item, ExperienceDraft) for item in value):
        raise ValidationError(
            "experiences must contain ExperienceDraft values.",
            details={"field": "experiences"},
        )
    return tuple(value)


def _require_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field_name} must be an object.", details={"field": field_name})
    return value


def _require_sequence(value: object, field_name: str) -> Sequence[Any]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValidationError(f"{field_name} must be an array.", details={"field": field_name})
    return value


def _validate_keys(
    payload: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str],
    location: str,
) -> None:
    actual = set(payload)
    missing = sorted(required - actual)
    unknown = sorted(actual - required - optional)
    if missing or unknown:
        raise ValidationError(
            "Extraction draft does not match the expected schema.",
            details={"location": location, "missing": missing, "unknown": unknown},
        )


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} must be a non-blank string.", details={"field": field_name})
    return " ".join(value.split())


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field_name)


def _optional_payload_text(value: object, field_name: str) -> str | None:
    return _optional_text(value, field_name)


def _optional_number(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field_name} must be numeric.", details={"field": field_name})
    return float(value)
