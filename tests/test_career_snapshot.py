"""Integration tests for read-only, verified Career profile projections."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from job_search_assistant.app_services import (
    ConfirmExperienceFacts,
    GetCareerProfileSnapshot,
    GetVerifiedEvidencePack,
    ImportResumeDocument,
    StartExtractionRun,
    build_foundation,
)
from job_search_assistant.career import (
    EXTRACTION_DRAFT_SCHEMA_VERSION,
    CareerProfile,
    ExperienceFactConfirmation,
    ProfileVersion,
)
from job_search_assistant.core import (
    ApplicationError,
    ConflictError,
    NotFoundError,
    RequestContext,
    ValidationError,
)
from job_search_assistant.infrastructure.files import (
    DeterministicCareerExtractionProvider,
    FileSystemDocumentStorage,
    PlainTextResumeExtractor,
)
from job_search_assistant.infrastructure.sqlite import SQLiteCareerStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_PATH = PROJECT_ROOT / "migrations"


class CareerSnapshotTestCase(unittest.TestCase):
    """Keep evidence packs traceable and snapshots minimal without any writes."""

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self._temporary_directory.name)
        foundation = build_foundation(
            database_path=self.workspace / "data" / "assistant.sqlite",
            migrations_path=MIGRATIONS_PATH,
        )
        self.database = foundation.database
        self.storage = FileSystemDocumentStorage(
            self.workspace / ".job-search-assistant" / "documents"
        )
        self.store = SQLiteCareerStore(self.database)
        self.context = RequestContext.create(
            actor_id="user-1",
            source="career-snapshot-test",
            correlation_id="career-snapshot-correlation",
        )
        self.import_service = ImportResumeDocument(
            store=self.store,
            storage=self.storage,
            text_extractor=PlainTextResumeExtractor(self.storage),
        )
        self.extraction_service = StartExtractionRun(
            store=self.store,
            storage=self.storage,
            extraction_provider=DeterministicCareerExtractionProvider(payload=_draft_payload()),
        )
        self.confirmation_service = ConfirmExperienceFacts(store=self.store, storage=self.storage)
        self.pack_service = GetVerifiedEvidencePack(store=self.store)
        self.snapshot_service = GetCareerProfileSnapshot(store=self.store)
        now = datetime.now(UTC)
        self.profile = self.store.create_career_profile(
            profile=CareerProfile(
                id="profile-1",
                owner_id="user-1",
                display_name="Ada Lovelace",
                created_at=now,
                updated_at=now,
            )
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_pack_excludes_unconfirmed_drafts_and_carries_evidence_provenance(self) -> None:
        version_id = self._publish_first_experience()

        pack = self.pack_service.execute(
            profile_id=self.profile.id,
            profile_version_id=version_id,
            task_context="job-matching",
        )

        self.assertEqual(self.profile.id, pack.profile_id)
        self.assertEqual(version_id, pack.profile_version_id)
        self.assertEqual("job-matching", pack.task_context)
        self.assertTrue(pack.items)
        self.assertEqual(
            {"experience", "achievement", "skill"},
            {item.scope for item in pack.items},
        )
        for item in pack.items:
            self.assertTrue(item.evidence_id)
            self.assertEqual("verified", item.verification_status.value)
            self.assertNotIn("Unconfirmed", item.content)

    def test_pack_rejects_unverified_rows_inserted_outside_the_store(self) -> None:
        version_id = self._publish_first_experience()
        now = datetime.now(UTC).isoformat()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO experiences (
                    id, profile_version_id, organization, role, verification_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("bypassed-draft", version_id, "Unconfirmed Ventures", "Analyst", "draft", now),
            )

        with self.assertRaises(ValidationError) as raised:
            self.pack_service.execute(profile_id=self.profile.id, profile_version_id=version_id)
        self.assertEqual("validation_error", raised.exception.code)
        self.assertEqual("draft", raised.exception.details["verification_status"])

        with self.assertRaises(ValidationError):
            self.snapshot_service.execute(profile_id=self.profile.id, profile_version_id=version_id)

    def test_pack_and_snapshot_are_read_only_and_snapshot_is_minimal(self) -> None:
        version_id = self._publish_first_experience()
        before = self._table_counts()

        pack = self.pack_service.execute(profile_id=self.profile.id, profile_version_id=version_id)
        snapshot = self.snapshot_service.execute(
            profile_id=self.profile.id,
            profile_version_id=version_id,
        )

        self.assertEqual(before, self._table_counts())
        self.assertTrue(pack.items)
        self.assertEqual("Ada Lovelace", snapshot.display_name)
        self.assertEqual(1, len(snapshot.experiences))
        self.assertEqual("Analytical Engines", snapshot.experiences[0].organization)
        rendered = repr(asdict(snapshot))
        for internal_field in (
            "file_ref",
            "text_ref",
            "output_ref",
            "raw_json",
            "audit_events",
            "source_summary",
        ):
            self.assertNotIn(internal_field, rendered)

    def test_invalid_profile_version_and_empty_version_have_structured_errors(self) -> None:
        with self.assertRaises(NotFoundError) as unknown_profile:
            self.pack_service.execute(profile_id="unknown-profile")
        self.assertEqual("not_found", unknown_profile.exception.code)

        with self.assertRaises(NotFoundError) as unknown_version:
            self.snapshot_service.execute(
                profile_id=self.profile.id,
                profile_version_id="unknown-version",
            )
        self.assertEqual("not_found", unknown_version.exception.code)

        other = self.store.create_career_profile(
            profile=CareerProfile(
                id="profile-2",
                owner_id="user-1",
                display_name="Grace Hopper",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        version_id = self._publish_first_experience()
        with self.assertRaises(ConflictError) as wrong_profile:
            self.pack_service.execute(profile_id=other.id, profile_version_id=version_id)
        self.assertEqual("conflict", wrong_profile.exception.code)

        empty_version = ProfileVersion.next(
            id="empty-profile-version",
            previous_version=self.store.get_profile_version(profile_version_id=version_id),
            created_at=datetime.now(UTC),
        )
        self.store.publish_profile_version(
            profile=self.store.get_career_profile(profile_id=self.profile.id),
            profile_version=empty_version,
            experiences=(),
            achievements=(),
            skills=(),
            experience_skills=(),
            evidence=(),
        )
        with self.assertRaises(ApplicationError) as no_facts:
            self.pack_service.execute(
                profile_id=self.profile.id,
                profile_version_id=empty_version.id,
            )
        self.assertEqual("no_verified_career_facts", no_facts.exception.code)

    def _publish_first_experience(self) -> str:
        document_count = self._table_counts()["resume_documents"]
        source = self.workspace / "incoming" / f"resume-{document_count}.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("Ada Lovelace\nBackend Engineer\n", encoding="utf-8")
        document = self.import_service.execute(
            file_ref=source,
            metadata={"mime_type": "text/plain"},
            idempotency_key=f"snapshot-import-{source.stem}",
            context=self.context,
        )
        extraction = self.extraction_service.execute(
            document_id=document.document_id,
            idempotency_key=f"snapshot-extraction-{source.stem}",
            context=self.context,
        )
        return self.confirmation_service.execute(
            profile_id=self.profile.id,
            extraction_run_id=extraction.run_id,
            confirmations=(
                ExperienceFactConfirmation(0, achievement_indexes=(0,), skill_indexes=(0,)),
            ),
            idempotency_key=f"snapshot-confirm-{source.stem}",
            context=self.context,
        ).profile_version_id

    def _table_counts(self) -> dict[str, int]:
        tables = [
            str(row["name"])
            for row in self.database.fetch_all(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            table: int(
                self.database.fetch_all(f"SELECT COUNT(*) AS count FROM {table}")[0]["count"]
            )
            for table in tables
        }


def _draft_payload() -> dict[str, object]:
    return {
        "schema_version": EXTRACTION_DRAFT_SCHEMA_VERSION,
        "experiences": [
            {
                "organization": "Analytical Engines",
                "role": "Programmer",
                "verification_status": "draft",
                "evidence": [{"source_locator": "line:1-2"}],
                "achievements": [
                    {
                        "action_text": "Wrote notes on the engine.",
                        "verification_status": "draft",
                        "evidence": [{"source_locator": "line:1"}],
                    }
                ],
                "skills": [
                    {
                        "raw_skill_name": "Mathematics",
                        "verification_status": "draft",
                        "evidence": [{"source_locator": "line:2"}],
                    }
                ],
            },
            {
                "organization": "Unconfirmed Ventures",
                "role": "Speculative Analyst",
                "verification_status": "needs_clarification",
                "evidence": [{"source_locator": "line:3"}],
            },
        ],
    }


if __name__ == "__main__":
    unittest.main()
