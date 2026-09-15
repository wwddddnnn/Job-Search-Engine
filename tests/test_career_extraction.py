"""Integration tests for the S3 Career extraction-run workflow."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from job_search_assistant.app_services import (
    ImportResumeDocument,
    StartExtractionRun,
    build_foundation,
)
from job_search_assistant.career import (
    EXTRACTION_DRAFT_SCHEMA_VERSION,
    ExtractionRunStatus,
)
from job_search_assistant.core import InfrastructureError, RequestContext, ValidationError
from job_search_assistant.infrastructure.files import (
    DeterministicCareerExtractionProvider,
    FileSystemDocumentStorage,
    PlainTextResumeExtractor,
)
from job_search_assistant.infrastructure.sqlite import SQLiteCareerStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_PATH = PROJECT_ROOT / "migrations"


class CareerExtractionTestCase(unittest.TestCase):
    """Exercise run creation, immutable output, and failed-run diagnostics."""

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
            source="career-extraction-test",
            correlation_id="career-extraction-correlation",
        )
        self.import_service = ImportResumeDocument(
            store=self.store,
            storage=self.storage,
            text_extractor=PlainTextResumeExtractor(self.storage),
        )
        self.provider = DeterministicCareerExtractionProvider(payload=_draft_payload())
        self.service = StartExtractionRun(
            store=self.store,
            storage=self.storage,
            extraction_provider=self.provider,
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_output_is_a_schema_valid_draft_and_never_verified(self) -> None:
        document_id = self._import_resume()

        result = self.service.execute(
            document_id=document_id,
            idempotency_key="extraction-draft-only-1",
            context=self.context,
        )

        self.assertEqual(ExtractionRunStatus.DRAFT_READY, result.status)
        run = self.store.get_extraction_run(run_id=result.run_id)
        self.assertEqual(ExtractionRunStatus.DRAFT_READY, run.status)
        self.assertIsNotNone(run.output_ref)
        draft = self.storage.load_extraction_draft(output_ref=run.output_ref or "")
        self.assertEqual(EXTRACTION_DRAFT_SCHEMA_VERSION, draft["schema_version"])
        self._assert_only_draft_statuses(draft)

    def test_rerun_appends_a_new_run_without_overwriting_the_old_output(self) -> None:
        document_id = self._import_resume()
        first_result = self.service.execute(
            document_id=document_id,
            idempotency_key="extraction-rerun-1",
            context=self.context,
        )
        first_before = self.store.get_extraction_run(run_id=first_result.run_id)
        first_payload = self.storage.load_extraction_draft(output_ref=first_before.output_ref or "")

        second_result = self.service.execute(
            document_id=document_id,
            idempotency_key="extraction-rerun-2",
            context=self.context,
        )

        self.assertNotEqual(first_result.run_id, second_result.run_id)
        self.assertEqual(
            2,
            len(
                self.database.fetch_all(
                    "SELECT id FROM llm_extraction_runs WHERE document_id = ?",
                    (document_id,),
                )
            ),
        )
        first_after = self.store.get_extraction_run(run_id=first_result.run_id)
        self.assertEqual(first_before, first_after)
        self.assertEqual(
            first_payload,
            self.storage.load_extraction_draft(output_ref=first_after.output_ref or ""),
        )

    def test_completed_idempotency_replay_returns_the_existing_run_without_text_io(self) -> None:
        document_id = self._import_resume()
        first = self.service.execute(
            document_id=document_id,
            idempotency_key="extraction-replay-1",
            context=self.context,
        )

        with patch.object(
            self.storage,
            "load_text",
            side_effect=AssertionError("replay read text"),
        ):
            replay = self.service.execute(
                document_id=document_id,
                idempotency_key="extraction-replay-1",
                context=self.context,
            )

        self.assertEqual(first, replay)

    def test_in_progress_replay_reuses_the_existing_run_without_creating_another_row(self) -> None:
        document_id = self._import_resume()
        document = self.store.get_resume_document(document_id=document_id)
        text = self.store.list_resume_texts(document_id=document_id)[0]
        input_hash = sha256(
            self.storage.load_text(text_ref=text.text_ref).encode("utf-8")
        ).hexdigest()
        run = document.start_extraction_run(
            run_id="in-progress-extraction-run",
            input_hash=input_hash,
            model=self.service.model,
            prompt_version=self.service.prompt_version,
            schema_version=self.service.schema_version,
        )
        self.store.reserve_extraction_run(
            run=run,
            idempotency_key="extraction-in-progress-1",
            request={
                "document_id": document_id,
                "model": self.service.model,
                "prompt_version": self.service.prompt_version,
                "schema_version": self.service.schema_version,
            },
            context=self.context,
        )

        result = self.service.execute(
            document_id=document_id,
            idempotency_key="extraction-in-progress-1",
            context=self.context,
        )

        self.assertEqual(run.id, result.run_id)
        rows = self.database.fetch_all("SELECT id FROM llm_extraction_runs")
        self.assertEqual([run.id], [str(row["id"]) for row in rows])

    def test_unconfigured_provider_is_explicit_and_persists_a_failed_run_without_drafts(
        self,
    ) -> None:
        document_id = self._import_resume()
        service = StartExtractionRun(
            store=self.store,
            storage=self.storage,
            extraction_provider=DeterministicCareerExtractionProvider(),
        )

        with self.assertRaises(InfrastructureError) as raised:
            service.execute(
                document_id=document_id,
                idempotency_key="extraction-unconfigured-1",
                context=self.context,
            )

        self.assertEqual("infrastructure_error", raised.exception.code)
        self.assertIn("not configured", raised.exception.message.lower())
        row = self.database.fetch_all(
            "SELECT id FROM llm_extraction_runs WHERE document_id = ?",
            (document_id,),
        )[0]
        run = self.store.get_extraction_run(run_id=str(row["id"]))
        self.assertEqual(ExtractionRunStatus.DRAFT_FAILED, run.status)
        self.assertIsNone(run.output_ref)
        self.assertIsNotNone(run.error_summary)
        self.assertEqual([], self.database.fetch_all("SELECT id FROM experiences"))

    def test_schema_failure_is_persisted_without_a_draft_output(self) -> None:
        document_id = self._import_resume()
        invalid_provider = DeterministicCareerExtractionProvider(
            payload={
                "schema_version": EXTRACTION_DRAFT_SCHEMA_VERSION,
                "experiences": [
                    {
                        "organization": "Analytical Engines",
                        "role": "Programmer",
                        "verification_status": "verified",
                        "evidence": [{"source_locator": "line:1"}],
                    }
                ],
            }
        )
        service = StartExtractionRun(
            store=self.store,
            storage=self.storage,
            extraction_provider=invalid_provider,
        )

        with self.assertRaises(ValidationError):
            service.execute(
                document_id=document_id,
                idempotency_key="extraction-schema-failed-1",
                context=self.context,
            )

        row = self.database.fetch_all(
            "SELECT id FROM llm_extraction_runs WHERE document_id = ?",
            (document_id,),
        )[0]
        run = self.store.get_extraction_run(run_id=str(row["id"]))
        self.assertEqual(ExtractionRunStatus.DRAFT_FAILED, run.status)
        self.assertIsNone(run.output_ref)
        self.assertIsNotNone(run.error_summary)
        self.assertEqual([], self.database.fetch_all("SELECT id FROM experiences"))

    def test_candidate_without_a_source_locator_is_rejected_and_persisted_as_failed(self) -> None:
        document_id = self._import_resume()
        locatorless_provider = DeterministicCareerExtractionProvider(
            payload={
                "schema_version": EXTRACTION_DRAFT_SCHEMA_VERSION,
                "experiences": [
                    {
                        "organization": "Analytical Engines",
                        "role": "Programmer",
                        "verification_status": "draft",
                        "evidence": [{"source_excerpt": "Wrote notes on the engine."}],
                    }
                ],
            }
        )
        service = StartExtractionRun(
            store=self.store,
            storage=self.storage,
            extraction_provider=locatorless_provider,
        )

        with self.assertRaises(ValidationError):
            service.execute(
                document_id=document_id,
                idempotency_key="extraction-locator-failed-1",
                context=self.context,
            )

        row = self.database.fetch_all(
            "SELECT id FROM llm_extraction_runs WHERE document_id = ?",
            (document_id,),
        )[0]
        run = self.store.get_extraction_run(run_id=str(row["id"]))
        self.assertEqual(ExtractionRunStatus.DRAFT_FAILED, run.status)
        self.assertIsNone(run.output_ref)

    def _import_resume(self) -> str:
        source = self.workspace / "incoming" / "resume.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("Ada Lovelace\nBackend Engineer\n", encoding="utf-8")
        result = self.import_service.execute(
            file_ref=source,
            metadata={"mime_type": "text/plain"},
            idempotency_key=(
                "import-for-extraction-"
                f"{len(self.database.fetch_all('SELECT id FROM resume_documents'))}"
            ),
            context=self.context,
        )
        return result.document_id

    def _assert_only_draft_statuses(self, value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "verification_status":
                    self.assertIn(child, {"draft", "needs_clarification"})
                self._assert_only_draft_statuses(child)
        elif isinstance(value, list):
            for child in value:
                self._assert_only_draft_statuses(child)


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
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
