"""Career document import and draft-extraction application services."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import mimetypes
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from job_search_assistant.career.extraction import (
    EXTRACTION_DRAFT_SCHEMA_VERSION,
    ExtractionDraft,
    validate_extraction_draft,
)
from job_search_assistant.career.ports import (
    CareerExtractionPort,
    DocumentStoragePort,
    ExtractionParseError,
    ResumeTextExtractionError,
    ResumeTextExtractorPort,
)
from job_search_assistant.career.store import (
    CareerStore,
    ExtractionRunReservation,
    ProfileVersionFacts,
    ResumeImportReservation,
)
from job_search_assistant.career.types import (
    CareerProfile,
    DocumentStatus,
    EvidenceSourceType,
    Experience,
    ExperienceAchievement,
    ExperienceEvidence,
    ExperienceFactConfirmation,
    ExperienceSkill,
    ExtractionRun,
    ExtractionRunStatus,
    ProfileVersion,
    ResumeDocument,
    ResumeText,
    Skill,
    VerificationStatus,
    require_verified_profile_facts,
)
from job_search_assistant.core.context import RequestContext
from job_search_assistant.core.errors import (
    ApplicationError,
    ConflictError,
    InfrastructureError,
    ValidationError,
)
from job_search_assistant.core.idempotency import IdempotencyReservationState


@dataclass(frozen=True, slots=True)
class ImportResumeDocumentResult:
    """Safe result returned by an import command or an idempotent replay."""

    document_id: str
    status: DocumentStatus

    @classmethod
    def from_document(cls, document: ResumeDocument) -> "ImportResumeDocumentResult":
        """Build the externally safe representation without exposing raw content references."""
        return cls(document_id=document.id, status=document.status)


@dataclass(slots=True)
class ImportResumeDocument:
    """Import one local resume file, retain it, and record a terminal parse outcome.

    Source-file reads, controlled-file writes, and parser work occur outside
    SQLite transactions.  The store first records an ``Imported`` checkpoint
    with its idempotency reservation, then atomically records either extracted
    text or ``ExtractionFailed`` together with audit history and the replayable
    idempotency response.
    """

    store: CareerStore
    storage: DocumentStoragePort
    text_extractor: ResumeTextExtractorPort

    def execute(
        self,
        *,
        file_ref: Path | str,
        metadata: Mapping[str, Any],
        idempotency_key: str,
        context: RequestContext | None = None,
    ) -> ImportResumeDocumentResult:
        """Run the idempotent import command and return its document identifier/status."""
        request_context = context or RequestContext.create(source="career-import")
        source_path = _source_path(file_ref)
        normalized_metadata, mime_type = _normalize_metadata(metadata, source_path)
        normalized_key = _require_identifier(idempotency_key, "idempotency_key")

        request = {
            "file_ref": str(source_path),
            "metadata": normalized_metadata,
        }
        replay = self.store.peek_resume_import(
            idempotency_key=normalized_key,
            request=request,
            context=request_context,
        )
        if replay is not None:
            if replay.state is IdempotencyReservationState.COMPLETED:
                return ImportResumeDocumentResult.from_document(replay.document)
            return self._complete_import(reservation=replay, context=request_context)

        # This local source read intentionally precedes any database transaction
        # and occurs only after a completed replay has returned above.
        content = _read_source_file(source_path)
        content_hash = sha256(content).hexdigest()
        stored_file_ref = self.storage.store_document(content=content, content_hash=content_hash)

        document = ResumeDocument(
            id=str(uuid4()),
            file_ref=stored_file_ref,
            mime_type=mime_type,
            content_hash=content_hash,
            uploaded_at=datetime.now(UTC),
            metadata=normalized_metadata,
        )
        reservation = self.store.reserve_resume_import(
            document=document,
            idempotency_key=normalized_key,
            request=request,
            context=request_context,
        )
        if reservation.state is IdempotencyReservationState.COMPLETED:
            return ImportResumeDocumentResult.from_document(reservation.document)
        return self._complete_import(reservation=reservation, context=request_context)

    def _complete_import(
        self,
        *,
        reservation: ResumeImportReservation,
        context: RequestContext,
    ) -> ImportResumeDocumentResult:
        """Resume parser work from the durable document checkpoint for a key."""
        imported = reservation.document
        terminal_document, extracted_text = self._extract_and_store(imported)
        persisted = self.store.finalize_resume_import(
            document=terminal_document,
            text=extracted_text,
            idempotency_record_id=reservation.idempotency_record_id,
            context=context,
        )
        return ImportResumeDocumentResult.from_document(persisted)

    def _extract_and_store(
        self,
        document: ResumeDocument,
    ) -> tuple[ResumeDocument, ResumeText | None]:
        try:
            extracted = self.text_extractor.extract(document=document)
            text_hash = sha256(extracted.text.encode("utf-8")).hexdigest()
            text_ref = self.storage.store_text(
                text=extracted.text,
                content_hash=text_hash,
                locator_map=extracted.locator_map,
            )
            text = ResumeText(
                id=str(uuid4()),
                document_id=document.id,
                text_ref=text_ref,
                content_hash=text_hash,
                extractor_version=extracted.extractor_version,
                locator_map_ref=text_ref,
                created_at=datetime.now(UTC),
            )
            return (
                document.mark_text_extracted(
                    text_ref=text_ref,
                    extractor_version=extracted.extractor_version,
                ),
                text,
            )
        except (ResumeTextExtractionError, UnicodeError, ValidationError) as exc:
            return document.mark_extraction_failed(error=_safe_extraction_error(exc)), None


@dataclass(frozen=True, slots=True)
class StartExtractionRunResult:
    """Safe result returned by a draft extraction command or idempotent replay."""

    run_id: str
    status: ExtractionRunStatus

    @classmethod
    def from_run(cls, run: ExtractionRun) -> "StartExtractionRunResult":
        """Build the externally safe representation without exposing output references."""
        return cls(run_id=run.id, status=run.status)


@dataclass(slots=True)
class StartExtractionRun:
    """Start one append-only, schema-validated Career extraction draft.

    The durable ``draft_extracting`` run and its idempotency checkpoint are
    committed before calling the provider.  Loading controlled text, invoking
    the provider, and writing the immutable draft artifact all occur outside a
    SQLite transaction; a short final transaction records the terminal run
    state, audit event, and replay response.
    """

    store: CareerStore
    storage: DocumentStoragePort
    extraction_provider: CareerExtractionPort
    model: str = "deterministic-career-extraction-v1"
    prompt_version: str = "career-extraction-prompt-v1"
    schema_version: str = EXTRACTION_DRAFT_SCHEMA_VERSION

    def execute(
        self,
        *,
        document_id: str,
        idempotency_key: str,
        context: RequestContext | None = None,
    ) -> StartExtractionRunResult:
        """Run one idempotent draft extraction without granting verified status."""
        request_context = context or RequestContext.create(source="career-extraction")
        normalized_document_id = _require_identifier(document_id, "document_id")
        normalized_key = _require_identifier(idempotency_key, "idempotency_key")
        model = _require_identifier(self.model, "model")
        prompt_version = _require_identifier(self.prompt_version, "prompt_version")
        schema_version = _require_identifier(self.schema_version, "schema_version")
        request = {
            "document_id": normalized_document_id,
            "model": model,
            "prompt_version": prompt_version,
            "schema_version": schema_version,
        }

        replay = self.store.peek_extraction_run(
            idempotency_key=normalized_key,
            request=request,
            context=request_context,
        )
        if replay is not None and replay.state is IdempotencyReservationState.COMPLETED:
            return StartExtractionRunResult.from_run(replay.run)

        extracted_text: str | None = None
        if replay is None:
            document, extracted_text = self._load_extraction_input(normalized_document_id)
            run = document.start_extraction_run(
                run_id=str(uuid4()),
                input_hash=sha256(extracted_text.encode("utf-8")).hexdigest(),
                model=model,
                prompt_version=prompt_version,
                schema_version=schema_version,
                started_at=datetime.now(UTC),
            )
            reservation = self.store.reserve_extraction_run(
                run=run,
                idempotency_key=normalized_key,
                request=request,
                context=request_context,
            )
            if reservation.state is IdempotencyReservationState.COMPLETED:
                return StartExtractionRunResult.from_run(reservation.run)
        else:
            reservation = replay

        run = reservation.run
        if run.document_id != normalized_document_id:
            raise InfrastructureError(
                "Extraction idempotency checkpoint belongs to a different document.",
                details={"run_id": run.id, "document_id": run.document_id},
            ).with_correlation_id(request_context.correlation_id)
        if extracted_text is None:
            _, extracted_text = self._load_extraction_input(normalized_document_id)
        if sha256(extracted_text.encode("utf-8")).hexdigest() != run.input_hash:
            raise InfrastructureError(
                "Stored extraction input no longer matches the durable run input hash.",
                details={"run_id": run.id, "document_id": run.document_id},
            ).with_correlation_id(request_context.correlation_id)
        return self._extract_validate_store_and_finalize(
            run=run,
            extracted_text=extracted_text,
            idempotency_record_id=reservation.idempotency_record_id,
            context=request_context,
        )

    def _load_extraction_input(self, document_id: str) -> tuple[ResumeDocument, str]:
        """Read the durable text artifact without holding a SQLite transaction open."""
        document = self.store.get_resume_document(document_id=document_id)
        if (
            document.status is not DocumentStatus.TEXT_EXTRACTED
            or document.extracted_text_ref is None
        ):
            raise ValidationError(
                "A resume document must have extracted text before starting an extraction run.",
                details={"document_id": document.id, "status": document.status.value},
            )
        text = next(
            (
                item
                for item in self.store.list_resume_texts(document_id=document.id)
                if item.text_ref == document.extracted_text_ref
            ),
            None,
        )
        if text is None:
            raise InfrastructureError(
                "Resume document points to text that has no durable text record.",
                details={"document_id": document.id, "text_ref": document.extracted_text_ref},
            )
        return document, self.storage.load_text(text_ref=text.text_ref)

    def _extract_validate_store_and_finalize(
        self,
        *,
        run: ExtractionRun,
        extracted_text: str,
        idempotency_record_id: str,
        context: RequestContext,
    ) -> StartExtractionRunResult:
        """Persist only extraction, validation, or parse failures as draft failures.

        Infrastructure faults deliberately escape this method with the durable
        run and idempotency checkpoint still in progress, so the same key can
        retry after the underlying dependency has recovered.

        Provider configuration is a deterministic command failure for this
        slice: it is persisted as ``draft_failed`` and the completed start key
        replays that terminal run.  After changing configuration, callers must
        use a new key to create a new append-only run.
        """
        try:
            payload = self.extraction_provider.extract(extracted_text=extracted_text, run=run)
            draft = validate_extraction_draft(
                payload,
                expected_schema_version=run.schema_version,
            )
            _require_draft_locators(draft)
        except json.JSONDecodeError as exc:
            parse_error = ExtractionParseError(
                "Career extraction provider returned malformed JSON.",
                details={
                    "run_id": run.id,
                    "document_id": run.document_id,
                    "reason": "invalid_json",
                },
            ).with_correlation_id(context.correlation_id)
            failed = run.mark_draft_failed(
                error_summary=_extraction_error_summary(parse_error),
                completed_at=datetime.now(UTC),
            )
            self.store.finalize_extraction_run(
                run=failed,
                idempotency_record_id=idempotency_record_id,
                context=context,
            )
            raise parse_error from exc
        except (
            ResumeTextExtractionError,
            UnicodeError,
            ValidationError,
        ) as exc:
            failed = run.mark_draft_failed(
                error_summary=_extraction_error_summary(exc),
                completed_at=datetime.now(UTC),
            )
            self.store.finalize_extraction_run(
                run=failed,
                idempotency_record_id=idempotency_record_id,
                context=context,
            )
            if isinstance(exc, ApplicationError):
                raise exc.with_correlation_id(context.correlation_id)
            raise
        # Artifact I/O is deliberately outside the catch above.  In particular,
        # InfrastructureError must leave this run retryable rather than being
        # misclassified as a terminal model/schema failure.
        output_ref = self.storage.store_extraction_draft(draft=_draft_to_payload(draft))
        completed = run.mark_draft_ready(
            output_ref=output_ref,
            completed_at=datetime.now(UTC),
        )
        persisted = self.store.finalize_extraction_run(
            run=completed,
            idempotency_record_id=idempotency_record_id,
            context=context,
        )
        return StartExtractionRunResult.from_run(persisted)


@dataclass(frozen=True, slots=True)
class ConfirmExperienceFactsResult:
    """Safe identifier and state for one published, immutable profile version."""

    profile_id: str
    profile_version_id: str
    version: int
    extraction_run_id: str

    @classmethod
    def from_profile_version(
        cls,
        *,
        profile_version: ProfileVersion,
        extraction_run_id: str,
    ) -> "ConfirmExperienceFactsResult":
        """Build the replayable command result without exposing raw draft content."""
        return cls(
            profile_id=profile_version.profile_id,
            profile_version_id=profile_version.id,
            version=profile_version.version,
            extraction_run_id=extraction_run_id,
        )


@dataclass(slots=True)
class ConfirmExperienceFacts:
    """Publish only explicitly user-confirmed facts from one ready extraction draft.

    The confirmation list is an allow-list, not a bulk-approval switch.  Each
    selected experience becomes verified; achievements and skills require their
    own explicit child-index selections.  All omitted draft candidates remain
    in the immutable extraction artifact and are never copied into the new
    ``ProfileVersion``.  The command owns the review transition, publication,
    audit history, and idempotency key; adapters must not write those states
    through the store directly.
    """

    store: CareerStore
    storage: DocumentStoragePort

    def execute(
        self,
        *,
        profile_id: str,
        extraction_run_id: str,
        confirmations: Sequence[ExperienceFactConfirmation],
        idempotency_key: str,
        context: RequestContext | None = None,
    ) -> ConfirmExperienceFactsResult:
        """Confirm selected draft facts and append the next immutable profile version."""
        request_context = context or RequestContext.create(source="career-confirmation")
        normalized_profile_id = _require_identifier(profile_id, "profile_id")
        normalized_run_id = _require_identifier(extraction_run_id, "extraction_run_id")
        normalized_key = _require_identifier(idempotency_key, "idempotency_key")
        normalized_confirmations = _normalize_confirmations(confirmations)
        request = {
            "profile_id": normalized_profile_id,
            "extraction_run_id": normalized_run_id,
            "confirmations": [
                {
                    "experience_index": item.experience_index,
                    "achievement_indexes": list(item.achievement_indexes),
                    "skill_indexes": list(item.skill_indexes),
                }
                for item in normalized_confirmations
            ],
        }

        replay = self.store.peek_experience_confirmation(
            idempotency_key=normalized_key,
            request=request,
            context=request_context,
        )
        if replay is not None and replay.state is IdempotencyReservationState.COMPLETED:
            profile_version = self.store.get_profile_version(
                profile_version_id=replay.profile_version_id or "",
            )
            return ConfirmExperienceFactsResult.from_profile_version(
                profile_version=profile_version,
                extraction_run_id=normalized_run_id,
            )

        run = self.store.get_extraction_run(run_id=normalized_run_id)
        if run.status not in {ExtractionRunStatus.DRAFT_READY, ExtractionRunStatus.UNDER_REVIEW}:
            raise ValidationError(
                "Experience facts can only be confirmed from a ready draft under review.",
                details={"run_id": run.id, "status": run.status.value},
            ).with_correlation_id(request_context.correlation_id)
        if run.output_ref is None:
            raise InfrastructureError(
                "A reviewable extraction run has no durable draft output.",
                details={"run_id": run.id},
            ).with_correlation_id(request_context.correlation_id)

        draft = validate_extraction_draft(
            self.storage.load_extraction_draft(output_ref=run.output_ref),
            expected_schema_version=run.schema_version,
        )
        profile = self.store.get_career_profile(profile_id=normalized_profile_id)
        previous_version = (
            None
            if profile.current_version_id is None
            else self.store.get_profile_version(profile_version_id=profile.current_version_id)
        )
        published_at = datetime.now(UTC)
        profile_version = (
            ProfileVersion.initial(
                id=str(uuid4()),
                profile_id=profile.id,
                source_summary=_confirmation_source_summary(run, normalized_confirmations),
                created_at=published_at,
            )
            if previous_version is None
            else ProfileVersion.next(
                id=str(uuid4()),
                previous_version=previous_version,
                source_summary=_confirmation_source_summary(run, normalized_confirmations),
                created_at=published_at,
            )
        )
        facts = _confirmed_profile_facts(
            profile_version=profile_version,
            draft=draft,
            document_id=run.document_id,
            confirmations=normalized_confirmations,
            confirmed_at=published_at,
        )
        require_verified_profile_facts(
            experiences=facts.experiences,
            achievements=facts.achievements,
            experience_skills=facts.experience_skills,
            evidence=facts.evidence,
        )

        reservation = self.store.reserve_experience_confirmation(
            profile_id=profile.id,
            extraction_run_id=run.id,
            idempotency_key=normalized_key,
            request=request,
            context=request_context,
        )
        if reservation.state is IdempotencyReservationState.COMPLETED:
            completed_version = self.store.get_profile_version(
                profile_version_id=reservation.profile_version_id or "",
            )
            return ConfirmExperienceFactsResult.from_profile_version(
                profile_version=completed_version,
                extraction_run_id=run.id,
            )

        persisted = self.store.finalize_experience_confirmation(
            profile=profile,
            profile_version=profile_version,
            run=run,
            experiences=facts.experiences,
            achievements=facts.achievements,
            skills=facts.skills,
            experience_skills=facts.experience_skills,
            evidence=facts.evidence,
            idempotency_record_id=reservation.idempotency_record_id,
            idempotency_key=normalized_key,
            request=request,
            context=request_context,
        )
        return ConfirmExperienceFactsResult.from_profile_version(
            profile_version=persisted,
            extraction_run_id=run.id,
        )


class NoVerifiedCareerFactsError(ApplicationError):
    """A requested immutable version has no safely usable confirmed facts."""

    def __init__(
        self,
        *,
        profile_id: str,
        profile_version_id: str | None,
        reason: str = "no_verified_facts",
    ) -> None:
        super().__init__(
            "no_verified_career_facts",
            "The requested career profile version has no verified facts.",
            {
                "profile_id": profile_id,
                "profile_version_id": profile_version_id,
                "reason": reason,
            },
        )


@dataclass(frozen=True, slots=True)
class VerifiedEvidencePackItem:
    """One verified fact/evidence projection safe to supply to downstream work."""

    evidence_id: str
    scope: str
    verification_status: VerificationStatus
    content: str
    source_type: EvidenceSourceType
    source_document_id: str | None
    source_excerpt: str | None
    source_locator: str | None


@dataclass(frozen=True, slots=True)
class VerifiedEvidencePack:
    """A traceable, task-scoped projection of verified career facts only."""

    profile_id: str
    profile_version_id: str
    version: int
    task_context: str | None
    items: tuple[VerifiedEvidencePackItem, ...]


@dataclass(frozen=True, slots=True)
class CareerAchievementSnapshot:
    """Minimal confirmed achievement fields for a profile-view scenario."""

    action_text: str
    outcome_text: str | None
    metric_value: float | None
    metric_unit: str | None


@dataclass(frozen=True, slots=True)
class CareerSkillSnapshot:
    """Minimal confirmed skill fields for a profile-view scenario."""

    name: str
    proficiency: str | None


@dataclass(frozen=True, slots=True)
class CareerExperienceSnapshot:
    """Minimal confirmed experience fields without evidence-storage internals."""

    organization: str
    role: str
    date_range: str | None
    summary: str | None
    achievements: tuple[CareerAchievementSnapshot, ...]
    skills: tuple[CareerSkillSnapshot, ...]


@dataclass(frozen=True, slots=True)
class CareerProfileSnapshot:
    """Minimal read model of one immutable, verified career profile version."""

    profile_id: str
    profile_version_id: str
    version: int
    display_name: str
    experiences: tuple[CareerExperienceSnapshot, ...]


@dataclass(slots=True)
class GetVerifiedEvidencePack:
    """Return a read-only, traceable evidence pack for verified career facts.

    The use case reloads the immutable fact graph and verifies every loaded
    fact itself.  This protects downstream consumers even if data was inserted
    outside the normal confirmation/store boundary.
    """

    store: CareerStore

    def execute(
        self,
        *,
        profile_id: str,
        profile_version_id: str | None = None,
        task_context: str | None = None,
    ) -> VerifiedEvidencePack:
        """Return verified facts with one or more evidence-bearing items each."""
        profile, facts = _load_verified_profile_facts(
            store=self.store,
            profile_id=profile_id,
            profile_version_id=profile_version_id,
        )
        if task_context is not None:
            task_context = _require_identifier(task_context, "task_context")
        items = _evidence_pack_items(facts=facts, profile_id=profile.id)
        if not items:
            raise NoVerifiedCareerFactsError(
                profile_id=profile.id,
                profile_version_id=facts.profile_version.id,
            )
        return VerifiedEvidencePack(
            profile_id=profile.id,
            profile_version_id=facts.profile_version.id,
            version=facts.profile_version.version,
            task_context=task_context,
            items=items,
        )


@dataclass(slots=True)
class GetCareerProfileSnapshot:
    """Return the minimal, read-only profile projection for an approved version."""

    store: CareerStore

    def execute(
        self,
        *,
        profile_id: str,
        profile_version_id: str | None = None,
    ) -> CareerProfileSnapshot:
        """Return confirmed profile content without raw documents, storage refs, or audit data."""
        profile, facts = _load_verified_profile_facts(
            store=self.store,
            profile_id=profile_id,
            profile_version_id=profile_version_id,
        )
        achievements_by_experience: dict[str, list[ExperienceAchievement]] = {}
        for achievement in facts.achievements:
            achievements_by_experience.setdefault(achievement.experience_id, []).append(achievement)
        skills_by_experience: dict[str, list[ExperienceSkill]] = {}
        for skill in facts.experience_skills:
            skills_by_experience.setdefault(skill.experience_id, []).append(skill)
        return CareerProfileSnapshot(
            profile_id=profile.id,
            profile_version_id=facts.profile_version.id,
            version=facts.profile_version.version,
            display_name=profile.display_name,
            experiences=tuple(
                CareerExperienceSnapshot(
                    organization=experience.organization,
                    role=experience.role,
                    date_range=experience.date_range,
                    summary=experience.summary,
                    achievements=tuple(
                        CareerAchievementSnapshot(
                            action_text=achievement.action_text,
                            outcome_text=achievement.outcome_text,
                            metric_value=achievement.metric_value,
                            metric_unit=achievement.metric_unit,
                        )
                        for achievement in achievements_by_experience.get(experience.id, ())
                    ),
                    skills=tuple(
                        CareerSkillSnapshot(
                            name=skill.raw_skill_name,
                            proficiency=skill.proficiency,
                        )
                        for skill in skills_by_experience.get(experience.id, ())
                    ),
                )
                for experience in facts.experiences
            ),
        )


def _load_verified_profile_facts(
    *,
    store: CareerStore,
    profile_id: str,
    profile_version_id: str | None,
) -> tuple[CareerProfile, ProfileVersionFacts]:
    """Resolve one profile version and reject any non-verified persisted fact."""
    normalized_profile_id = _require_identifier(profile_id, "profile_id")
    profile = store.get_career_profile(profile_id=normalized_profile_id)
    if profile_version_id is None:
        resolved_version_id = profile.current_version_id
        if resolved_version_id is None:
            raise NoVerifiedCareerFactsError(
                profile_id=profile.id,
                profile_version_id=None,
            )
    else:
        resolved_version_id = _require_identifier(profile_version_id, "profile_version_id")
    facts = store.get_profile_version_facts(profile_version_id=resolved_version_id)
    if facts.profile_version.profile_id != profile.id:
        raise ConflictError(
            "The requested profile version belongs to a different career profile.",
            details={
                "profile_id": profile.id,
                "profile_version_id": facts.profile_version.id,
            },
        )
    require_verified_profile_facts(
        experiences=facts.experiences,
        achievements=facts.achievements,
        experience_skills=facts.experience_skills,
        evidence=facts.evidence,
    )
    if not facts.experiences:
        raise NoVerifiedCareerFactsError(
            profile_id=profile.id,
            profile_version_id=facts.profile_version.id,
        )
    return profile, facts


def _evidence_pack_items(
    *,
    facts: ProfileVersionFacts,
    profile_id: str,
) -> tuple[VerifiedEvidencePackItem, ...]:
    """Associate every returned fact with verified evidence and a stable scope."""
    general_evidence_by_experience: dict[str, list[ExperienceEvidence]] = {}
    achievement_evidence_by_id: dict[str, list[ExperienceEvidence]] = {}
    for evidence in facts.evidence:
        if evidence.experience_achievement_id is None:
            general_evidence_by_experience.setdefault(evidence.experience_id, []).append(evidence)
        else:
            achievement_evidence_by_id.setdefault(
                evidence.experience_achievement_id,
                [],
            ).append(evidence)

    items: list[VerifiedEvidencePackItem] = []
    for experience in facts.experiences:
        evidence = general_evidence_by_experience.get(experience.id, ())
        _require_pack_evidence(
            evidence=evidence,
            profile_id=profile_id,
            profile_version_id=facts.profile_version.id,
            scope="experience",
        )
        items.extend(
            _pack_items_for_evidence(
                evidence=evidence,
                scope="experience",
                content=f"{experience.organization}: {experience.role}",
            )
        )

    for achievement in facts.achievements:
        evidence = achievement_evidence_by_id.get(achievement.id, ())
        _require_pack_evidence(
            evidence=evidence,
            profile_id=profile_id,
            profile_version_id=facts.profile_version.id,
            scope="achievement",
        )
        items.extend(
            _pack_items_for_evidence(
                evidence=evidence,
                scope="achievement",
                content=achievement.action_text,
            )
        )

    for skill in facts.experience_skills:
        evidence = general_evidence_by_experience.get(skill.experience_id, ())
        _require_pack_evidence(
            evidence=evidence,
            profile_id=profile_id,
            profile_version_id=facts.profile_version.id,
            scope="skill",
        )
        items.extend(
            _pack_items_for_evidence(
                evidence=evidence,
                scope="skill",
                content=skill.raw_skill_name,
            )
        )
    return tuple(items)


def _require_pack_evidence(
    *,
    evidence: Sequence[ExperienceEvidence],
    profile_id: str,
    profile_version_id: str,
    scope: str,
) -> None:
    if not evidence:
        raise NoVerifiedCareerFactsError(
            profile_id=profile_id,
            profile_version_id=profile_version_id,
            reason=f"missing_{scope}_evidence",
        )


def _pack_items_for_evidence(
    *,
    evidence: Sequence[ExperienceEvidence],
    scope: str,
    content: str,
) -> tuple[VerifiedEvidencePackItem, ...]:
    return tuple(
        VerifiedEvidencePackItem(
            evidence_id=item.id,
            scope=scope,
            verification_status=item.verification_status,
            content=content,
            source_type=item.source_type,
            source_document_id=item.source_document_id,
            source_excerpt=item.source_excerpt,
            source_locator=item.source_locator,
        )
        for item in evidence
    )


@dataclass(frozen=True, slots=True)
class _ConfirmedProfileFacts:
    """The fully verified fact graph prepared before a short SQLite transaction."""

    experiences: list[Experience]
    achievements: list[ExperienceAchievement]
    skills: list[Skill]
    experience_skills: list[ExperienceSkill]
    evidence: list[ExperienceEvidence]


def _normalize_confirmations(
    confirmations: Sequence[ExperienceFactConfirmation],
) -> tuple[ExperienceFactConfirmation, ...]:
    if isinstance(confirmations, (str, bytes)) or not isinstance(confirmations, Sequence):
        raise ValidationError(
            "confirmations must be a sequence.",
            details={"field": "confirmations"},
        )
    normalized = tuple(confirmations)
    if not normalized:
        raise ValidationError(
            "At least one experience must be explicitly confirmed before publishing.",
            details={"field": "confirmations"},
        )
    if any(not isinstance(item, ExperienceFactConfirmation) for item in normalized):
        raise ValidationError(
            "confirmations must contain ExperienceFactConfirmation values.",
            details={"field": "confirmations"},
        )
    indexes = [item.experience_index for item in normalized]
    if len(set(indexes)) != len(indexes):
        raise ValidationError(
            "Each draft experience can only be confirmed once per command.",
            details={"field": "confirmations"},
        )
    return tuple(sorted(normalized, key=lambda item: item.experience_index))


def _confirmation_source_summary(
    run: ExtractionRun,
    confirmations: Sequence[ExperienceFactConfirmation],
) -> dict[str, Any]:
    return {
        "source": "user_confirmed_extraction_draft",
        "extraction_run_id": run.id,
        "resume_document_id": run.document_id,
        "confirmed_experience_indexes": [item.experience_index for item in confirmations],
    }


def _confirmed_profile_facts(
    *,
    profile_version: ProfileVersion,
    draft: ExtractionDraft,
    document_id: str,
    confirmations: Sequence[ExperienceFactConfirmation],
    confirmed_at: datetime,
) -> _ConfirmedProfileFacts:
    """Copy only explicit draft selections into verified, document-backed facts."""
    experiences: list[Experience] = []
    achievements: list[ExperienceAchievement] = []
    skills: list[Skill] = []
    experience_skills: list[ExperienceSkill] = []
    evidence: list[ExperienceEvidence] = []
    skill_ids: dict[tuple[str, str], str] = {}

    for confirmation in confirmations:
        try:
            draft_experience = draft.experiences[confirmation.experience_index]
        except IndexError as exc:
            raise ValidationError(
                "A confirmation references an experience outside the extraction draft.",
                details={"experience_index": confirmation.experience_index},
            ) from exc
        experience_id = str(uuid4())
        experience = Experience(
            id=experience_id,
            profile_version_id=profile_version.id,
            organization=draft_experience.organization,
            role=draft_experience.role,
            date_range=draft_experience.date_range,
            summary=draft_experience.summary,
            verification_status=VerificationStatus.VERIFIED,
            created_at=confirmed_at,
        )
        experiences.append(experience)
        evidence.extend(
            _verified_document_evidence(
                experience_id=experience.id,
                document_id=document_id,
                draft_evidence=draft_experience.evidence,
                confirmed_at=confirmed_at,
            )
        )
        evidence.append(
            _user_confirmation_evidence(
                experience_id=experience.id,
                assertion_text=f"{experience.organization}: {experience.role}",
                confirmed_at=confirmed_at,
            )
        )

        for achievement_index in confirmation.achievement_indexes:
            try:
                draft_achievement = draft_experience.achievements[achievement_index]
            except IndexError as exc:
                raise ValidationError(
                    "A confirmation references an achievement outside its draft experience.",
                    details={
                        "experience_index": confirmation.experience_index,
                        "achievement_index": achievement_index,
                    },
                ) from exc
            achievement_id = str(uuid4())
            achievement = ExperienceAchievement(
                id=achievement_id,
                experience_id=experience.id,
                action_text=draft_achievement.action_text,
                outcome_text=draft_achievement.outcome_text,
                metric_value=draft_achievement.metric_value,
                metric_unit=draft_achievement.metric_unit,
                metric_source_document_id=(
                    document_id if draft_achievement.metric_value is not None else None
                ),
                verification_status=VerificationStatus.VERIFIED,
                created_at=confirmed_at,
            )
            achievements.append(achievement)
            evidence.extend(
                _verified_document_evidence(
                    experience_id=experience.id,
                    experience_achievement_id=achievement.id,
                    document_id=document_id,
                    draft_evidence=draft_achievement.evidence,
                    confirmed_at=confirmed_at,
                )
            )
            evidence.append(
                _user_confirmation_evidence(
                    experience_id=experience.id,
                    experience_achievement_id=achievement.id,
                    assertion_text=achievement.action_text,
                    confirmed_at=confirmed_at,
                )
            )

        for skill_index in confirmation.skill_indexes:
            try:
                draft_skill = draft_experience.skills[skill_index]
            except IndexError as exc:
                raise ValidationError(
                    "A confirmation references a skill outside its draft experience.",
                    details={
                        "experience_index": confirmation.experience_index,
                        "skill_index": skill_index,
                    },
                ) from exc
            canonical_name = draft_skill.canonical_name or draft_skill.raw_skill_name
            skill_key = (canonical_name, "")
            skill_id = skill_ids.get(skill_key)
            if skill_id is None:
                skill_id = str(uuid4())
                skill_ids[skill_key] = skill_id
                skills.append(
                    Skill(
                        id=skill_id,
                        canonical_name=canonical_name,
                        taxonomy_ref="",
                        created_at=confirmed_at,
                    )
                )
            experience_skills.append(
                ExperienceSkill(
                    id=str(uuid4()),
                    experience_id=experience.id,
                    skill_id=skill_id,
                    raw_skill_name=draft_skill.raw_skill_name,
                    proficiency=draft_skill.proficiency,
                    verification_status=VerificationStatus.VERIFIED,
                    created_at=confirmed_at,
                )
            )
            evidence.extend(
                _verified_document_evidence(
                    experience_id=experience.id,
                    document_id=document_id,
                    draft_evidence=draft_skill.evidence,
                    confirmed_at=confirmed_at,
                )
            )
            evidence.append(
                _user_confirmation_evidence(
                    experience_id=experience.id,
                    assertion_text=draft_skill.raw_skill_name,
                    confirmed_at=confirmed_at,
                )
            )
    return _ConfirmedProfileFacts(
        experiences=experiences,
        achievements=achievements,
        skills=skills,
        experience_skills=experience_skills,
        evidence=evidence,
    )


def _verified_document_evidence(
    *,
    experience_id: str,
    document_id: str,
    draft_evidence: Sequence[Any],
    confirmed_at: datetime,
    experience_achievement_id: str | None = None,
) -> list[ExperienceEvidence]:
    return [
        ExperienceEvidence(
            id=str(uuid4()),
            experience_id=experience_id,
            experience_achievement_id=experience_achievement_id,
            source_type=EvidenceSourceType.RESUME_DOCUMENT,
            source_document_id=document_id,
            source_excerpt=item.source_excerpt,
            source_locator=item.source_locator,
            confidence=item.confidence,
            verification_status=VerificationStatus.VERIFIED,
            user_verified=True,
            verified_at=confirmed_at,
            created_at=confirmed_at,
        )
        for item in draft_evidence
    ]


def _user_confirmation_evidence(
    *,
    experience_id: str,
    assertion_text: str,
    confirmed_at: datetime,
    experience_achievement_id: str | None = None,
) -> ExperienceEvidence:
    """Record the affirmative user action without claiming it came from the resume."""
    return ExperienceEvidence.user_assertion(
        id=str(uuid4()),
        experience_id=experience_id,
        experience_achievement_id=experience_achievement_id,
        assertion_text=assertion_text,
        created_at=confirmed_at,
        confirmed_at=confirmed_at,
    )


def _source_path(file_ref: Path | str) -> Path:
    if isinstance(file_ref, Path):
        path = file_ref
    elif isinstance(file_ref, str) and file_ref.strip():
        path = Path(file_ref)
    else:
        raise ValidationError(
            "file_ref must be a non-blank local file path.",
            details={"field": "file_ref"},
        )
    if path.exists() and path.is_dir():
        raise ValidationError(
            "file_ref must point to a file, not a directory.",
            details={"field": "file_ref", "file_ref": str(path)},
        )
    return path


def _read_source_file(source_path: Path) -> bytes:
    try:
        return source_path.read_bytes()
    except FileNotFoundError as exc:
        raise ValidationError(
            "file_ref does not point to a readable local file.",
            details={"field": "file_ref", "file_ref": str(source_path)},
        ) from exc
    except OSError as exc:
        raise ValidationError(
            "file_ref does not point to a readable local file.",
            details={"field": "file_ref", "file_ref": str(source_path)},
        ) from exc


def _normalize_metadata(
    metadata: Mapping[str, Any],
    source_path: Path,
) -> tuple[dict[str, Any], str]:
    if not isinstance(metadata, Mapping):
        raise ValidationError("metadata must be an object.", details={"field": "metadata"})
    try:
        normalized = json.loads(
            json.dumps(
                _json_value(metadata),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "metadata must contain JSON-compatible values.",
            details={"field": "metadata", "reason": str(exc)},
        ) from exc
    if not isinstance(normalized, dict):
        raise ValidationError("metadata must be an object.", details={"field": "metadata"})
    declared_mime_type = normalized.get("mime_type")
    if declared_mime_type is None:
        mime_type = _mime_type_from_filename(source_path)
        normalized["mime_type"] = mime_type
    elif isinstance(declared_mime_type, str) and declared_mime_type.strip():
        mime_type = declared_mime_type.strip().lower()
        normalized["mime_type"] = mime_type
    else:
        raise ValidationError(
            "metadata.mime_type must be a non-blank string when provided.",
            details={"field": "metadata.mime_type"},
        )
    return normalized, mime_type


def _mime_type_from_filename(source_path: Path) -> str:
    suffix = source_path.suffix.lower()
    if suffix == ".md":
        return "text/markdown"
    if suffix == ".txt":
        return "text/plain"
    guessed, _ = mimetypes.guess_type(source_path.name)
    if guessed is None:
        raise ValidationError(
            "metadata.mime_type is required for an unrecognised file extension.",
            details={"field": "metadata.mime_type", "file_ref": str(source_path)},
        )
    return guessed.lower()


def _safe_extraction_error(exc: Exception) -> str:
    if isinstance(exc, ApplicationError):
        message = exc.message.strip() or "Text extraction failed."
        return f"{exc.code}: {message}"[:1000]
    return "unexpected_extraction_error: Text extraction did not complete."


def _require_draft_locators(draft: ExtractionDraft) -> None:
    """Require every provider-proposed fact to retain a source locator.

    The base draft schema allows an unsupported fact to be marked
    ``needs_clarification``.  The CareerExtractionPort contract for this slice
    is stricter: every fact it emits must still point back into the imported
    text so later user review can inspect the proposal's provenance.
    """
    for experience_index, experience in enumerate(draft.experiences):
        _require_item_locator(experience.evidence, f"experiences[{experience_index}]")
        for achievement_index, achievement in enumerate(experience.achievements):
            _require_item_locator(
                achievement.evidence,
                f"experiences[{experience_index}].achievements[{achievement_index}]",
            )
        for skill_index, skill in enumerate(experience.skills):
            _require_item_locator(
                skill.evidence,
                f"experiences[{experience_index}].skills[{skill_index}]",
            )


def _require_item_locator(evidence: tuple[Any, ...], location: str) -> None:
    if not any(getattr(item, "source_locator", None) for item in evidence):
        raise ValidationError(
            "Every extraction candidate requires a source locator.",
            details={"location": location, "field": "evidence.source_locator"},
        )


def _draft_to_payload(draft: ExtractionDraft) -> dict[str, Any]:
    """Serialize typed draft values, retaining only their unverified schema fields."""
    return asdict(draft)


def _extraction_error_summary(exc: Exception) -> dict[str, str]:
    """Store a bounded, adapter-safe reason while preserving the original exception to callers."""
    if isinstance(exc, ApplicationError):
        return {
            "code": exc.code,
            "message": (exc.message.strip() or "Career extraction failed.")[:1000],
        }
    return {
        "code": "unexpected_extraction_error",
        "message": "Career extraction did not complete.",
    }


def _require_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            f"{field_name} must be a non-blank string.",
            details={"field": field_name},
        )
    return value.strip()


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value
