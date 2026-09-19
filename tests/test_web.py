"""Real loopback HTTP tests, exclusively stdlib urllib + ThreadingHTTPServer."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from job_search_assistant.__main__ import main
from job_search_assistant.adapters.web.server import LocalHTTPServer, MAX_BODY, STATIC_ROOT
from job_search_assistant.app_services.local_ui import build_local_ui
from job_search_assistant.core.errors import (
    ApplicationError, AuthorizationError, ConflictError, InfrastructureError,
    InvalidStateError, NotFoundError, ValidationError,
)

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = self.build_service()
        self.server = self.start_server(self.service)
        self.client = build_opener(ProxyHandler({}))

    def build_service(self):
        return build_local_ui(database_path=self.root / "db.sqlite", migrations_path=MIGRATIONS)

    def start_server(self, service):
        server = LocalHTTPServer(("127.0.0.1", 0), service, incoming_dir=self.root / "incoming")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join()

        self.addCleanup(close)
        return server

    def request(self, path, *, method="GET", body=None, raw=None, headers=None, server=None):
        server = server or self.server
        supplied = {"Content-Type": "application/json"}
        if method != "GET":
            supplied["X-JSA-Token"] = server.token
        supplied.update(headers or {})
        supplied = {key: value for key, value in supplied.items() if value is not None}
        data = raw if raw is not None else (
            json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        )
        request = Request(f"http://127.0.0.1:{server.server_port}/", data=data,
                          headers=supplied, method=method)
        # Preserve hostile request targets instead of letting URL joining normalize them.
        request.selector = path
        try:
            response = self.client.open(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            content = response.read()
            result = (json.loads(content)
                      if response.headers.get_content_type() == "application/json" else content)
            return response.status, result, response.headers

    def assert_error(self, response, status, code):
        actual, payload, headers = response
        self.assertEqual(status, actual, payload)
        self.assertEqual("application/json", headers.get_content_type())
        self.assertEqual({"error"}, set(payload))
        self.assertEqual({"code", "message", "correlation_id"}, set(payload["error"]))
        self.assertEqual(code, payload["error"]["code"])
        self.assertTrue(payload["error"]["correlation_id"])
        self.assertNotIn("private-secret", json.dumps(payload))
        return payload["error"]["correlation_id"]

    def import_body(self, **changes):
        return {"filename": "简历.md", "content": "# Resume\n", "idempotency_key": "import-1",
                **changes}

    def test_error_mapping_and_request_scoped_correlation(self):
        errors = [
            (ValidationError("private-secret"), 422),
            (NotFoundError("private-secret", "id"), 404),
            (ConflictError("private-secret"), 409),
            (InvalidStateError("private-secret", "id", "draft", "publish"), 409),
            (InfrastructureError("private-secret", details={"path": "private-secret"}), 500),
            (AuthorizationError(), 403),
            (ApplicationError("custom_error", "private-secret"), 500),
        ]
        ids = set()
        for error, status in errors:
            with self.subTest(error=error.code), patch.object(
                self.service, "documents", side_effect=error,
            ):
                ids.add(self.assert_error(self.request("/api/documents"), status, error.code))
        self.assertEqual(len(errors), len(ids))
        with patch.object(self.service, "documents", side_effect=RuntimeError("private-secret")):
            self.assert_error(self.request("/api/documents"), 500, "infrastructure_error")

    def test_write_context_matches_error_correlation(self):
        contexts = []

        def fail(**kwargs):
            contexts.append(kwargs["context"])
            raise ConflictError("private-secret")

        with patch.object(self.service, "open_draft", side_effect=fail):
            correlation = self.assert_error(
                self.request("/api/draft", method="POST", body={}), 409, "conflict",
            )
        self.assertEqual(correlation, contexts[0].correlation_id)
        self.assertEqual("local-user", contexts[0].actor_id)

    def test_token_and_host_protection(self):
        for token in (None, "incorrect", "é"):
            with self.subTest(token=token):
                self.assert_error(self.request(
                    "/api/draft", method="POST", body={}, headers={"X-JSA-Token": token},
                ), 403, "authorization_error")
        for host in ("evil.example", "localhost", "127.0.0.1.evil", "127.0.0.1:1"):
            self.assert_error(self.request("/api/session", headers={"Host": host}),
                              403, "authorization_error")
        self.assertEqual(200, self.request("/api/session", headers={"Host": "127.0.0.1"})[0])

    def test_static_content_types_and_no_directory_listing(self):
        for path, mime in (("/", "text/html"), ("/style.css", "text/css"),
                           ("/js/api.js", "text/javascript")):
            status, content, headers = self.request(path)
            self.assertEqual(200, status)
            self.assertIn("charset=utf-8", headers["Content-Type"])
            self.assertEqual(mime, headers.get_content_type())
            self.assertTrue(content)
        for path in ("/js/", "/js/views", "/unknown.html", "/server.py"):
            self.assertEqual(404, self.request(path)[0])

    def test_traversal_absolute_paths_and_symlinks_are_rejected(self):
        for path in ("/../server.py", "/%2e%2e/server.py", "/js/../../server.py",
                     "/%2fetc/passwd", "//etc/passwd", "/etc/passwd",
                     "http://127.0.0.1/js/api.js", "/%5c..%5cserver.py"):
            with self.subTest(path=path):
                self.assertEqual(404, self.request(path)[0])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "escape.html").symlink_to(STATIC_ROOT / "index.html")
            with patch("job_search_assistant.adapters.web.server.STATIC_ROOT", root):
                self.assertEqual(404, self.request("/escape.html")[0])

    def test_json_media_size_and_field_validation(self):
        for raw in (b"{", b"[]", b'{"language":NaN}', b'{"language":"en","language":"zh"}',
                    b'\xff'):
            self.assert_error(self.request("/api/settings/ui", method="PUT", raw=raw),
                              400, "bad_request")
        self.assert_error(self.request("/api/draft", method="POST", body={},
                                       headers={"Content-Type": "text/plain"}),
                          415, "unsupported_media_type")
        self.assert_error(self.request("/api/documents", method="POST", raw=b"{}",
                                       headers={"Content-Length": str(MAX_BODY + 1)}),
                          413, "payload_too_large")
        for body in ({"language": "fr"}, {"language": "en", "actor": "admin"},
                     {"language": 7}, {}):
            self.assert_error(self.request("/api/settings/ui", method="PUT", body=body),
                              422, "validation_error")
        for filename in ("a.txt", "../resume.md", "a\\b.md"):
            self.assert_error(self.request("/api/documents", method="POST",
                                           body=self.import_body(filename=filename)),
                              422, "validation_error")
        self.assert_error(self.request("/api/draft", method="POST", body={"items": []}),
                          422, "validation_error")

    def test_unknown_endpoints_and_methods_do_not_expose_html_errors(self):
        self.assert_error(self.request("/api/unknown"), 404, "not_found")
        self.assert_error(self.request("/api/draft/items", method="POST", body={}),
                          404, "not_found")
        self.assert_error(self.request("/api/draft", method="DELETE"), 501, "http_error")
        self.assert_error(self.request("/api/documents?internal=true"), 400, "bad_request")
        self.assert_error(self.request("/api/documents/absent"), 404, "not_found")

    def test_import_round_trip_exact_bytes_replay_and_restart(self):
        content = '\ufeff# 简历\r\n\r\n<script>alert(1)</script>\r\n[bad](javascript:alert(1))  \n'
        body = self.import_body(content=content)
        status, first, _ = self.request("/api/documents", method="POST", body=body)
        self.assertEqual(200, status, first)
        self.assertEqual("text_extracted", first["status"])
        self.assertEqual(first, self.request("/api/documents", method="POST", body=body)[1])
        documents = self.request("/api/documents")[1]
        self.assertEqual(1, len(documents))
        self.assertEqual("简历.md", documents[0]["filename"])
        self.assertEqual({"id", "filename", "imported_at", "status"}, set(documents[0]))
        original = self.request("/api/documents/" + first["document_id"])[1]["content"]
        self.assertEqual(content.encode("utf-8"), original.encode("utf-8"))
        self.assertEqual([], list((self.root / "incoming").iterdir()))
        self.assertEqual(content.encode("utf-8"), next(
            (self.root / "documents").glob("*/original.bin"),
        ).read_bytes())
        self.assert_error(self.request("/api/documents", method="POST",
                                       body=self.import_body(content="changed")), 409, "conflict")
        second = self.request("/api/documents", method="POST", body=self.import_body(
            filename="second.md", content="second", idempotency_key="second",
        ))[1]
        self.assertNotEqual(first["document_id"], second["document_id"])
        restarted = self.start_server(self.build_service())
        self.assertEqual(2, len(self.request("/api/documents", server=restarted)[1]))
        self.assertEqual(content, self.request(
            "/api/documents/" + first["document_id"], server=restarted,
        )[1]["content"])

    def test_settings_restart_token_rotation_and_audit(self):
        body = {"language": "en", "idempotency_key": "language-1"}
        self.assertEqual({"language": "zh"}, self.request("/api/settings/ui")[1])
        self.assertEqual({"language": "en"}, self.request(
            "/api/settings/ui", method="PUT", body=body,
        )[1])
        self.request("/api/settings/ui", method="PUT", body=body)
        restarted = self.start_server(self.build_service())
        session = self.request("/api/session", server=restarted)[1]
        self.assertEqual("en", session["language"])
        self.assertNotEqual(self.server.token, session["token"])
        self.assert_error(self.request("/api/settings/ui", method="PUT", body=body,
                                       headers={"X-JSA-Token": self.server.token},
                                       server=restarted),
                          403, "authorization_error")
        rows = self.service.queries.database.fetch_all(
            "SELECT actor_id, correlation_id FROM audit_events WHERE action = 'ui.settings.update'",
        )
        self.assertEqual(1, len(rows))
        self.assertEqual("local-user", rows[0]["actor_id"])
        self.assertTrue(rows[0]["correlation_id"])
        self.assert_error(self.request("/api/settings/ui", method="PUT", body={
            **body, "language": "zh",
        }), 409, "conflict")

    def test_empty_profile_draft_creation_and_restart(self):
        session = self.request("/api/session")[1]
        self.assertIsNone(session["profile"])
        self.assertIsNone(self.request("/api/draft")[1])
        body = {"display_name": "Test", "idempotency_key": "draft-1"}
        status, draft, _ = self.request("/api/draft", method="POST", body=body)
        self.assertEqual(200, status, draft)
        self.assertEqual([], draft["items"])
        profile = self.request("/api/profile")[1]
        self.assertEqual(0, profile["version"])
        self.assertIsNone(profile["profile_version_id"])
        restarted = self.start_server(self.build_service())
        self.assertEqual(draft, self.request("/api/draft", server=restarted)[1])
        self.assertEqual(draft, self.request(
            "/api/draft", method="POST", body={}, server=restarted,
        )[1])

    def test_profile_failures_are_not_empty_states(self):
        self.request("/api/draft", method="POST", body={})
        with patch.object(self.service.career, "get_career_profile",
                          side_effect=InfrastructureError("private-secret")):
            self.assert_error(self.request("/api/profile"), 500, "infrastructure_error")

    def review(self, action, draft, **payload):
        from uuid import uuid4
        return self.request("/api/review/" + action, method="POST", body={
            "draft_id": draft["id"], "expected_version": draft["version"],
            **({} if action == "preview" else {"idempotency_key": str(uuid4())}), **payload,
        })

    def test_review_http_partial_publish_sources_restore_and_history(self):
        document = self.request("/api/documents", method="POST", body=self.import_body(
            content="# 原文 😀\r\nRepeated **result**\nRepeated **result**",
        ))[1]
        draft = self.request("/api/draft", method="POST", body={})[1]
        response = self.review("create", draft, kind="experience",
                               fields={"organization": "A", "role": "Engineer"}, selection={
                                   "document_id": document["document_id"], "start": 2, "end": 6,
                               })
        self.assertEqual(200, response[0], response[1])
        draft = response[1]
        item = draft["items"][0]
        self.assertEqual("原文 😀", item["sources"][0]["source_excerpt"])
        self.assertEqual("codepoint:2:6", item["sources"][0]["source_locator"])
        draft = self.review("create", draft, kind="skill", parent_id=item["id"],
                            fields={"raw_skill_name": "Python"})[1]
        skill_id = draft["items"][1]["id"]
        draft = self.review("decide", draft, item_id=item["id"], decision="confirm")[1]
        self.assertEqual("draft", draft["items"][1]["status"])
        preview = self.review("preview", draft, base_version_id=None)[1]
        self.assertEqual(1, len(preview["content"]))
        result = self.review("publish", draft, base_version_id=None)[1]
        draft = result["draft"]
        version_id = result["profile_version_id"]
        old = self.request("/api/profile/versions/" + version_id)[1]
        self.assertEqual([], old["experiences"][0]["skills"])
        draft = self.review("edit", draft, item_id=item["id"],
                            changes={"role": "Unconfirmed new role"})[1]
        self.assertEqual(item["sources"], draft["items"][0]["sources"])
        draft = self.review("decide", draft, item_id=skill_id, decision="confirm")[1]
        result = self.review("publish", draft, base_version_id=version_id)[1]
        draft = result["draft"]
        current = self.request("/api/profile")[1]
        self.assertEqual("Engineer", current["experiences"][0]["role"])
        self.assertEqual("Python", current["experiences"][0]["skills"][0]["name"])
        self.assertEqual("Unconfirmed new role", draft["items"][0]["fields"]["role"])
        draft = self.review("delete", draft, item_id=item["id"])[1]
        draft = self.review("restore", draft, item_id=item["id"])[1]
        self.assertFalse(draft["items"][0]["deleted"])
        self.assertEqual("draft", draft["items"][0]["status"])
        draft = self.review("delete", draft, item_id=item["id"])[1]
        draft = self.review("decide", draft, item_id=item["id"], decision="confirm_delete")[1]
        preview = self.review("preview", draft, base_version_id=draft["base_version_id"])[1]
        self.assertEqual(2, len(preview["summary"]["deleted"]))
        self.review("publish", draft, base_version_id=draft["base_version_id"])
        self.assertEqual([], self.request("/api/profile")[1]["experiences"])
        self.assertEqual(old, self.request("/api/profile/versions/" + version_id)[1])
        self.assertEqual(3, len(self.request("/api/profile/versions")[1]))

    def test_review_http_failure_retry_idempotency_and_stale_save(self):
        draft = self.request("/api/draft", method="POST", body={})[1]
        payload = {"kind": "experience", "fields": {"organization": "A", "role": "B"},
                   "idempotency_key": "stable-create"}
        with patch.object(self.service.review.store, "execute_review_command",
                          side_effect=InfrastructureError("failed save")):
            self.assert_error(self.review("create", draft, **payload), 500,
                              "infrastructure_error")
        saved = self.review("create", draft, **payload)[1]
        self.assertEqual(saved, self.review("create", draft, **payload)[1])
        item_id = saved["items"][0]["id"]
        newest = self.review("edit", saved, item_id=item_id, changes={"role": "new"})[1]
        self.assert_error(self.review("edit", saved, item_id=item_id, changes={"role": "old"}),
                          409, "conflict")
        self.assertEqual(saved, self.review("create", draft, **payload)[1])
        self.assertEqual(newest, self.request("/api/draft")[1])
        self.assert_error(self.review("publish", newest, base_version_id="stale"),
                          409, "conflict")
        self.assert_error(self.review("preview", saved, base_version_id=None), 409, "conflict")

    def test_review_http_rejects_unknown_nested_fields_and_invalid_offsets(self):
        draft = self.request("/api/draft", method="POST", body={})[1]
        for payload in (
            {"kind": "experience", "fields": [], "other": True},
            {"kind": "experience", "fields": {"organization": "A", "role": "B", "extra": 1}},
            {"kind": "experience", "fields": {}, "selection": {"document_id": "x", "extra": 1}},
            {"kind": "experience", "fields": {}, "expected_version": True},
        ):
            self.assert_error(self.review("create", draft, **payload), 422, "validation_error")
        doc = self.request("/api/documents", method="POST", body=self.import_body())[1]
        self.assert_error(self.review("create", draft, kind="experience", fields={}, selection={
            "document_id": doc["document_id"], "start": 0, "end": 1000,
        }), 422, "validation_error")
        saved = self.review("create", draft, kind="experience",
                            fields={"organization": "A", "role": "B"})[1]
        item_id = saved["items"][0]["id"]
        self.assert_error(self.review("restore", saved, item_id=item_id), 409, "invalid_state")
        self.assert_error(self.review("edit", saved, item_id="missing", changes={"role": "B"}),
                          404, "not_found")

    def test_review_non_object_body_is_validation_error(self):
        self.assert_error(self.request("/api/review/create", method="POST",
                                       body=[{"a": 1}]), 422, "validation_error")

    def test_review_write_guards_cover_new_prefix(self):
        cases = [
            ({"X-JSA-Token": None}, 403, "authorization_error"),
            ({"X-JSA-Token": "wrong"}, 403, "authorization_error"),
            ({"Host": "evil.example"}, 403, "authorization_error"),
            ({"Content-Type": "text/plain"}, 415, "unsupported_media_type"),
            ({"Content-Length": str(MAX_BODY + 1)}, 413, "payload_too_large"),
        ]
        for headers, status, code in cases:
            with self.subTest(headers=headers):
                self.assert_error(self.request("/api/review/create", method="POST",
                                               raw=b"{}", headers=headers), status, code)
        self.assertIsNone(self.request("/api/draft")[1])

    def test_review_conflict_copy_reload_resave_and_publish(self):
        draft = self.request("/api/draft", method="POST", body={})[1]
        fields = {"organization": "Copied organization", "role": "Copied role"}
        saved = self.review("create", draft, kind="experience",
                            fields={"organization": "Other tab", "role": "Engineer"})[1]
        self.assert_error(self.review("create", draft, kind="experience", fields=fields,
                                      idempotency_key="failed-create"), 409, "conflict")
        latest = self.request("/api/draft")[1]
        self.assertEqual(saved, latest)
        status, recovered, _ = self.review("create", latest, kind="experience", fields=fields,
                                           idempotency_key="after-refresh")
        self.assertEqual(200, status)
        self.assertEqual(fields["role"], recovered["items"][-1]["fields"]["role"])
        recovered = self.review("decide", recovered, item_id=recovered["items"][-1]["id"],
                                decision="confirm")[1]
        self.assertEqual(200, self.review("preview", recovered, base_version_id=None)[0])
        self.assertEqual(200, self.review("publish", recovered, base_version_id=None)[0])

    def test_review_http_reject_clarify_and_edit_require_reconfirmation(self):
        draft = self.request("/api/draft", method="POST", body={})[1]
        draft = self.review("create", draft, kind="experience",
                            fields={"organization": "A", "role": "B"})[1]
        item_id = draft["items"][0]["id"]
        for decision, expected in (("reject", "rejected"), ("clarify", "needs_clarification")):
            draft = self.review("decide", draft, item_id=item_id, decision=decision)[1]
            self.assertEqual(expected, draft["items"][0]["status"])
            self.assertEqual([], self.review("preview", draft, base_version_id=None)[1]["content"])
            self.assertEqual(draft, self.request("/api/draft")[1])
        draft = self.review("decide", draft, item_id=item_id, decision="confirm")[1]
        self.assertEqual("verified", draft["items"][0]["status"])
        draft = self.review("edit", draft, item_id=item_id, changes={"role": "Edited"})[1]
        self.assertEqual("draft", draft["items"][0]["status"])
        self.assertEqual([], self.review("preview", draft, base_version_id=None)[1]["content"])
        draft = self.review("decide", draft, item_id=item_id, decision="confirm")[1]
        self.assertEqual("Edited", self.review("preview", draft, base_version_id=None)
                         [1]["content"][0]["fields"]["role"])

    def test_host_binding_and_cli_shutdown(self):
        with self.assertRaises(ValueError):
            LocalHTTPServer(("0.0.0.0", 0), self.service, incoming_dir=self.root / "incoming")
        args = ["--database", str(self.root / "cli.sqlite"), "--migrations", str(MIGRATIONS),
                "serve", "--port", "0"]
        with patch.object(LocalHTTPServer, "serve_forever", side_effect=KeyboardInterrupt), \
                patch("builtins.print") as output:
            self.assertEqual(0, main(args))
            self.assertIn("http://127.0.0.1:", output.call_args.args[0])
            self.assertIn("Ctrl-C", output.call_args.args[0])
        args[-1] = str(self.server.server_port)
        with patch("builtins.print") as output:
            self.assertEqual(1, main(args))
            self.assertIn("port may be in use", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
