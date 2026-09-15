"""Integration tests for the S2 controlled Career resume-import slice."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from job_search_assistant.app_services import ImportResumeDocument, build_foundation
from job_search_assistant.career import DocumentStatus, ResumeDocument, Skill
from job_search_assistant.core import ConflictError, InfrastructureError, RequestContext
from job_search_assistant.infrastructure.files import (
    FileSystemDocumentStorage,
    PlainTextResumeExtractor,
)
from job_search_assistant.infrastructure.sqlite import SQLiteCareerStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_PATH = PROJECT_ROOT / "migrations"


class CareerImportTestCase(unittest.TestCase):
    """Run imports through real SQLite and controlled filesystem adapters."""

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
        self.service = ImportResumeDocument(
            store=self.store,
            storage=self.storage,
            text_extractor=PlainTextResumeExtractor(self.storage),
        )
        self.context = RequestContext.create(
            actor_id="user-1",
            source="career-import-test",
            correlation_id="career-import-correlation",
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_success_retains_original_file_and_extracted_text(self) -> None:
        source = self._write_source("resume.txt", "Ada Lovelace\nBackend Engineer\n")

        result = self.service.execute(
            file_ref=source,
            metadata={"mime_type": "text/plain", "original_name": "resume.txt"},
            idempotency_key="import-success-1",
            context=self.context,
        )

        self.assertEqual(DocumentStatus.TEXT_EXTRACTED, result.status)
        document = self.store.get_resume_document(document_id=result.document_id)
        self.assertEqual(DocumentStatus.TEXT_EXTRACTED, document.status)
        self.assertEqual(
            b"Ada Lovelace\nBackend Engineer\n",
            self.storage.load_document(file_ref=document.file_ref),
        )

        texts = self.store.list_resume_texts(document_id=document.id)
        self.assertEqual(1, len(texts))
        self.assertEqual(
            "Ada Lovelace\nBackend Engineer\n",
            self.storage.load_text(text_ref=texts[0].text_ref),
        )
        self.assertEqual(texts[0].text_ref, texts[0].locator_map_ref)
        self.assertEqual(
            "plain_text",
            self.storage.load_locator_map(text_ref=texts[0].text_ref)["format"],
        )

    def test_idempotency_replay_returns_prior_result_and_rejects_changed_payload(self) -> None:
        source = self._write_source("resume.txt", "Ada Lovelace\n")
        request = {
            "file_ref": source,
            "metadata": {"mime_type": "text/plain"},
            "idempotency_key": "import-replay-1",
            "context": self.context,
        }

        first = self.service.execute(**request)
        replay = self.service.execute(**request)

        self.assertEqual(first, replay)
        self.assertEqual(1, len(self.store.list_resume_texts(document_id=first.document_id)))
        with self.assertRaises(ConflictError) as raised:
            self.service.execute(
                file_ref=source,
                metadata={"mime_type": "text/plain", "original_name": "changed-name.txt"},
                idempotency_key="import-replay-1",
                context=self.context,
            )
        self.assertEqual("conflict", raised.exception.code)

    def test_completed_replay_does_not_require_the_original_local_source_file(self) -> None:
        source = self._write_source("resume.txt", "Ada Lovelace\n")
        request = {
            "file_ref": source,
            "metadata": {"mime_type": "text/plain"},
            "idempotency_key": "import-replay-without-source-1",
            "context": self.context,
        }

        first = self.service.execute(**request)
        source.unlink()

        replay = self.service.execute(**request)

        self.assertEqual(first, replay)

    def test_in_progress_replay_reuses_its_existing_document(self) -> None:
        source = self._write_source("resume.txt", "Ada Lovelace\n")
        content = source.read_bytes()
        content_hash = sha256(content).hexdigest()
        document = ResumeDocument(
            id="in-progress-resume-document",
            file_ref=self.storage.store_document(content=content, content_hash=content_hash),
            mime_type="text/plain",
            content_hash=content_hash,
            uploaded_at=datetime.now(UTC),
            metadata={"mime_type": "text/plain"},
        )
        self.store.reserve_resume_import(
            document=document,
            idempotency_key="import-in-progress-1",
            request={
                "file_ref": str(source),
                "metadata": {"mime_type": "text/plain"},
            },
            context=self.context,
        )
        source.unlink()

        result = self.service.execute(
            file_ref=source,
            metadata={"mime_type": "text/plain"},
            idempotency_key="import-in-progress-1",
            context=self.context,
        )

        self.assertEqual(document.id, result.document_id)
        rows = self.database.fetch_all("SELECT id FROM resume_documents")
        self.assertEqual([document.id], [str(row["id"]) for row in rows])

    def test_create_skill_maps_missing_taxonomy_reference_to_empty_string(self) -> None:
        skill = Skill(
            id="skill-no-taxonomy",
            canonical_name="python",
            taxonomy_ref=None,
            created_at=datetime.now(UTC),
        )

        persisted = self.store.create_skill(skill=skill)

        self.assertEqual("", persisted.taxonomy_ref)
        row = self.database.fetch_all(
            "SELECT taxonomy_ref FROM skills WHERE id = ?",
            (skill.id,),
        )[0]
        self.assertEqual("", row["taxonomy_ref"])

    def test_import_audit_events_include_action_target_actor_and_correlation_id(self) -> None:
        source = self._write_source("resume.txt", "Ada Lovelace\n")
        result = self.service.execute(
            file_ref=source,
            metadata={"mime_type": "text/plain"},
            idempotency_key="import-audit-1",
            context=self.context,
        )

        rows = self.database.fetch_all(
            """
            SELECT action, target_type, target_id, actor_id, correlation_id
            FROM audit_events
            WHERE target_id = ?
            ORDER BY occurred_at ASC, id ASC
            """,
            (result.document_id,),
        )

        self.assertEqual(
            ["career.resume_document.imported", "career.resume_document.text_extracted"],
            [str(row["action"]) for row in rows],
        )
        for row in rows:
            self.assertEqual("resume_document", row["target_type"])
            self.assertEqual(result.document_id, row["target_id"])
            self.assertEqual("user-1", row["actor_id"])
            self.assertEqual("career-import-correlation", row["correlation_id"])

    def test_parser_failure_records_extraction_failed_without_removing_original_file(self) -> None:
        source = self._write_source("resume.pdf", "%PDF-not-a-real-pdf")

        result = self.service.execute(
            file_ref=source,
            metadata={"mime_type": "application/pdf"},
            idempotency_key="import-parser-failed-1",
            context=self.context,
        )

        self.assertEqual(DocumentStatus.EXTRACTION_FAILED, result.status)
        document = self.store.get_resume_document(document_id=result.document_id)
        self.assertEqual(DocumentStatus.EXTRACTION_FAILED, document.status)
        self.assertIn("resume_text_extraction_failed", document.extraction_error or "")
        self.assertEqual(
            b"%PDF-not-a-real-pdf",
            self.storage.load_document(file_ref=document.file_ref),
        )
        self.assertEqual([], list(self.store.list_resume_texts(document_id=document.id)))

    def test_infrastructure_failure_during_text_storage_bubbles_without_terminalizing_parser_state(
        self,
    ) -> None:
        source = self._write_source("resume.txt", "Ada Lovelace\n")

        with patch.object(
            self.storage,
            "store_text",
            side_effect=InfrastructureError("Simulated storage failure."),
        ):
            with self.assertRaises(InfrastructureError):
                self.service.execute(
                    file_ref=source,
                    metadata={"mime_type": "text/plain"},
                    idempotency_key="import-infrastructure-failure-1",
                    context=self.context,
                )

        row = self.database.fetch_all("SELECT id FROM resume_documents")[0]
        document = self.store.get_resume_document(document_id=str(row["id"]))
        self.assertEqual(DocumentStatus.IMPORTED, document.status)

    def test_replacing_with_different_content_creates_a_new_document_without_history_overwrite(
        self,
    ) -> None:
        first_source = self._write_source("resume-v1.txt", "Ada Lovelace\nEngineer\n")
        second_source = self._write_source("resume-v2.txt", "Ada Lovelace\nPrincipal Engineer\n")

        first = self.service.execute(
            file_ref=first_source,
            metadata={"mime_type": "text/plain"},
            idempotency_key="import-replace-v1",
            context=self.context,
        )
        second = self.service.execute(
            file_ref=second_source,
            metadata={"mime_type": "text/plain"},
            idempotency_key="import-replace-v2",
            context=self.context,
        )

        self.assertNotEqual(first.document_id, second.document_id)
        documents = self.database.fetch_all(
            "SELECT id, content_hash FROM resume_documents ORDER BY uploaded_at ASC, id ASC"
        )
        self.assertEqual(2, len(documents))
        self.assertNotEqual(documents[0]["content_hash"], documents[1]["content_hash"])
        first_document = self.store.get_resume_document(document_id=first.document_id)
        second_document = self.store.get_resume_document(document_id=second.document_id)
        self.assertEqual(
            b"Ada Lovelace\nEngineer\n",
            self.storage.load_document(file_ref=first_document.file_ref),
        )
        self.assertEqual(
            b"Ada Lovelace\nPrincipal Engineer\n",
            self.storage.load_document(file_ref=second_document.file_ref),
        )

    def _write_source(self, filename: str, content: str) -> Path:
        source = self.workspace / "incoming" / filename
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(content, encoding="utf-8")
        return source


if __name__ == "__main__":
    unittest.main()
