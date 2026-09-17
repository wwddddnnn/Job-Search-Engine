"""Career-domain value objects, invariants, and state machines.

The objects in this module deliberately model only durable domain state.  File
access, database persistence, and model calls are expressed through ports in
neighbouring modules so this layer remains independently testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from job_search_assistant.core.errors import ConflictError, InvalidStateError, ValidationError


class DocumentStatus(StrEnum):
    """The parser lifecycle for one immutable uploaded resume document."""

    IMPORTED = "imported"
    TEXT_EXTRACTED = "text_extracted"
    EXTRACTION_FAILED = "extraction_failed"


class ExtractionRunStatus(StrEnum):
    """The lifecycle of a single extraction draft and its user review."""

    DRAFT_EXTRACTING = "draft_extracting"
    DRAFT_READY = "draft_ready"
    DRAFT_FAILED = "draft_failed"
    UNDER_REVIEW = "under_review"
    PROFILE_VERSION_PUBLISHED = "profile_version_published"


class VerificationStatus(StrEnum):
    """The trust state of a fact; only users may make it ``verified``."""

    DRAFT = "draft"
    NEEDS_CLARIFICATION = "needs_clarification"
    VERIFIED = "verified"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ExperienceFactConfirmation:
    """The user's explicit selection of facts from one extraction-draft experience.

    Presence in a confirmation request is the affirmative user action for the
    experience itself.  Achievements and skills are deliberately opt-in too:
    selecting an experience does *not* implicitly verify all of its children.
    Unselected draft candidates remain unverified in the immutable extraction
    artifact and are never copied into a published profile version.
    """

    experience_index: int
    achievement_indexes: tuple[int, ...] = ()
    skill_indexes: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "experience_index",
            _require_non_negative_index(self.experience_index, "experience_index"),
        )
        object.__setattr__(
            self,
            "achievement_indexes",
            _require_distinct_indexes(self.achievement_indexes, "achievement_indexes"),
        )
        object.__setattr__(
            self,
            "skill_indexes",
            _require_distinct_indexes(self.skill_indexes, "skill_indexes"),
        )


class EvidenceSourceType(StrEnum):
    """The only allowed origins for persisted career evidence."""

    RESUME_DOCUMENT = "resume_document"
    USER_ASSERTION = "user_assertion"


@dataclass(frozen=True, slots=True)
class ResumeDocument:
    """An immutable uploaded file and the state of its text extraction."""

    id: str
    file_ref: str
    mime_type: str
    content_hash: str
    uploaded_at: datetime
    status: DocumentStatus = DocumentStatus.IMPORTED
    metadata: Mapping[str, Any] = field(default_factory=dict)
    parser_version: str | None = None
    extracted_text_ref: str | None = None
    extraction_error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "id"))
        object.__setattr__(self, "file_ref", _require_text(self.file_ref, "file_ref"))
        object.__setattr__(self, "mime_type", _require_text(self.mime_type, "mime_type"))
        object.__setattr__(self, "content_hash", _require_text(self.content_hash, "content_hash"))
        object.__setattr__(self, "status", _as_document_status(self.status))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata, "metadata"))
        _require_datetime(self.uploaded_at, "uploaded_at")

        parser_version = _optional_text(self.parser_version, "parser_version")
        text_ref = _optional_text(self.extracted_text_ref, "extracted_text_ref")
        extraction_error = _optional_text(self.extraction_error, "extraction_error")
        object.__setattr__(self, "parser_version", parser_version)
        object.__setattr__(self, "extracted_text_ref", text_ref)
        object.__setattr__(self, "extraction_error", extraction_error)

        if self.status is DocumentStatus.IMPORTED:
            if text_ref is not None or parser_version is not None or extraction_error is not None:
                raise ValidationError(
                    "An imported document cannot have parser output or an extraction error.",
                    details={"document_id": self.id, "status": self.status.value},
                )
        elif self.status is DocumentStatus.TEXT_EXTRACTED:
            if text_ref is None or parser_version is None or extraction_error is not None:
                raise ValidationError(
                    "A text-extracted document requires a text reference and extractor version.",
                    details={"document_id": self.id, "status": self.status.value},
                )
        elif self.status is DocumentStatus.EXTRACTION_FAILED:
            if extraction_error is None or text_ref is not None:
                raise ValidationError(
                    "A failed document extraction requires an error and cannot have text output.",
                    details={"document_id": self.id, "status": self.status.value},
                )

    def mark_text_extracted(self, *, text_ref: str, extractor_version: str) -> "ResumeDocument":
        """Advance an imported document after its extracted text is durably stored."""
        self._require_status(DocumentStatus.IMPORTED, "record extracted text")
        return replace(
            self,
            status=DocumentStatus.TEXT_EXTRACTED,
            extracted_text_ref=_require_text(text_ref, "text_ref"),
            parser_version=_require_text(extractor_version, "extractor_version"),
            extraction_error=None,
        )

    def mark_extraction_failed(self, *, error: str) -> "ResumeDocument":
        """Record a terminal parser failure without discarding the original file."""
        self._require_status(DocumentStatus.IMPORTED, "record extraction failure")
        return replace(
            self,
            status=DocumentStatus.EXTRACTION_FAILED,
            parser_version=None,
            extracted_text_ref=None,
            extraction_error=_require_text(error, "error"),
        )

    def start_extraction_run(
        self,
        *,
        run_id: str,
        input_hash: str,
        model: str,
        prompt_version: str,
        schema_version: str,
        started_at: datetime | None = None,
    ) -> "ExtractionRun":
        """Start draft extraction only after a durable text extraction exists."""
        return ExtractionRun.start(
            id=run_id,
            document=self,
            input_hash=input_hash,
            model=model,
            prompt_version=prompt_version,
            schema_version=schema_version,
            started_at=started_at,
        )

    def _require_status(self, expected: DocumentStatus, action: str) -> None:
        if self.status is not expected:
            raise InvalidStateError("resume_document", self.id, self.status.value, action)


@dataclass(frozen=True, slots=True)
class ResumeText:
    """One append-only result of extracting text from a resume document."""

    id: str
    document_id: str
    text_ref: str
    content_hash: str
    extractor_version: str
    created_at: datetime
    locator_map_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "id"))
        object.__setattr__(self, "document_id", _require_text(self.document_id, "document_id"))
        object.__setattr__(self, "text_ref", _require_text(self.text_ref, "text_ref"))
        object.__setattr__(self, "content_hash", _require_text(self.content_hash, "content_hash"))
        object.__setattr__(self, "extractor_version", _require_text(self.extractor_version, "extractor_version"))
        object.__setattr__(self, "locator_map_ref", _optional_text(self.locator_map_ref, "locator_map_ref"))
        _require_datetime(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class ExtractionRun:
    """An append-only LLM run whose output is always a draft until confirmed."""

    id: str
    document_id: str
    input_hash: str
    model: str
    prompt_version: str
    schema_version: str
    started_at: datetime
    status: ExtractionRunStatus = ExtractionRunStatus.DRAFT_EXTRACTING
    output_ref: str | None = None
    error_summary: Mapping[str, Any] | None = None
    completed_at: datetime | None = None
    published_profile_version_id: str | None = None

    @classmethod
    def start(
        cls,
        *,
        id: str,
        document: ResumeDocument,
        input_hash: str,
        model: str,
        prompt_version: str,
        schema_version: str,
        started_at: datetime | None = None,
    ) -> "ExtractionRun":
        """Create a run at the sole legal entry point: ``TextExtracted``."""
        if document.status is not DocumentStatus.TEXT_EXTRACTED:
            raise InvalidStateError(
                "resume_document",
                document.id,
                document.status.value,
                "start extraction run",
            )
        return cls(
            id=id,
            document_id=document.id,
            input_hash=input_hash,
            model=model,
            prompt_version=prompt_version,
            schema_version=schema_version,
            started_at=started_at or datetime.now(UTC),
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "id"))
        object.__setattr__(self, "document_id", _require_text(self.document_id, "document_id"))
        object.__setattr__(self, "input_hash", _require_text(self.input_hash, "input_hash"))
        object.__setattr__(self, "model", _require_text(self.model, "model"))
        object.__setattr__(self, "prompt_version", _require_text(self.prompt_version, "prompt_version"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, "schema_version"))
        object.__setattr__(self, "status", _as_extraction_run_status(self.status))
        object.__setattr__(self, "output_ref", _optional_text(self.output_ref, "output_ref"))
        object.__setattr__(
            self,
            "error_summary",
            None if self.error_summary is None else _freeze_mapping(self.error_summary, "error_summary"),
        )
        object.__setattr__(
            self,
            "published_profile_version_id",
            _optional_text(self.published_profile_version_id, "published_profile_version_id"),
        )
        _require_datetime(self.started_at, "started_at")
        if self.completed_at is not None:
            _require_datetime(self.completed_at, "completed_at")

        if self.status is ExtractionRunStatus.DRAFT_EXTRACTING:
            _require_absent(self.output_ref, "output_ref", self.id, self.status)
            _require_absent(self.error_summary, "error_summary", self.id, self.status)
            _require_absent(self.completed_at, "completed_at", self.id, self.status)
            _require_absent(self.published_profile_version_id, "published_profile_version_id", self.id, self.status)
        elif self.status is ExtractionRunStatus.DRAFT_READY:
            _require_present(self.output_ref, "output_ref", self.id, self.status)
            _require_absent(self.error_summary, "error_summary", self.id, self.status)
            _require_present(self.completed_at, "completed_at", self.id, self.status)
            _require_absent(self.published_profile_version_id, "published_profile_version_id", self.id, self.status)
        elif self.status is ExtractionRunStatus.DRAFT_FAILED:
            _require_absent(self.output_ref, "output_ref", self.id, self.status)
            _require_present(self.error_summary, "error_summary", self.id, self.status)
            _require_present(self.completed_at, "completed_at", self.id, self.status)
            _require_absent(self.published_profile_version_id, "published_profile_version_id", self.id, self.status)
        elif self.status is ExtractionRunStatus.UNDER_REVIEW:
            _require_present(self.output_ref, "output_ref", self.id, self.status)
            _require_absent(self.error_summary, "error_summary", self.id, self.status)
            _require_present(self.completed_at, "completed_at", self.id, self.status)
            _require_absent(self.published_profile_version_id, "published_profile_version_id", self.id, self.status)
        elif self.status is ExtractionRunStatus.PROFILE_VERSION_PUBLISHED:
            _require_present(self.output_ref, "output_ref", self.id, self.status)
            _require_present(self.completed_at, "completed_at", self.id, self.status)
            _require_present(
                self.published_profile_version_id,
                "published_profile_version_id",
                self.id,
                self.status,
            )

    def mark_draft_ready(
        self,
        *,
        output_ref: str,
        completed_at: datetime,
    ) -> "ExtractionRun":
        """Accept schema-valid model output as a draft, never as verified facts.

        The application service supplies the completion time so a persisted
        transition can be reconstructed deterministically for validation.
        """
        self._require_status(ExtractionRunStatus.DRAFT_EXTRACTING, "mark extraction draft ready")
        return replace(
            self,
            status=ExtractionRunStatus.DRAFT_READY,
            output_ref=_require_text(output_ref, "output_ref"),
            error_summary=None,
            completed_at=completed_at,
            published_profile_version_id=None,
        )

    def mark_draft_failed(
        self,
        *,
        error_summary: Mapping[str, Any],
        completed_at: datetime,
    ) -> "ExtractionRun":
        """Record a terminal model or draft-schema failure."""
        self._require_status(ExtractionRunStatus.DRAFT_EXTRACTING, "record extraction failure")
        return replace(
            self,
            status=ExtractionRunStatus.DRAFT_FAILED,
            output_ref=None,
            error_summary=_freeze_mapping(error_summary, "error_summary"),
            completed_at=completed_at,
            published_profile_version_id=None,
        )

    def begin_review(self) -> "ExtractionRun":
        """Move a ready draft into the explicit user-review state."""
        self._require_status(ExtractionRunStatus.DRAFT_READY, "begin draft review")
        return replace(self, status=ExtractionRunStatus.UNDER_REVIEW)

    def return_to_draft(self) -> "ExtractionRun":
        """Return a reviewed draft for edits or another extraction attempt."""
        self._require_status(ExtractionRunStatus.UNDER_REVIEW, "return draft to ready")
        return replace(self, status=ExtractionRunStatus.DRAFT_READY)

    def publish_profile_version(
        self,
        *,
        profile_version_id: str,
        completed_at: datetime | None = None,
    ) -> "ExtractionRun":
        """Record the terminal, explicit-confirmation publication transition."""
        self._require_status(ExtractionRunStatus.UNDER_REVIEW, "publish profile version")
        return replace(
            self,
            status=ExtractionRunStatus.PROFILE_VERSION_PUBLISHED,
            published_profile_version_id=_require_text(profile_version_id, "profile_version_id"),
            completed_at=completed_at or datetime.now(UTC),
        )

    def _require_status(self, expected: ExtractionRunStatus, action: str) -> None:
        if self.status is not expected:
            raise InvalidStateError("extraction_run", self.id, self.status.value, action)


@dataclass(frozen=True, slots=True)
class CareerProfile:
    """The stable profile identity with a pointer to its latest published version."""

    id: str
    owner_id: str
    display_name: str
    created_at: datetime
    updated_at: datetime
    tenant_id: str | None = None
    current_version_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "id"))
        object.__setattr__(self, "owner_id", _require_text(self.owner_id, "owner_id"))
        object.__setattr__(self, "display_name", _require_text(self.display_name, "display_name"))
        object.__setattr__(self, "tenant_id", _optional_text(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "current_version_id", _optional_text(self.current_version_id, "current_version_id"))
        _require_datetime(self.created_at, "created_at")
        _require_datetime(self.updated_at, "updated_at")

    def with_published_version(
        self,
        *,
        profile_version: "ProfileVersion",
        previous_version: "ProfileVersion | None",
        updated_at: datetime | None = None,
    ) -> "CareerProfile":
        """Advance the pointer only to this profile's next immutable version.

        The explicit ``profile_version.profile_id == self.id`` check applies
        to both initial and subsequent publication, so a version belonging to
        another profile can never become this profile's current pointer.
        """
        if profile_version.profile_id != self.id:
            raise ConflictError(
                "A profile version belongs to a different career profile.",
                details={"profile_id": self.id, "profile_version_id": profile_version.id},
            )
        if self.current_version_id is None:
            if previous_version is not None or profile_version.version != 1:
                raise ConflictError(
                    "The first profile version must be version 1.",
                    details={"profile_id": self.id, "version": profile_version.version},
                )
        else:
            if previous_version is None or previous_version.id != self.current_version_id:
                raise ConflictError(
                    "Publishing requires the profile's current version as the predecessor.",
                    details={"profile_id": self.id, "current_version_id": self.current_version_id},
                )
            if profile_version.version != previous_version.version + 1:
                raise ConflictError(
                    "Profile versions must increment by exactly one.",
                    details={
                        "profile_id": self.id,
                        "previous_version": previous_version.version,
                        "requested_version": profile_version.version,
                    },
                )
        return replace(
            self,
            current_version_id=profile_version.id,
            updated_at=updated_at or datetime.now(UTC),
        )


@dataclass(frozen=True, slots=True)
class ProfileVersion:
    """An immutable, citeable set of explicitly confirmed career facts."""

    id: str
    profile_id: str
    version: int
    created_at: datetime
    source_summary: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "id"))
        object.__setattr__(self, "profile_id", _require_text(self.profile_id, "profile_id"))
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version <= 0:
            raise ValidationError(
                "profile version must be a positive integer.",
                details={"field": "version", "value": self.version},
            )
        _require_datetime(self.created_at, "created_at")
        object.__setattr__(self, "source_summary", _freeze_mapping(self.source_summary, "source_summary"))

    @classmethod
    def initial(
        cls,
        *,
        id: str,
        profile_id: str,
        source_summary: Mapping[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> "ProfileVersion":
        """Create version 1 for a profile that has no published predecessor."""
        return cls(
            id=id,
            profile_id=profile_id,
            version=1,
            created_at=created_at or datetime.now(UTC),
            source_summary=source_summary or {},
        )

    @classmethod
    def next(
        cls,
        *,
        id: str,
        previous_version: "ProfileVersion",
        source_summary: Mapping[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> "ProfileVersion":
        """Create exactly the next version without mutating its predecessor."""
        if not isinstance(previous_version, cls):
            raise ValidationError(
                "previous_version must be a ProfileVersion.",
                details={"field": "previous_version"},
            )
        return cls(
            id=id,
            profile_id=previous_version.profile_id,
            version=previous_version.version + 1,
            created_at=created_at or datetime.now(UTC),
            source_summary=source_summary or {},
        )


@dataclass(frozen=True, slots=True)
class Experience:
    """One verified, rejected, or still-unreviewed period of employment."""

    id: str
    profile_version_id: str
    organization: str
    role: str
    verification_status: VerificationStatus
    created_at: datetime
    date_range: str | None = None
    summary: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "id"))
        object.__setattr__(self, "profile_version_id", _require_text(self.profile_version_id, "profile_version_id"))
        object.__setattr__(self, "organization", _require_text(self.organization, "organization"))
        object.__setattr__(self, "role", _require_text(self.role, "role"))
        object.__setattr__(self, "verification_status", _as_verification_status(self.verification_status))
        object.__setattr__(self, "date_range", _optional_text(self.date_range, "date_range"))
        object.__setattr__(self, "summary", _optional_text(self.summary, "summary"))
        _require_datetime(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class ExperienceAchievement:
    """A discrete outcome within an experience, including optional numeric impact."""

    id: str
    experience_id: str
    action_text: str
    verification_status: VerificationStatus
    created_at: datetime
    outcome_text: str | None = None
    metric_value: float | None = None
    metric_unit: str | None = None
    metric_source_document_id: str | None = None
    metric_user_confirmed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "id"))
        object.__setattr__(self, "experience_id", _require_text(self.experience_id, "experience_id"))
        object.__setattr__(self, "action_text", _require_text(self.action_text, "action_text"))
        object.__setattr__(self, "verification_status", _as_verification_status(self.verification_status))
        object.__setattr__(self, "outcome_text", _optional_text(self.outcome_text, "outcome_text"))
        object.__setattr__(self, "metric_unit", _optional_text(self.metric_unit, "metric_unit"))
        object.__setattr__(
            self,
            "metric_source_document_id",
            _optional_text(self.metric_source_document_id, "metric_source_document_id"),
        )
        if self.metric_value is not None:
            if isinstance(self.metric_value, bool) or not isinstance(self.metric_value, (int, float)):
                raise ValidationError(
                    "metric_value must be numeric when provided.",
                    details={"field": "metric_value"},
                )
            object.__setattr__(self, "metric_value", float(self.metric_value))
        if self.metric_unit is not None and self.metric_value is None:
            raise ValidationError(
                "metric_unit requires metric_value.",
                details={"field": "metric_unit"},
            )
        if self.metric_user_confirmed_at is not None:
            _require_datetime(self.metric_user_confirmed_at, "metric_user_confirmed_at")
        if (
            self.metric_value is not None
            and self.metric_source_document_id is None
            and self.metric_user_confirmed_at is None
        ):
            raise ValidationError(
                "A numeric achievement metric requires a document source or explicit user confirmation.",
                details={"achievement_id": self.id, "field": "metric_value"},
            )
        _require_datetime(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class Skill:
    """A normalized skill used for deterministic matching without replacing raw text."""

    id: str
    canonical_name: str
    created_at: datetime
    taxonomy_ref: str | None = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "id"))
        object.__setattr__(self, "canonical_name", _require_text(self.canonical_name, "canonical_name"))
        # SQLite deliberately uses a non-null empty string for an unspecified
        # taxonomy so its composite uniqueness rule has deterministic semantics.
        # Accepting ``None`` here preserves callers built against S1 while
        # normalising the durable domain value to that same representation.
        taxonomy_ref = (
            ""
            if self.taxonomy_ref is None or self.taxonomy_ref == ""
            else _require_text(self.taxonomy_ref, "taxonomy_ref")
        )
        object.__setattr__(self, "taxonomy_ref", taxonomy_ref)
        _require_datetime(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class ExperienceSkill:
    """An experience-to-skill association retaining the wording seen by the user."""

    id: str
    experience_id: str
    skill_id: str
    raw_skill_name: str
    verification_status: VerificationStatus
    created_at: datetime
    proficiency: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "id"))
        object.__setattr__(self, "experience_id", _require_text(self.experience_id, "experience_id"))
        object.__setattr__(self, "skill_id", _require_text(self.skill_id, "skill_id"))
        object.__setattr__(self, "raw_skill_name", _require_text(self.raw_skill_name, "raw_skill_name"))
        object.__setattr__(self, "verification_status", _as_verification_status(self.verification_status))
        object.__setattr__(self, "proficiency", _optional_text(self.proficiency, "proficiency"))
        _require_datetime(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class ExperienceEvidence:
    """The smallest evidence unit that can support a career fact or achievement."""

    id: str
    experience_id: str
    source_type: EvidenceSourceType
    verification_status: VerificationStatus
    user_verified: bool
    created_at: datetime
    source_document_id: str | None = None
    source_excerpt: str | None = None
    source_locator: str | None = None
    confidence: float | None = None
    verified_at: datetime | None = None
    experience_achievement_id: str | None = None
    experience_skill_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "id"))
        object.__setattr__(self, "experience_id", _require_text(self.experience_id, "experience_id"))
        object.__setattr__(self, "source_type", _as_evidence_source_type(self.source_type))
        object.__setattr__(self, "verification_status", _as_verification_status(self.verification_status))
        if not isinstance(self.user_verified, bool):
            raise ValidationError("user_verified must be a boolean.", details={"field": "user_verified"})
        object.__setattr__(self, "source_document_id", _optional_text(self.source_document_id, "source_document_id"))
        object.__setattr__(self, "source_excerpt", _optional_text(self.source_excerpt, "source_excerpt"))
        object.__setattr__(self, "source_locator", _optional_text(self.source_locator, "source_locator"))
        object.__setattr__(
            self,
            "experience_achievement_id",
            _optional_text(self.experience_achievement_id, "experience_achievement_id"),
        )
        object.__setattr__(
            self, "experience_skill_id",
            _optional_text(self.experience_skill_id, "experience_skill_id"),
        )
        if self.experience_skill_id is not None and self.experience_achievement_id is not None:
            raise ValidationError("Evidence cannot target both an achievement and a skill.")
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
        _require_datetime(self.created_at, "created_at")
        if self.verified_at is not None:
            _require_datetime(self.verified_at, "verified_at")

        if self.source_type is EvidenceSourceType.RESUME_DOCUMENT:
            if self.source_document_id is None:
                raise ValidationError(
                    "Evidence without a document source must be recorded as user_assertion.",
                    details={"field": "source_document_id", "required_source_type": "user_assertion"},
                )
            if self.source_excerpt is None and self.source_locator is None:
                raise ValidationError(
                    "Resume-document evidence requires an excerpt or source locator.",
                    details={"evidence_id": self.id},
                )
        elif self.source_type is EvidenceSourceType.USER_ASSERTION:
            if self.source_document_id is not None:
                raise ValidationError(
                    "A user assertion cannot claim a resume-document source.",
                    details={"evidence_id": self.id, "source_document_id": self.source_document_id},
                )
            if not self.user_verified or self.verified_at is None:
                raise ValidationError(
                    "A user assertion requires explicit user confirmation and confirmation time.",
                    details={"evidence_id": self.id},
                )

        if self.user_verified:
            if self.verification_status is not VerificationStatus.VERIFIED or self.verified_at is None:
                raise ValidationError(
                    "Verified evidence requires verified status and verification time.",
                    details={"evidence_id": self.id},
                )
        elif self.verification_status is VerificationStatus.VERIFIED or self.verified_at is not None:
            raise ValidationError(
                "Unverified evidence cannot be marked verified or have a verification time.",
                details={"evidence_id": self.id},
            )

    @classmethod
    def user_assertion(
        cls,
        *,
        id: str,
        experience_id: str,
        created_at: datetime,
        confirmed_at: datetime,
        assertion_text: str | None = None,
        experience_achievement_id: str | None = None,
    ) -> "ExperienceEvidence":
        """Create evidence for a user-supplied fact that has no document source."""
        return cls(
            id=id,
            experience_id=experience_id,
            source_type=EvidenceSourceType.USER_ASSERTION,
            verification_status=VerificationStatus.VERIFIED,
            user_verified=True,
            created_at=created_at,
            source_excerpt=assertion_text,
            verified_at=confirmed_at,
            experience_achievement_id=experience_achievement_id,
        )


def require_verified_profile_facts(
    *,
    experiences: tuple[Experience, ...] | list[Experience],
    achievements: tuple[ExperienceAchievement, ...] | list[ExperienceAchievement],
    experience_skills: tuple[ExperienceSkill, ...] | list[ExperienceSkill],
    evidence: tuple[ExperienceEvidence, ...] | list[ExperienceEvidence],
) -> None:
    """Reject every unverified fact before it can enter a published version.

    SQLite 0004 intentionally permits draft review values in fact tables so
    future review workflows can retain them.  A ``ProfileVersion`` is a
    stronger boundary: it may contain only explicitly user-confirmed facts.
    Both the confirmation use case and the persistence adapter call this
    guard, so a caller cannot turn a draft item into published data merely by
    constructing persistence objects directly.
    """
    groups = {
        "experience": experiences,
        "experience_achievement": achievements,
        "experience_skill": experience_skills,
        "experience_evidence": evidence,
    }
    for fact_type, values in groups.items():
        for value in values:
            if value.verification_status is not VerificationStatus.VERIFIED:
                raise ValidationError(
                    "Published profile versions may contain verified facts only.",
                    details={
                        "fact_type": fact_type,
                        "fact_id": value.id,
                        "verification_status": value.verification_status.value,
                    },
                )
    for item in evidence:
        if not item.user_verified or item.verified_at is None:
            raise ValidationError(
                "Published profile evidence requires explicit user verification.",
                details={"evidence_id": item.id},
            )


# The public spelling mirrors the section title in the architecture document.
ResumeDocumentStatus = DocumentStatus


def _as_document_status(value: DocumentStatus | str) -> DocumentStatus:
    try:
        return value if isinstance(value, DocumentStatus) else DocumentStatus(str(value))
    except ValueError as exc:
        raise ValidationError("Invalid document status.", details={"status": str(value)}) from exc


def _as_extraction_run_status(value: ExtractionRunStatus | str) -> ExtractionRunStatus:
    try:
        return value if isinstance(value, ExtractionRunStatus) else ExtractionRunStatus(str(value))
    except ValueError as exc:
        raise ValidationError("Invalid extraction run status.", details={"status": str(value)}) from exc


def _as_verification_status(value: VerificationStatus | str) -> VerificationStatus:
    try:
        return value if isinstance(value, VerificationStatus) else VerificationStatus(str(value))
    except ValueError as exc:
        raise ValidationError("Invalid verification status.", details={"status": str(value)}) from exc


def _as_evidence_source_type(value: EvidenceSourceType | str) -> EvidenceSourceType:
    try:
        return value if isinstance(value, EvidenceSourceType) else EvidenceSourceType(str(value))
    except ValueError as exc:
        raise ValidationError("Invalid evidence source type.", details={"source_type": str(value)}) from exc


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            f"{field_name} must be a non-blank string.",
            details={"field": field_name},
        )
    return " ".join(value.split())


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field_name)


def _require_non_negative_index(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(
            f"{field_name} must be a non-negative integer.",
            details={"field": field_name, "value": value},
        )
    return value


def _require_distinct_indexes(value: object, field_name: str) -> tuple[int, ...]:
    if not isinstance(value, tuple):
        raise ValidationError(
            f"{field_name} must be a tuple of indexes.",
            details={"field": field_name},
        )
    indexes = tuple(_require_non_negative_index(index, field_name) for index in value)
    if len(set(indexes)) != len(indexes):
        raise ValidationError(
            f"{field_name} must not contain duplicate indexes.",
            details={"field": field_name},
        )
    return indexes


def _require_datetime(value: object, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise ValidationError(
            f"{field_name} must be a datetime.",
            details={"field": field_name},
        )


def _freeze_mapping(value: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field_name} must be an object.", details={"field": field_name})
    return MappingProxyType({str(key): _freeze_value(item) for key, item in value.items()})


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    return value


def _require_present(value: object, field_name: str, entity_id: str, status: StrEnum) -> None:
    if value is None:
        raise ValidationError(
            f"{field_name} is required for extraction run status '{status.value}'.",
            details={"extraction_run_id": entity_id, "field": field_name, "status": status.value},
        )


def _require_absent(value: object, field_name: str, entity_id: str, status: StrEnum) -> None:
    if value is not None:
        raise ValidationError(
            f"{field_name} is not allowed for extraction run status '{status.value}'.",
            details={"extraction_run_id": entity_id, "field": field_name, "status": status.value},
        )
