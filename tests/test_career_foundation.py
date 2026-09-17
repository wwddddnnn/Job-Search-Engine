"""S1 tests for the Career Foundation migration and pure domain contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from job_search_assistant.app_services import build_foundation
from job_search_assistant.career import (
    EXTRACTION_DRAFT_SCHEMA_VERSION,
    CareerProfile,
    DocumentStatus,
    EvidenceSourceType,
    ExperienceEvidence,
    ExtractionRunStatus,
    ProfileVersion,
    ResumeDocument,
    VerificationStatus,
    validate_extraction_draft,
)
from job_search_assistant.core import InvalidStateError, ValidationError
from job_search_assistant.infrastructure.sqlite import SQLiteDatabase


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_MIGRATIONS = PROJECT_ROOT / "migrations"
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


class CareerFoundationMigrationTestCase(unittest.TestCase):
    """Exercise the existing checksum-protected migration mechanism with 0004."""

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self._temporary_directory.name)
        self.migrations_path = self.workspace / "migrations"
        shutil.copytree(SOURCE_MIGRATIONS, self.migrations_path)
        self.database_path = self.workspace / "data" / "assistant.sqlite"
        self.services = build_foundation(
            database_path=self.database_path,
            migrations_path=self.migrations_path,
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_0004_is_idempotent_and_creates_all_career_tables(self) -> None:
        self.services.database.migrate()
        tables = {
            str(row["name"])
            for row in self.services.database.fetch_all(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue(
            {
                "resume_documents",
                "document_texts",
                "llm_extraction_runs",
                "career_profiles",
                "profile_versions",
                "experiences",
                "experience_achievements",
                "skills",
                "experience_skills",
                "experience_evidence",
            }.issubset(tables)
        )
        versions = self.services.database.fetch_all("SELECT version FROM schema_migrations ORDER BY version")
        self.assertEqual(
            ["0001", "0002", "0003", "0004", "0005", "0006"],
            [str(row["version"]) for row in versions],
        )

    def test_modified_applied_0004_is_rejected_by_checksum(self) -> None:
        migration = self.migrations_path / "0004_career_foundation.sql"
        migration.write_text(migration.read_text(encoding="utf-8") + "\n-- modified\n", encoding="utf-8")

        with self.assertRaisesRegex(Exception, "applied database migration was modified"):
            SQLiteDatabase(self.database_path, self.migrations_path).migrate()

    def test_profile_versions_must_increment_and_cannot_be_updated(self) -> None:
        with self.services.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO career_profiles (id, owner_id, tenant_id, display_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("profile-1", "owner-1", None, "Ada Lovelace", NOW.isoformat(), NOW.isoformat()),
            )
            connection.execute(
                """
                INSERT INTO profile_versions (id, profile_id, version, source_summary_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("profile-version-1", "profile-1", 1, "{}", NOW.isoformat()),
            )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "increment by 1"):
            with self.services.database.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO profile_versions (id, profile_id, version, source_summary_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("profile-version-3", "profile-1", 3, "{}", NOW.isoformat()),
                )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            with self.services.database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE profile_versions SET source_summary_json = ? WHERE id = ?",
                    ('{"changed":true}', "profile-version-1"),
                )


class CareerFoundationDomainTestCase(unittest.TestCase):
    """Validate the state machine, evidence rule, and immutable version objects."""

    def test_all_legal_state_transitions_are_available(self) -> None:
        imported = _document()
        extracted = imported.mark_text_extracted(text_ref="documents/text/hash.txt", extractor_version="parser-v1")
        self.assertEqual(DocumentStatus.TEXT_EXTRACTED, extracted.status)
        failed_document = _document(document_id="document-failed").mark_extraction_failed(error="unsupported format")
        self.assertEqual(DocumentStatus.EXTRACTION_FAILED, failed_document.status)

        run = extracted.start_extraction_run(
            run_id="run-1",
            input_hash="input-hash",
            model="fake-model",
            prompt_version="prompt-v1",
            schema_version=EXTRACTION_DRAFT_SCHEMA_VERSION,
            started_at=NOW,
        )
        self.assertEqual(ExtractionRunStatus.DRAFT_EXTRACTING, run.status)
        ready = run.mark_draft_ready(output_ref="documents/drafts/run-1.json", completed_at=NOW)
        self.assertEqual(ExtractionRunStatus.DRAFT_READY, ready.status)
        reviewing = ready.begin_review()
        self.assertEqual(ExtractionRunStatus.UNDER_REVIEW, reviewing.status)
        reworked = reviewing.return_to_draft()
        self.assertEqual(ExtractionRunStatus.DRAFT_READY, reworked.status)
        published = reworked.begin_review().publish_profile_version(
            profile_version_id="profile-version-1",
            completed_at=NOW,
        )
        self.assertEqual(ExtractionRunStatus.PROFILE_VERSION_PUBLISHED, published.status)

        failed_run = extracted.start_extraction_run(
            run_id="run-failed",
            input_hash="input-hash-2",
            model="fake-model",
            prompt_version="prompt-v1",
            schema_version=EXTRACTION_DRAFT_SCHEMA_VERSION,
            started_at=NOW,
        ).mark_draft_failed(error_summary={"code": "schema_invalid"}, completed_at=NOW)
        self.assertEqual(ExtractionRunStatus.DRAFT_FAILED, failed_run.status)

    def test_illegal_state_transitions_raise_structured_domain_errors(self) -> None:
        imported = _document()
        with self.assertRaises(InvalidStateError) as imported_run:
            imported.start_extraction_run(
                run_id="run-invalid",
                input_hash="input-hash",
                model="fake-model",
                prompt_version="prompt-v1",
                schema_version=EXTRACTION_DRAFT_SCHEMA_VERSION,
                started_at=NOW,
            )
        self.assertEqual("invalid_state", imported_run.exception.code)
        self.assertEqual("imported", imported_run.exception.details["current_state"])

        extracted = imported.mark_text_extracted(text_ref="documents/text/hash.txt", extractor_version="parser-v1")
        with self.assertRaises(InvalidStateError) as parser_repeat:
            extracted.mark_extraction_failed(error="too late")
        self.assertEqual("invalid_state", parser_repeat.exception.code)

        ready = extracted.start_extraction_run(
            run_id="run-2",
            input_hash="input-hash",
            model="fake-model",
            prompt_version="prompt-v1",
            schema_version=EXTRACTION_DRAFT_SCHEMA_VERSION,
            started_at=NOW,
        ).mark_draft_ready(output_ref="documents/drafts/run-2.json", completed_at=NOW)
        with self.assertRaises(InvalidStateError) as ready_publish:
            ready.publish_profile_version(profile_version_id="profile-version-1", completed_at=NOW)
        self.assertEqual("invalid_state", ready_publish.exception.code)

        failed = extracted.start_extraction_run(
            run_id="run-3",
            input_hash="input-hash-3",
            model="fake-model",
            prompt_version="prompt-v1",
            schema_version=EXTRACTION_DRAFT_SCHEMA_VERSION,
            started_at=NOW,
        ).mark_draft_failed(error_summary={"code": "model_error"}, completed_at=NOW)
        with self.assertRaises(InvalidStateError) as failed_review:
            failed.begin_review()
        self.assertEqual("invalid_state", failed_review.exception.code)

    def test_facts_without_document_sources_are_user_assertions(self) -> None:
        with self.assertRaises(ValidationError) as masquerading_resume:
            ExperienceEvidence(
                id="evidence-invalid",
                experience_id="experience-1",
                source_type=EvidenceSourceType.RESUME_DOCUMENT,
                verification_status=VerificationStatus.NEEDS_CLARIFICATION,
                user_verified=False,
                created_at=NOW,
                source_excerpt="Led a migration.",
            )
        self.assertEqual("validation_error", masquerading_resume.exception.code)
        self.assertEqual(
            "user_assertion",
            masquerading_resume.exception.details["required_source_type"],
        )

        assertion = ExperienceEvidence.user_assertion(
            id="evidence-user-assertion",
            experience_id="experience-1",
            assertion_text="I mentored three engineers.",
            created_at=NOW,
            confirmed_at=NOW,
        )
        self.assertEqual(EvidenceSourceType.USER_ASSERTION, assertion.source_type)
        self.assertIsNone(assertion.source_document_id)
        self.assertTrue(assertion.user_verified)
        self.assertEqual(NOW, assertion.verified_at)

    def test_profile_versions_increment_without_mutating_history(self) -> None:
        first = ProfileVersion.initial(
            id="profile-version-1",
            profile_id="profile-1",
            source_summary={"confirmed_items": 2},
            created_at=NOW,
        )
        second = ProfileVersion.next(
            id="profile-version-2",
            previous_version=first,
            source_summary={"confirmed_items": 3},
            created_at=NOW,
        )
        self.assertEqual(1, first.version)
        self.assertEqual(2, second.version)
        self.assertEqual({"confirmed_items": 2}, dict(first.source_summary))

        profile = CareerProfile(
            id="profile-1",
            owner_id="owner-1",
            display_name="Ada Lovelace",
            created_at=NOW,
            updated_at=NOW,
        )
        after_first = profile.with_published_version(profile_version=first, previous_version=None, updated_at=NOW)
        after_second = after_first.with_published_version(
            profile_version=second,
            previous_version=first,
            updated_at=NOW,
        )
        self.assertEqual("profile-version-2", after_second.current_version_id)
        with self.assertRaises(FrozenInstanceError):
            first.version = 99  # type: ignore[misc]
        with self.assertRaises(TypeError):
            first.source_summary["confirmed_items"] = 99  # type: ignore[index]

    def test_extraction_draft_schema_rejects_verified_llm_output(self) -> None:
        payload = {
            "schema_version": EXTRACTION_DRAFT_SCHEMA_VERSION,
            "experiences": [
                {
                    "organization": "Analytical Engines",
                    "role": "Programmer",
                    "verification_status": "verified",
                    "evidence": [{"source_excerpt": "Wrote notes on the engine."}],
                }
            ],
        }
        with self.assertRaises(ValidationError):
            validate_extraction_draft(payload)


def _document(*, document_id: str = "document-1") -> ResumeDocument:
    return ResumeDocument(
        id=document_id,
        file_ref=f"documents/{document_id}.pdf",
        mime_type="application/pdf",
        content_hash=f"hash-{document_id}",
        uploaded_at=NOW,
    )


if __name__ == "__main__":
    unittest.main()
