"""Persistence port for Career aggregates.

This module is deliberately only a contract.  SQLite implementation and
application services belong to later Phase 2 slices, keeping the domain free
of concrete I/O dependencies.
"""

from __future__ import annotations

from typing import Protocol, Sequence

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


class CareerStore(Protocol):
    """Transactional persistence boundary for future Career application services."""

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

    def create_extraction_run(self, *, run: ExtractionRun) -> ExtractionRun:
        """Append a new extraction run instead of overwriting prior output."""

    def get_extraction_run(self, *, run_id: str) -> ExtractionRun:
        """Return one extraction run."""

    def update_extraction_run_status(self, *, run: ExtractionRun) -> ExtractionRun:
        """Persist a valid extraction-run state-machine transition."""

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
