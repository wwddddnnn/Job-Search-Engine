"""Application facade DTO tests independent of HTTP socket availability."""

from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from job_search_assistant.app_services.local_ui import build_local_ui
from job_search_assistant.core.context import RequestContext
from job_search_assistant.core.errors import ConflictError, ValidationError


class LocalReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.context = RequestContext.create(actor_id="test", source="test")
        self.service = build_local_ui(database_path=self.root / "db.sqlite",
                                      migrations_path=Path(__file__).resolve().parents[1]
                                      / "migrations")
        self.draft = self.service.open_draft(display_name="Test", context=self.context,
                                             idempotency_key="open")

    def command(self, action, **payload):
        result = self.service.review_command(action, {
            "draft_id": self.draft["id"], "expected_version": self.draft["version"],
            **({} if action == "preview" else {"idempotency_key": str(uuid4())}), **payload,
        }, context=self.context)
        if action != "preview":
            self.draft = result.get("draft", result)
        return result

    def test_locator_source_preservation_and_readonly_history_projection(self):
        source = self.root / "resume.md"
        source.write_text("# 原文 😀\r\nRepeated **result**", encoding="utf-8")
        imported = self.service.import_document(file_ref=source, filename="resume.md",
                                                 context=self.context, idempotency_key="import")
        self.command("create", kind="experience", fields={"organization": "A", "role": "B"},
                     selection={"document_id": imported["document_id"], "start": 2, "end": 6})
        item = self.draft["items"][0]
        self.assertEqual("原文 😀", item["sources"][0]["source_excerpt"])
        self.assertEqual("codepoint:2:6", item["sources"][0]["source_locator"])
        self.command("decide", item_id=item["id"], decision="confirm")
        preview = self.command("preview", base_version_id=None)
        self.assertEqual("B", preview["content"][0]["fields"]["role"])
        first = self.command("publish", base_version_id=None)
        old = self.service.profile(first["profile_version_id"])
        self.command("edit", item_id=item["id"], changes={"role": "C"})
        self.assertEqual(item["sources"], self.draft["items"][0]["sources"])
        self.command("decide", item_id=item["id"], decision="confirm")
        self.command("publish", base_version_id=self.draft["base_version_id"])
        self.assertEqual(2, len(self.service.history()))
        self.assertEqual(old, self.service.profile(first["profile_version_id"]))
        self.assertEqual("C", self.service.profile()["experiences"][0]["role"])
        self.assertEqual(source.read_bytes().decode(),
                         self.service.document(imported["document_id"])["content"])

    def test_invalid_ui_dtos_and_version_conflict(self):
        for payload in (
            {"kind": "experience", "fields": [], "extra": True},
            {"kind": "experience", "fields": {"organization": "A", "role": "B", "x": 1}},
            {"kind": "experience", "fields": {}, "expected_version": True},
            {"kind": "experience", "fields": {}, "selection": []},
            {"kind": "experience", "fields": {"organization": "A", "role": "\ud800"}},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                self.command("create", **payload)
        self.command("create", kind="experience", fields={"organization": "A", "role": "B"})
        with self.assertRaises(ConflictError):
            self.command("preview", base_version_id="stale")
