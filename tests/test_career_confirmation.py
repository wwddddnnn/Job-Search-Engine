"""Integration tests for the S4 explicit fact-confirmation workflow."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from job_search_assistant.app_services import (
    ConfirmExperienceFacts,
    ImportResumeDocument,
    StartExtractionRun,
    build_foundation,
)
from job_search_assistant.career import (
    EXTRACTION_DRAFT_SCHEMA_VERSION,
    CareerProfile,
    Experience,
    ExperienceAchievement,
    ExperienceFactConfirmation,
    ExperienceSkill,
    ProfileVersion,
    Skill,
    VerificationStatus,
)
from job_search_assistant.core import (
    ConflictError,
    InvalidStateError,
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


class CareerConfirmationTestCase(unittest.TestCase):
    """Exercise publication, immutable history, and revision audit through real SQLite."""

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
            source="career-confirmation-test",
            correlation_id="career-confirmation-correlation",
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

    def test_only_explicitly_confirmed_items_are_verified_and_published(self) -> None:
        run_id = self._ready_run()

        result = self.confirmation_service.execute(
            profile_id=self.profile.id,
            extraction_run_id=run_id,
            confirmations=(
                ExperienceFactConfirmation(0, achievement_indexes=(0,), skill_indexes=(0,)),
            ),
            idempotency_key="confirm-explicit-only-1",
            context=self.context,
        )

        experiences = self.database.fetch_all(
            "SELECT organization, verification_status FROM experiences "
            "WHERE profile_version_id = ?",
            (result.profile_version_id,),
        )
        self.assertEqual([("Analytical Engines", "verified")], [tuple(row) for row in experiences])
        self.assertEqual(
            ["verified"],
            [
                str(row["verification_status"])
                for row in self.database.fetch_all(
                    "SELECT verification_status FROM experience_achievements"
                )
            ],
        )
        self.assertEqual(
            ["verified"],
            [
                str(row["verification_status"])
                for row in self.database.fetch_all(
                    "SELECT verification_status FROM experience_skills"
                )
            ],
        )
        evidence_types = {
            str(row["source_type"])
            for row in self.database.fetch_all("SELECT source_type FROM experience_evidence")
        }
        self.assertEqual({"resume_document", "user_assertion"}, evidence_types)
        self.assertEqual(
            [],
            self.database.fetch_all(
                "SELECT id FROM experiences WHERE organization = ?",
                ("Unconfirmed Ventures",),
            ),
        )
        draft = self.storage.load_extraction_draft(
            output_ref=self.store.get_extraction_run(run_id=run_id).output_ref or ""
        )
        self.assertEqual("needs_clarification", draft["experiences"][1]["verification_status"])

    def test_published_versions_reject_unverified_fact_rows(self) -> None:
        for status in (
            VerificationStatus.DRAFT,
            VerificationStatus.NEEDS_CLARIFICATION,
            VerificationStatus.REJECTED,
        ):
            with self.subTest(status=status.value):
                profile_version = ProfileVersion.initial(
                    id=f"{status.value}-profile-version",
                    profile_id=self.profile.id,
                    created_at=datetime.now(UTC),
                )
                unverified_experience = Experience(
                    id=f"{status.value}-experience",
                    profile_version_id=profile_version.id,
                    organization="Unverified Company",
                    role="Unverified Role",
                    verification_status=status,
                    created_at=datetime.now(UTC),
                )

                with self.assertRaises(ValidationError) as raised:
                    self.store.publish_profile_version(
                        profile=self.profile,
                        profile_version=profile_version,
                        experiences=(unverified_experience,),
                        achievements=(),
                        skills=(),
                        experience_skills=(),
                        evidence=(),
                    )

                self.assertEqual("validation_error", raised.exception.code)
                self.assertEqual(status.value, raised.exception.details["verification_status"])

        self.assertEqual(
            [],
            self.database.fetch_all("SELECT id FROM profile_versions"),
        )

    def test_published_versions_reject_unverified_skill_rows(self) -> None:
        for status in (
            VerificationStatus.DRAFT,
            VerificationStatus.NEEDS_CLARIFICATION,
            VerificationStatus.REJECTED,
        ):
            with self.subTest(status=status.value):
                profile_version = ProfileVersion.initial(
                    id=f"skill-{status.value}-profile-version",
                    profile_id=self.profile.id,
                    created_at=datetime.now(UTC),
                )
                experience = Experience(
                    id=f"skill-{status.value}-experience",
                    profile_version_id=profile_version.id,
                    organization="Verified Company",
                    role="Verified Role",
                    verification_status=VerificationStatus.VERIFIED,
                    created_at=datetime.now(UTC),
                )
                skill = Skill(
                    id=f"skill-{status.value}",
                    canonical_name="python",
                    created_at=datetime.now(UTC),
                )
                unverified_association = ExperienceSkill(
                    id=f"association-{status.value}",
                    experience_id=experience.id,
                    skill_id=skill.id,
                    raw_skill_name="Python",
                    verification_status=status,
                    created_at=datetime.now(UTC),
                )

                with self.assertRaises(ValidationError) as raised:
                    self.store.publish_profile_version(
                        profile=self.profile,
                        profile_version=profile_version,
                        experiences=(experience,),
                        achievements=(),
                        skills=(skill,),
                        experience_skills=(unverified_association,),
                        evidence=(),
                    )

                self.assertEqual(status.value, raised.exception.details["verification_status"])

    def test_published_versions_reject_unverified_achievement_rows(self) -> None:
        profile_version = ProfileVersion.initial(
            id="achievement-draft-profile-version",
            profile_id=self.profile.id,
            created_at=datetime.now(UTC),
        )
        experience = Experience(
            id="achievement-draft-experience",
            profile_version_id=profile_version.id,
            organization="Verified Company",
            role="Verified Role",
            verification_status=VerificationStatus.VERIFIED,
            created_at=datetime.now(UTC),
        )
        achievement = ExperienceAchievement(
            id="achievement-draft",
            experience_id=experience.id,
            action_text="Draft achievement",
            verification_status=VerificationStatus.DRAFT,
            created_at=datetime.now(UTC),
        )

        with self.assertRaises(ValidationError) as raised:
            self.store.publish_profile_version(
                profile=self.profile,
                profile_version=profile_version,
                experiences=(experience,),
                achievements=(achievement,),
                skills=(),
                experience_skills=(),
                evidence=(),
            )

        self.assertEqual("draft", raised.exception.details["verification_status"])

    def test_publishing_another_profiles_version_is_rejected(self) -> None:
        now = datetime.now(UTC)
        other_profile = self.store.create_career_profile(
            profile=CareerProfile(
                id="profile-2",
                owner_id="user-1",
                display_name="Grace Hopper",
                created_at=now,
                updated_at=now,
            )
        )
        profile_version = ProfileVersion.initial(
            id="profile-1-version-1",
            profile_id=self.profile.id,
            created_at=now,
        )

        with self.assertRaises(ConflictError) as raised:
            self.store.publish_profile_version(
                profile=other_profile,
                profile_version=profile_version,
                experiences=(),
                achievements=(),
                skills=(),
                experience_skills=(),
                evidence=(),
            )

        self.assertEqual(other_profile.id, raised.exception.details["profile_id"])

    def test_same_confirmation_key_replays_the_same_published_version_without_draft_io(
        self,
    ) -> None:
        run_id = self._ready_run()
        request = {
            "profile_id": self.profile.id,
            "extraction_run_id": run_id,
            "confirmations": (ExperienceFactConfirmation(0),),
            "idempotency_key": "confirm-replay-1",
            "context": self.context,
        }
        first = self.confirmation_service.execute(**request)

        with patch.object(
            self.storage,
            "load_extraction_draft",
            side_effect=AssertionError("completed confirmation replay read a draft"),
        ):
            replay = self.confirmation_service.execute(**request)

        self.assertEqual(first, replay)
        self.assertEqual(
            1,
            len(
                self.database.fetch_all(
                    "SELECT id FROM profile_versions WHERE profile_id = ?",
                    (self.profile.id,),
                )
            ),
        )

    def test_empty_or_duplicate_confirmations_are_rejected(self) -> None:
        run_id = self._ready_run()

        with self.assertRaises(ValidationError) as empty:
            self.confirmation_service.execute(
                profile_id=self.profile.id,
                extraction_run_id=run_id,
                confirmations=(),
                idempotency_key="confirm-empty-1",
                context=self.context,
            )
        self.assertEqual("confirmations", empty.exception.details["field"])

        with self.assertRaises(ValidationError) as duplicate:
            self.confirmation_service.execute(
                profile_id=self.profile.id,
                extraction_run_id=run_id,
                confirmations=(ExperienceFactConfirmation(0), ExperienceFactConfirmation(0)),
                idempotency_key="confirm-duplicate-1",
                context=self.context,
            )
        self.assertEqual("confirmations", duplicate.exception.details["field"])

    def test_out_of_range_confirmation_indexes_are_rejected(self) -> None:
        cases = (
            (ExperienceFactConfirmation(99), "experience_index"),
            (ExperienceFactConfirmation(0, achievement_indexes=(99,)), "achievement_index"),
            (ExperienceFactConfirmation(0, skill_indexes=(99,)), "skill_index"),
        )
        for number, (confirmation, field) in enumerate(cases):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError) as raised:
                    self.confirmation_service.execute(
                        profile_id=self.profile.id,
                        extraction_run_id=self._ready_run(),
                        confirmations=(confirmation,),
                        idempotency_key=f"confirm-out-of-range-{number}",
                        context=self.context,
                    )
                self.assertIn(field, raised.exception.details)

    def test_published_history_is_not_rewritten_and_illegal_transition_is_rejected(self) -> None:
        first_run = self._ready_run()
        first = self.confirmation_service.execute(
            profile_id=self.profile.id,
            extraction_run_id=first_run,
            confirmations=(ExperienceFactConfirmation(0, skill_indexes=(0,)),),
            idempotency_key="confirm-history-1",
            context=self.context,
        )
        first_experiences = [
            tuple(row)
            for row in self.database.fetch_all(
                "SELECT id, organization, role FROM experiences WHERE profile_version_id = ?",
                (first.profile_version_id,),
            )
        ]
        first_skills = [
            tuple(row)
            for row in self.database.fetch_all(
                """
                SELECT es.id, es.raw_skill_name, s.canonical_name
                FROM experience_skills AS es
                JOIN skills AS s ON s.id = es.skill_id
                WHERE es.experience_id IN (
                    SELECT id FROM experiences WHERE profile_version_id = ?
                )
                """,
                (first.profile_version_id,),
            )
        ]
        second_run = self._ready_run()
        second = self.confirmation_service.execute(
            profile_id=self.profile.id,
            extraction_run_id=second_run,
            confirmations=(ExperienceFactConfirmation(1),),
            idempotency_key="confirm-history-2",
            context=self.context,
        )

        self.assertEqual(1, first.version)
        self.assertEqual(2, second.version)
        self.assertEqual(
            first_experiences,
            [
                tuple(row)
                for row in self.database.fetch_all(
                    "SELECT id, organization, role FROM experiences WHERE profile_version_id = ?",
                    (first.profile_version_id,),
                )
            ],
        )
        self.assertEqual(
            first_skills,
            [
                tuple(row)
                for row in self.database.fetch_all(
                    """
                    SELECT es.id, es.raw_skill_name, s.canonical_name
                    FROM experience_skills AS es
                    JOIN skills AS s ON s.id = es.skill_id
                    WHERE es.experience_id IN (
                        SELECT id FROM experiences WHERE profile_version_id = ?
                    )
                    """,
                    (first.profile_version_id,),
                )
            ],
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            with self.database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE profile_versions SET source_summary_json = ? WHERE id = ?",
                    ("{}", first.profile_version_id),
                )
        published_run = self.store.get_extraction_run(run_id=first_run)
        with self.assertRaises(InvalidStateError):
            published_run.begin_review()

    def test_revision_audit_has_traceable_before_after_actor_and_correlation(self) -> None:
        run_id = self._ready_run()
        result = self.confirmation_service.execute(
            profile_id=self.profile.id,
            extraction_run_id=run_id,
            confirmations=(ExperienceFactConfirmation(0, achievement_indexes=(0,)),),
            idempotency_key="confirm-audit-1",
            context=self.context,
        )

        row = self.database.fetch_all(
            """
            SELECT action, target_type, target_id, actor_id, correlation_id, before_json, after_json
            FROM audit_events
            WHERE action = ? AND target_id = ?
            """,
            ("career.profile_version.published", self.profile.id),
        )[0]
        before = json.loads(str(row["before_json"]))
        after = json.loads(str(row["after_json"]))
        self.assertEqual("career.profile_version.published", row["action"])
        self.assertEqual("career_profile", row["target_type"])
        self.assertEqual(self.profile.id, row["target_id"])
        self.assertEqual("user-1", row["actor_id"])
        self.assertEqual("career-confirmation-correlation", row["correlation_id"])
        self.assertIsNone(before["profile"]["current_version_id"])
        self.assertEqual(result.profile_version_id, after["profile"]["current_version_id"])
        self.assertEqual("Analytical Engines", after["experiences"][0]["organization"])
        self.assertEqual("Wrote notes on the engine.", after["achievements"][0]["action_text"])

    def _ready_run(self) -> str:
        document_count = len(self.database.fetch_all("SELECT id FROM resume_documents"))
        source = self.workspace / "incoming" / f"resume-{document_count}.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("Ada Lovelace\nBackend Engineer\n", encoding="utf-8")
        document = self.import_service.execute(
            file_ref=source,
            metadata={"mime_type": "text/plain"},
            idempotency_key=f"confirmation-import-{source.stem}",
            context=self.context,
        )
        extraction = self.extraction_service.execute(
            document_id=document.document_id,
            idempotency_key=f"confirmation-extraction-{source.stem}",
            context=self.context,
        )
        return extraction.run_id


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
                "skills": [
                    {
                        "raw_skill_name": "Unconfirmed Skill",
                        "verification_status": "needs_clarification",
                        "evidence": [{"source_locator": "line:3"}],
                    }
                ],
            },
        ],
    }


if __name__ == "__main__":
    unittest.main()
