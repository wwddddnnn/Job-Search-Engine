"""Offline integration tests for Phase 2.5 S1 manual review and publication."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from job_search_assistant.app_services import (
    CareerReviewService, ConfirmExperienceFacts, GetCareerProfileSnapshot, GetVerifiedEvidencePack,
    StartExtractionRun,
    ImportResumeDocument, NoVerifiedCareerFactsError, build_foundation,
)
from job_search_assistant.career.extraction import DraftEvidence, EXTRACTION_DRAFT_SCHEMA_VERSION
from job_search_assistant.career.types import CareerProfile, ExperienceFactConfirmation
from job_search_assistant.career.review import ReviewDraft, ReviewSource
from job_search_assistant.core import ConflictError, InfrastructureError, RequestContext
from job_search_assistant.core.errors import (
    ApplicationError, InvalidStateError, NotFoundError, ValidationError,
)
from job_search_assistant.infrastructure.files import (
    DeterministicCareerExtractionProvider, FileSystemDocumentStorage, PlainTextResumeExtractor,
)
from job_search_assistant.infrastructure.sqlite import (
    SQLiteCareerStore, SQLiteDatabase, SQLiteReviewStore,
)


MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


class CareerReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = build_foundation(
            database_path=self.root / "review.sqlite", migrations_path=MIGRATIONS,
        ).database
        self.store = SQLiteReviewStore(self.database)
        self.service = CareerReviewService(self.store)
        self.career = SQLiteCareerStore(self.database)
        self.context = RequestContext.create(
            actor_id="reviewer", source="test", correlation_id="r1",
        )
        self.counter = 0
        self.draft = self.service.create_draft(
            profile_id="profile", display_name="Example", **self.command(),
        )
        self.snapshot = GetCareerProfileSnapshot(self.career)
        self.pack = GetVerifiedEvidencePack(self.career)

    def command(self):
        self.counter += 1
        return {"context": self.context, "idempotency_key": f"key-{self.counter}"}

    def add(self, kind="experience", fields=None, parent_id=None, sources=()):
        if fields is None:
            fields = {"organization": "Company", "role": "Engineer"}
        self.draft = self.service.create_item(
            draft_id=self.draft.id, expected_version=self.draft.version,
            kind=kind, fields=fields, parent_id=parent_id, sources=sources, **self.command(),
        )
        return self.draft.items[-1].id

    def decide(self, item, decision="confirm"):
        self.draft = self.service.decide_item(
            draft_id=self.draft.id, expected_version=self.draft.version,
            item_id=item, decision=decision, **self.command(),
        )

    def edit(self, item, **changes):
        self.draft = self.service.edit_item(
            draft_id=self.draft.id, expected_version=self.draft.version,
            item_id=item, changes=changes, **self.command(),
        )

    def delete(self, item):
        self.draft = self.service.delete_item(
            draft_id=self.draft.id, expected_version=self.draft.version,
            item_id=item, **self.command(),
        )

    def restore(self, item):
        self.draft = self.service.restore_item(
            draft_id=self.draft.id, expected_version=self.draft.version,
            item_id=item, **self.command(),
        )

    def publish(self):
        result = self.service.publish(
            draft_id=self.draft.id, expected_version=self.draft.version,
            base_version_id=self.draft.base_version_id, **self.command(),
        )
        self.draft = ReviewDraft.from_dict(result["draft"])
        return result

    def import_source(self):
        source = self.root / "resume.md"
        source.write_text("# Resume\nCompany: Engineer\nBuilt a tool\nPython\n", encoding="utf-8")
        storage = FileSystemDocumentStorage(self.root / "documents")
        result = ImportResumeDocument(
            store=self.career, storage=storage, text_extractor=PlainTextResumeExtractor(storage),
        ).execute(file_ref=source, metadata={}, **self.command())
        return ReviewSource(result.document_id, DraftEvidence(
            source_excerpt="Company: Engineer", source_locator="offset:9:26",
        ))

    def test_restart_restores_content_progress_sources_and_does_not_touch_raw(self):
        source = self.import_source()
        original = (self.root / "resume.md").read_bytes()
        exp = self.add(sources=(source,))
        achievement = self.add("achievement", {"action_text": "Built a tool"}, exp, (source,))
        skill = self.add("skill", {"raw_skill_name": "Python"}, exp)
        self.decide(exp)
        self.decide(achievement, "clarify")
        self.decide(skill, "reject")
        self.edit(exp, summary="User revision")
        expected = self.draft.to_dict()
        database = SQLiteDatabase(self.database.database_path, MIGRATIONS)
        restarted = CareerReviewService(SQLiteReviewStore(database))
        self.assertEqual(expected, restarted.get_draft(draft_id=self.draft.id).to_dict())
        self.assertEqual("draft", self.draft.items[0].status)
        self.assertEqual(source, self.draft.items[0].sources[0])
        self.assertEqual(original, (self.root / "resume.md").read_bytes())
        self.assertEqual([], database.fetch_all("SELECT * FROM llm_extraction_runs"))
        audits = database.fetch_all(
            "SELECT * FROM audit_events WHERE action = 'career.review.edit'",
        )
        self.assertEqual(1, len(audits))
        self.assertEqual("reviewer", audits[0]["actor_id"])
        self.assertEqual("r1", audits[0]["correlation_id"])
        self.assertIn("User revision", audits[0]["after_json"])
        self.assertNotIn("User revision", audits[0]["before_json"])
        self.assertIn("offset:9:26", audits[0]["after_json"])

    def test_empty_state_and_unconfirmed_isolation_child_confirmation_is_independent(self):
        empty = self.snapshot.execute(profile_id="profile")
        self.assertIsNone(empty.profile_version_id)
        self.assertEqual(0, empty.version)
        exp = self.add()
        achievement = self.add("achievement", {"action_text": "Unconfirmed outcome"}, exp)
        skill = self.add("skill", {"raw_skill_name": "Unconfirmed skill"}, exp)
        with self.assertRaises(NoVerifiedCareerFactsError):
            self.pack.execute(profile_id="profile")
        with self.assertRaises(ValidationError):
            self.publish()
        self.decide(exp)
        first = self.publish()
        snapshot = self.snapshot.execute(profile_id="profile")
        self.assertEqual((), snapshot.experiences[0].achievements)
        self.assertEqual((), snapshot.experiences[0].skills)
        pack = self.pack.execute(profile_id="profile")
        self.assertTrue(all(item.source_type == "user_assertion" for item in pack.items))
        self.assertNotIn("Unconfirmed", repr(pack))
        self.assertTrue(next(i for i in self.draft.items if i.id == achievement).dirty)
        self.decide(achievement)
        self.decide(skill)
        second = self.publish()
        self.assertEqual(2, second["version"])
        pack = self.pack.execute(profile_id="profile")
        self.assertEqual({"experience", "achievement", "skill"}, {i.scope for i in pack.items})
        self.assertEqual((), self.snapshot.execute(
            profile_id="profile", profile_version_id=first["profile_version_id"],
        ).experiences[0].achievements)
        with self.assertRaises(NotFoundError):
            self.snapshot.execute(profile_id="absent")

    def test_partial_publication_preserves_unconfirmed_edits_and_history(self):
        exp = self.add()
        achievement = self.add("achievement", {"action_text": "Old outcome"}, exp)
        skill = self.add("skill", {"raw_skill_name": "Python"}, exp)
        for item in (exp, achievement, skill):
            self.decide(item)
        first = self.publish()
        original = self.career.get_profile_version_facts(
            profile_version_id=first["profile_version_id"],
        )
        self.edit(exp, role="Unconfirmed role")
        self.edit(achievement, action_text="Unconfirmed outcome")
        self.edit(skill, raw_skill_name="Rust", canonical_name="Rust")
        self.decide(skill)
        second = self.publish()
        current = self.snapshot.execute(profile_id="profile")
        self.assertEqual("Engineer", current.experiences[0].role)
        self.assertEqual("Old outcome", current.experiences[0].achievements[0].action_text)
        self.assertEqual("Rust", current.experiences[0].skills[0].name)
        self.assertEqual("Unconfirmed role", self.draft.items[0].fields["role"])
        self.assertNotIn("Unconfirmed", repr(self.pack.execute(profile_id="profile")))
        self.decide(exp)
        self.decide(achievement)
        self.publish()
        self.assertEqual(original, self.career.get_profile_version_facts(
            profile_version_id=first["profile_version_id"],
        ))
        self.assertEqual("Engineer", self.snapshot.execute(
            profile_id="profile", profile_version_id=second["profile_version_id"],
        ).experiences[0].role)

    def test_unconfirmed_delete_retains_old_fact_confirmed_delete_summary_and_undo(self):
        exp = self.add()
        achievement = self.add("achievement", {"action_text": "Outcome"}, exp)
        self.decide(exp)
        self.decide(achievement)
        first = self.publish()
        self.delete(achievement)
        self.edit(exp, summary="Confirmed revision")
        self.decide(exp)
        self.publish()
        snapshot = self.snapshot.execute(profile_id="profile")
        self.assertEqual(1, len(snapshot.experiences[0].achievements))
        self.restore(achievement)
        self.assertFalse(next(i for i in self.draft.items if i.id == achievement).deleted)
        self.delete(achievement)
        self.decide(achievement, "confirm_delete")
        result = self.publish()
        self.assertEqual(achievement, result["summary"]["deleted"][0]["item_id"])
        snapshot = self.snapshot.execute(profile_id="profile")
        self.assertEqual((), snapshot.experiences[0].achievements)
        self.assertEqual(1, len(self.snapshot.execute(
            profile_id="profile", profile_version_id=first["profile_version_id"],
        ).experiences[0].achievements))
        self.restore(achievement)
        self.decide(achievement)
        self.publish()
        snapshot = self.snapshot.execute(profile_id="profile")
        self.assertEqual(1, len(snapshot.experiences[0].achievements))

    def test_experience_delete_lists_descendants_and_empty_published_version_is_valid(self):
        exp = self.add()
        child = self.add("achievement", {"action_text": "Outcome"}, exp)
        skill = self.add("skill", {"raw_skill_name": "Python"}, exp)
        for item in (exp, child, skill):
            self.decide(item)
        first = self.publish()
        self.delete(exp)
        self.decide(exp, "confirm_delete")
        result = self.publish()
        self.assertEqual({exp, child, skill},
                         {i["item_id"] for i in result["summary"]["deleted"]})
        saved = self.service.get_draft(draft_id=self.draft.id)
        for item_id in (child, skill):
            item = next(i for i in saved.items if i.id == item_id)
            self.assertIsNone(item.published_id)
            self.assertEqual(exp, item.parent_id)
            self.assertFalse(item.deleted)
        self.assertEqual("Outcome", next(i for i in saved.items if i.id == child)
                         .fields["action_text"])
        self.assertEqual("Python", next(i for i in saved.items if i.id == skill)
                         .fields["raw_skill_name"])
        facts = self.career.get_profile_version_facts(
            profile_version_id=result["profile_version_id"],
        )
        self.assertEqual((), facts.achievements)
        self.assertEqual((), facts.experience_skills)
        snapshot = self.snapshot.execute(profile_id="profile")
        self.assertEqual((), snapshot.experiences)
        self.assertEqual(2, snapshot.version)
        self.assertIsNotNone(snapshot.profile_version_id)
        self.assertEqual(1, len(self.snapshot.execute(
            profile_id="profile", profile_version_id=first["profile_version_id"],
        ).experiences))
        with self.assertRaises(NoVerifiedCareerFactsError):
            self.pack.execute(profile_id="profile")

    def test_save_order_old_request_blocked_and_idempotent_replay_is_original_response(self):
        exp = self.add()
        old_version = self.draft.version
        command = self.command()
        args = dict(draft_id=self.draft.id, expected_version=old_version, item_id=exp,
                    changes={"role": "First edit"}, **command)
        first = self.service.edit_item(**args)
        self.draft = first
        self.edit(exp, role="Second edit")
        replay = self.service.edit_item(**args)
        self.assertEqual(first, replay)
        self.assertEqual("Second edit", self.service.get_draft(
            draft_id=self.draft.id,
        ).items[0].fields["role"])
        with self.assertRaises(ConflictError):
            self.service.edit_item(**{**args, "idempotency_key": "late-request"})
        with self.assertRaises(ConflictError):
            self.service.edit_item(**{**args, "changes": {"role": "Different payload"}})
        self.assertEqual(2, len(self.database.fetch_all(
            "SELECT * FROM audit_events WHERE action = 'career.review.edit'",
        )))

    def test_failed_save_rolls_back_business_audit_and_key_then_same_key_can_retry(self):
        exp = self.add()
        before = self.draft
        args = dict(draft_id=before.id, expected_version=before.version, item_id=exp,
                    changes={"role": "Input retained by caller"}, **self.command())
        audits = len(self.database.fetch_all("SELECT * FROM audit_events"))
        with patch.object(self.store._idempotency, "complete_in_transaction",
                          side_effect=InfrastructureError("injected failure")):
            with self.assertRaises(InfrastructureError):
                self.service.edit_item(**args)
        self.assertEqual(before, self.service.get_draft(draft_id=before.id))
        self.assertEqual(audits, len(self.database.fetch_all("SELECT * FROM audit_events")))
        self.assertEqual([], self.database.fetch_all(
            "SELECT * FROM idempotency_records WHERE idempotency_key = ?",
            (args["idempotency_key"],),
        ))
        saved = self.service.edit_item(**args)
        self.assertEqual("Input retained by caller", saved.items[0].fields["role"])
        self.assertEqual(saved, self.service.edit_item(**args))

    def test_publish_replay_stale_saved_view_and_stale_base_are_distinct_conflicts(self):
        exp = self.add()
        self.decide(exp)
        args = dict(draft_id=self.draft.id, expected_version=self.draft.version,
                    base_version_id=None, **self.command())
        result = self.service.publish(**args)
        self.draft = ReviewDraft.from_dict(result["draft"])
        self.assertEqual(result, self.service.publish(**args))
        self.assertEqual(1, len(self.database.fetch_all("SELECT * FROM profile_versions")))
        with self.assertRaises(ConflictError):
            self.service.publish(**{**args, **self.command()})
        self.edit(exp, role="New role")
        self.decide(exp)
        with self.assertRaises(ConflictError):
            self.service.publish(draft_id=self.draft.id, expected_version=self.draft.version,
                                 base_version_id=None, **self.command())
        self.publish()

    def test_publish_failure_is_atomic_and_retryable(self):
        exp = self.add()
        self.decide(exp)
        before = self.draft
        args = dict(draft_id=before.id, expected_version=before.version,
                    base_version_id=None, **self.command())
        with patch.object(self.store._audit, "append_in_transaction",
                          side_effect=InfrastructureError("audit unavailable")):
            with self.assertRaises(InfrastructureError):
                self.service.publish(**args)
        self.assertEqual([], self.database.fetch_all("SELECT * FROM profile_versions"))
        self.assertEqual([], self.database.fetch_all("SELECT * FROM experiences"))
        self.assertIsNone(self.career.get_career_profile(profile_id="profile").current_version_id)
        self.assertEqual(before, self.service.get_draft(draft_id=before.id))
        self.service.publish(**args)
        self.assertEqual(1, len(self.database.fetch_all("SELECT * FROM profile_versions")))

    def test_source_edit_requires_reconfirmation_and_skill_assertion_is_not_document_fact(self):
        source = self.import_source()
        exp = self.add(sources=(source,))
        skill = self.add("skill", {"raw_skill_name": "Rust"}, exp)
        self.decide(exp)
        self.edit(exp, role="Senior engineer")
        with self.assertRaises(ValidationError):
            self.publish()
        self.decide(exp)
        self.decide(skill)
        self.publish()
        pack = self.pack.execute(profile_id="profile")
        skill_evidence = [i for i in pack.items if i.scope == "skill"]
        self.assertEqual(1, len(skill_evidence))
        self.assertEqual("user_assertion", skill_evidence[0].source_type)
        self.assertIsNone(skill_evidence[0].source_document_id)
        experience_evidence = [i for i in pack.items if i.scope == "experience"]
        self.assertEqual(source.document_id, experience_evidence[0].source_document_id)

    def test_numeric_achievement_publishes_only_after_confirmation(self):
        exp = self.add()
        achievement = self.add("achievement", {
            "action_text": "Reduced latency", "metric_value": 25, "metric_unit": "%",
        }, exp)
        self.decide(achievement)
        with self.assertRaises(ValidationError):
            self.publish()  # Confirming a child does not confirm its parent.
        self.decide(exp)
        self.publish()
        facts = self.career.get_profile_version_facts(profile_version_id=self.draft.base_version_id)
        self.assertIsNotNone(facts.achievements[0].metric_user_confirmed_at)
        self.assertEqual(25, facts.achievements[0].metric_value)

    def test_invalid_transitions_missing_sources_and_parent_are_rejected(self):
        exp = self.add()
        with self.assertRaises(InvalidStateError):
            self.decide(exp, "confirm_delete")
        self.delete(exp)
        with self.assertRaises(InvalidStateError):
            self.decide(exp)
        with self.assertRaises(InvalidStateError):
            self.edit(exp, role="Oops")
        with self.assertRaises(ValidationError):
            self.add("skill", {"raw_skill_name": "Python"}, exp)
        self.restore(exp)
        with self.assertRaises(NotFoundError):
            self.add(sources=(ReviewSource("absent", DraftEvidence(source_locator="offset:0:1")),))
        with self.assertRaises(ValidationError):
            self.add(sources=(ReviewSource("doc", DraftEvidence(source_excerpt="no locator")),))
        with self.assertRaises(ValidationError):
            self.edit(exp, verification_status="verified")
        with self.assertRaises(ValidationError):
            self.service.edit_item(
                draft_id=self.draft.id, expected_version=self.draft.version, item_id=exp,
                changes={"role": "changed"}, idempotency_key="",
                context=self.context,
            )
        with self.assertRaises(ValidationError):
            self.service.edit_item(
                draft_id=self.draft.id, expected_version=self.draft.version, item_id=exp,
                changes={"role": "changed"}, idempotency_key="invalid-context",
                context=replace(self.context, correlation_id=""),
            )

    def preview(self):
        return self.service.preview_publication(
            draft_id=self.draft.id, expected_version=self.draft.version,
            base_version_id=self.draft.base_version_id,
        )

    def test_empty_preview_and_repeated_confirm_do_not_create_publication_changes(self):
        empty = {"added": [], "modified": [], "deleted": []}
        self.assertEqual(empty, self.preview()["summary"])
        exp = self.add()
        child = self.add("achievement", {"action_text": "Outcome"}, exp)
        skill = self.add("skill", {"raw_skill_name": "Python"}, exp)
        self.assertEqual(empty, self.preview()["summary"])
        for item in (exp, child, skill):
            self.decide(item)
        confirmed = self.draft.items
        for item in (exp, child, skill):
            self.decide(item)
        self.assertEqual(confirmed, self.draft.items)
        self.assertEqual(3, len(self.preview()["summary"]["added"]))
        self.publish()
        published = self.draft.items
        for item in (exp, child, skill):
            self.decide(item)
        self.assertEqual(published, self.draft.items)
        self.assertTrue(all(not item.dirty for item in self.draft.items))
        self.assertEqual(empty, self.preview()["summary"])
        with self.assertRaises(ValidationError):
            self.publish()
        self.assertEqual(1, len(self.database.fetch_all("SELECT * FROM profile_versions")))
        self.edit(exp, role="Changed role")
        self.assertEqual("draft", self.draft.items[0].status)
        self.assertEqual(empty, self.preview()["summary"])
        self.decide(exp)
        self.assertEqual([exp], [i["item_id"] for i in self.preview()["summary"]["modified"]])
        self.publish()
        self.assertEqual("Changed role", self.snapshot.execute(
            profile_id="profile",
        ).experiences[0].role)

    def test_preview_rejects_base_mismatch_even_with_empty_changes(self):
        exp = self.add()
        self.decide(exp)
        first = self.publish()
        self.edit(exp, role="Second role")
        self.decide(exp)
        self.publish()
        for base_id in (None, "unknown", first["profile_version_id"]):
            with self.subTest(base_id=base_id), self.assertRaises(ConflictError):
                self.service.preview_publication(
                    draft_id=self.draft.id, expected_version=self.draft.version,
                    base_version_id=base_id,
                )
        # Caller agrees with the draft, but another publication moved the profile pointer.
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE career_profiles SET current_version_id = ? WHERE id = 'profile'",
                (first["profile_version_id"],),
            )
        with self.assertRaises(ConflictError):
            self.preview()
        # Caller agrees with the current profile, but not the draft base.
        with self.assertRaises(ConflictError):
            self.service.preview_publication(
                draft_id=self.draft.id, expected_version=self.draft.version,
                base_version_id=first["profile_version_id"],
            )

    def test_missing_published_references_are_application_errors(self):
        exp = self.add()
        child = self.add("achievement", {"action_text": "Outcome"}, exp)
        skill = self.add("skill", {"raw_skill_name": "Python"}, exp)
        for item in (exp, child, skill):
            self.decide(item)
        self.publish()
        facts = self.career.get_profile_version_facts(profile_version_id=self.draft.base_version_id)
        for field in ("experiences", "achievements", "experience_skills", "skills"):
            with self.subTest(field=field), patch.object(
                self.store._career, "get_profile_version_facts",
                return_value=replace(facts, **{field: ()}),
            ):
                for operation in (self.preview, self.publish):
                    with self.assertRaises(ApplicationError) as caught:
                        operation()
                    self.assertIsInstance(caught.exception, (ConflictError, InfrastructureError))
                    self.assertIn(caught.exception.code, ("conflict", "infrastructure_error"))
                self.assertEqual(self.context.correlation_id, caught.exception.correlation_id)
        self.assertEqual(1, len(self.database.fetch_all("SELECT * FROM profile_versions")))

    def test_missing_skill_evidence_association_is_a_coded_conflict(self):
        from job_search_assistant.career.review_publication import item_evidence

        exp = self.add()
        skill = self.add("skill", {"raw_skill_name": "Python"}, exp)
        self.decide(exp)
        self.decide(skill)
        self.publish()
        facts = self.career.get_profile_version_facts(profile_version_id=self.draft.base_version_id)
        item = next(i for i in self.draft.items if i.id == skill)
        with self.assertRaises(ApplicationError) as caught:
            item_evidence(item, replace(facts, experience_skills=(), evidence=()))
        self.assertIsInstance(caught.exception, ConflictError)
        self.assertEqual("conflict", caught.exception.code)

    def test_preview_is_read_only_and_bound_to_both_saved_versions(self):
        exp = self.add()
        self.decide(exp)
        before_audits = len(self.database.fetch_all("SELECT * FROM audit_events"))
        before_keys = len(self.database.fetch_all("SELECT * FROM idempotency_records"))
        preview = self.service.preview_publication(
            draft_id=self.draft.id, expected_version=self.draft.version, base_version_id=None,
        )
        self.assertEqual(before_audits, len(self.database.fetch_all("SELECT * FROM audit_events")))
        self.assertEqual(
            before_keys, len(self.database.fetch_all("SELECT * FROM idempotency_records")),
        )
        self.assertEqual(self.draft, self.service.get_draft(draft_id=self.draft.id))
        result = self.publish()
        self.assertEqual(preview["summary"], result["summary"])
        self.assertEqual([], preview["summary"]["deleted"])

    def test_two_stores_concurrent_saves_have_one_winner_and_same_key_replays(self):
        exp = self.add()
        before = self.draft
        barrier = Barrier(2)

        def write(index, key):
            service = CareerReviewService(SQLiteReviewStore(self.database))
            barrier.wait(timeout=5)
            try:
                return service.edit_item(
                    draft_id=before.id, expected_version=before.version, item_id=exp,
                    changes={"role": f"Role {index}"}, context=self.context, idempotency_key=key,
                )
            except ConflictError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, i, f"parallel-{i}") for i in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(1, sum(isinstance(result, ConflictError) for result in results))
        winner = next(result for result in results if isinstance(result, ReviewDraft))
        self.assertEqual(winner, self.service.get_draft(draft_id=before.id))
        before = winner
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, 3, "parallel-same-key") for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(results[0], results[1])
        self.assertEqual(before.version + 1, results[0].version)

    def test_existing_phase2_profile_is_seeded_and_extraction_output_is_immutable(self):
        source = self.import_source()
        storage = FileSystemDocumentStorage(self.root / "documents")
        original_payload = {
            "schema_version": EXTRACTION_DRAFT_SCHEMA_VERSION,
            "experiences": [{
                "organization": "Legacy company", "role": "Legacy role",
                "evidence": [{"source_locator": "offset:9:26", "source_excerpt": "Company"}],
                "achievements": [{"action_text": "Legacy outcome", "evidence": [
                    {"source_locator": "offset:27:39", "source_excerpt": "Built a tool"},
                ]}],
                "skills": [{"raw_skill_name": "Python", "evidence": [
                    {"source_locator": "offset:40:46", "source_excerpt": "Python"},
                ]}],
            }],
        }
        run = StartExtractionRun(
            store=self.career, storage=storage,
            extraction_provider=DeterministicCareerExtractionProvider(payload=original_payload),
        ).execute(document_id=source.document_id, **self.command())
        now = datetime.now(UTC)
        self.career.create_career_profile(profile=CareerProfile("legacy", "u", "Legacy", now, now))
        result = ConfirmExperienceFacts(store=self.career, storage=storage).execute(
            profile_id="legacy", extraction_run_id=run.run_id,
            confirmations=(ExperienceFactConfirmation(
                experience_index=0, achievement_indexes=(0,), skill_indexes=(0,),
            ),), **self.command(),
        )
        old_run = self.career.get_extraction_run(run_id=run.run_id)
        payload = storage.load_extraction_draft(output_ref=old_run.output_ref)
        old_facts = self.career.get_profile_version_facts(
            profile_version_id=result.profile_version_id,
        )
        self.draft = self.service.create_draft(profile_id="legacy", **self.command())
        self.assertEqual(3, len(self.draft.items))
        self.assertTrue(all(not item.dirty for item in self.draft.items))
        exp = self.draft.items[0].id
        self.edit(exp, role="Edited legacy role")
        other = self.add(fields={"organization": "New company", "role": "New role"})
        self.decide(other)
        self.publish()
        snapshot = self.snapshot.execute(profile_id="legacy")
        self.assertIn("Legacy role", [e.role for e in snapshot.experiences])
        self.assertEqual(old_facts, self.career.get_profile_version_facts(
            profile_version_id=result.profile_version_id,
        ))
        self.assertEqual(payload, storage.load_extraction_draft(output_ref=old_run.output_ref))
        self.assertEqual(old_run, self.career.get_extraction_run(run_id=run.run_id))
        self.assertIn("offset:9:26", repr(self.draft.to_dict()))

    def test_skill_delete_reject_clarify_and_restore_preserve_old_versions(self):
        exp = self.add()
        skill = self.add("skill", {"raw_skill_name": "Python"}, exp)
        self.decide(exp)
        self.decide(skill)
        first = self.publish()
        self.edit(skill, raw_skill_name="Unverified skill", canonical_name="Unverified skill")
        for decision in ("clarify", "reject"):
            self.decide(skill, decision)
            self.edit(exp, summary=decision)
            self.decide(exp)
            self.publish()
            self.assertEqual("Python", self.snapshot.execute(
                profile_id="profile",
            ).experiences[0].skills[0].name)
        self.delete(skill)
        self.decide(skill, "confirm_delete")
        deleted = self.publish()
        self.assertEqual([skill], [i["item_id"] for i in deleted["summary"]["deleted"]])
        self.assertEqual((), self.snapshot.execute(profile_id="profile").experiences[0].skills)
        self.restore(skill)
        self.assertEqual("draft", next(i for i in self.draft.items if i.id == skill).status)
        self.decide(skill)
        self.publish()
        self.assertEqual("Unverified skill", self.snapshot.execute(
            profile_id="profile",
        ).experiences[0].skills[0].name)
        self.assertEqual("Python", self.snapshot.execute(
            profile_id="profile", profile_version_id=first["profile_version_id"],
        ).experiences[0].skills[0].name)

    def test_0005_upgrade_is_additive_and_checksum_protected(self):
        migration_dir = self.root / "migrations"
        migration_dir.mkdir()
        for path in sorted(MIGRATIONS.glob("000[1-4]*.sql")):
            shutil.copy(path, migration_dir / path.name)
        old = SQLiteDatabase(self.root / "upgrade.sqlite", migration_dir)
        old.migrate()
        with old.transaction() as connection:
            connection.execute(
                "INSERT INTO career_profiles (id, owner_id, display_name, created_at, updated_at) "
                "VALUES ('p', 'u', 'Name', '2026-01-01', '2026-01-01')",
            )
        source = next(MIGRATIONS.glob("0005*.sql"))
        target = migration_dir / source.name
        shutil.copy(source, target)
        self.assertEqual(old.migrate(), old.migrate())
        self.assertEqual("Name", old.fetch_all("SELECT display_name FROM career_profiles")[0][0])
        target.write_text(target.read_text() + "\n-- modified\n")
        with self.assertRaises(InfrastructureError):
            old.migrate()


if __name__ == "__main__":
    unittest.main()
