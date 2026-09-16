"""Persistence port for Career aggregates.

This module is deliberately only a contract.  SQLite implementation and
application services belong to later Phase 2 slices, keeping the domain free
of concrete I/O dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from job_search_assistant.core.context import RequestContext
from job_search_assistant.core.idempotency import IdempotencyReservationState

from job_search_assistant.career.types import (
    CareerProfile,
    Experience,
    ExperienceAchievement,
    ExperienceEvidence,
    ExperienceSkill,
    ExtractionRun,
    ProfileVersion,
    ResumeDocument,
    ResumeText,
    Skill,
)


@dataclass(frozen=True, slots=True)
class ResumeImportReservation:
    """The durable document associated with one ImportResumeDocument command."""

    state: IdempotencyReservationState
    idempotency_record_id: str
    document: ResumeDocument


@dataclass(frozen=True, slots=True)
class ExtractionRunReservation:
    """The durable extraction run associated with one StartExtractionRun command."""

    state: IdempotencyReservationState
    idempotency_record_id: str
    run: ExtractionRun


@dataclass(frozen=True, slots=True)
class ExperienceConfirmationReservation:
    """The durable confirmation command checkpoint for one profile publication."""

    state: IdempotencyReservationState
    idempotency_record_id: str
    profile_version_id: str | None = None


class CareerStore(Protocol):
    """Transactional persistence boundary for Career application services."""

    def peek_resume_import(
        self,
        *,
        idempotency_key: str,
        request: Mapping[str, Any],
        context: RequestContext,
    ) -> ResumeImportReservation | None:
        """Read a matching import reservation without changing its state or creating a row."""

    def reserve_resume_import(
        self,
        *,
        document: ResumeDocument,
        idempotency_key: str,
        request: Mapping[str, Any],
        context: RequestContext,
    ) -> ResumeImportReservation:
        """Reserve an import and persist its initial document/audit state.

        Repeating an ``IN_PROGRESS`` key returns its original document rather
        than inserting a second row, so an interrupted import resumes with the
        same controlled-file reference.
        """

    def finalize_resume_import(
        self,
        *,
        document: ResumeDocument,
        text: ResumeText | None,
        idempotency_record_id: str,
        context: RequestContext,
    ) -> ResumeDocument:
        """Atomically save a terminal parse state, audit event, and replay response."""

    def create_resume_document(self, *, document: ResumeDocument) -> ResumeDocument:
        """Persist a newly imported document without replacing historical documents."""

    def get_resume_document(self, *, document_id: str) -> ResumeDocument:
        """Return one document or raise the application's structured not-found error."""

    def update_resume_document_status(self, *, document: ResumeDocument) -> ResumeDocument:
        """Persist a valid document state-machine transition."""

    def append_resume_text(self, *, text: ResumeText) -> ResumeText:
        """Append one extractor result; historical text results are retained."""

    def list_resume_texts(self, *, document_id: str) -> Sequence[ResumeText]:
        """List append-only text extractions for one document."""

    def peek_extraction_run(
        self,
        *,
        idempotency_key: str,
        request: Mapping[str, Any],
        context: RequestContext,
    ) -> ExtractionRunReservation | None:
        """Read a matching extraction reservation without changing its state or creating a row."""

    def reserve_extraction_run(
        self,
        *,
        run: ExtractionRun,
        idempotency_key: str,
        request: Mapping[str, Any],
        context: RequestContext,
    ) -> ExtractionRunReservation:
        """Atomically reserve an extraction key and append its initial run checkpoint.

        Repeating an ``IN_PROGRESS`` key returns the existing run rather than
        creating a second run, allowing the work to resume safely.
        """

    def finalize_extraction_run(
        self,
        *,
        run: ExtractionRun,
        idempotency_record_id: str,
        context: RequestContext,
    ) -> ExtractionRun:
        """Persist a terminal draft outcome, audit it, and complete its idempotency record."""

    def persist_extraction_review_transition(
        self,
        *,
        run: ExtractionRun,
        idempotency_key: str,
        request: Mapping[str, Any],
        context: RequestContext,
    ) -> ExtractionRun:
        """Persist an idempotent review transition owned by an application command.

        ``ConfirmExperienceFacts`` is the current caller.  Adapters and UI
        code must never call this persistence primitive directly: they must
        supply an application command carrying its own scope/key and actor.
        The implementation uses the confirmation command's idempotency scope
        and key to prevent an unaudited, replay-unsafe state-write path.
        """

    def create_skill(self, *, skill: Skill) -> Skill:
        """Persist a normalized skill using ``''`` for a missing taxonomy reference."""

    def get_extraction_run(self, *, run_id: str) -> ExtractionRun:
        """Return one extraction run."""

    def create_career_profile(self, *, profile: CareerProfile) -> CareerProfile:
        """Persist a stable profile identity."""

    def get_career_profile(self, *, profile_id: str) -> CareerProfile:
        """Return one career profile."""

    def publish_profile_version(
        self,
        *,
        profile: CareerProfile,
        profile_version: ProfileVersion,
        experiences: Sequence[Experience],
        achievements: Sequence[ExperienceAchievement],
        skills: Sequence[Skill],
        experience_skills: Sequence[ExperienceSkill],
        evidence: Sequence[ExperienceEvidence],
    ) -> CareerProfile:
        """Atomically append an immutable profile version and move the current pointer."""

    def get_profile_version(self, *, profile_version_id: str) -> ProfileVersion:
        """Return one immutable profile version."""

    def peek_experience_confirmation(
        self,
        *,
        idempotency_key: str,
        request: Mapping[str, Any],
        context: RequestContext,
    ) -> ExperienceConfirmationReservation | None:
        """Read a confirmation replay checkpoint before loading its draft artifact."""

    def reserve_experience_confirmation(
        self,
        *,
        profile_id: str,
        extraction_run_id: str,
        idempotency_key: str,
        request: Mapping[str, Any],
        context: RequestContext,
    ) -> ExperienceConfirmationReservation:
        """Reserve the idempotent confirmation command before the final transaction."""

    def finalize_experience_confirmation(
        self,
        *,
        profile: CareerProfile,
        profile_version: ProfileVersion,
        run: ExtractionRun,
        experiences: Sequence[Experience],
        achievements: Sequence[ExperienceAchievement],
        skills: Sequence[Skill],
        experience_skills: Sequence[ExperienceSkill],
        evidence: Sequence[ExperienceEvidence],
        idempotency_record_id: str,
        idempotency_key: str,
        request: Mapping[str, Any],
        context: RequestContext,
    ) -> ProfileVersion:
        """Atomically publish confirmed facts, audit before/after, and complete replay."""
