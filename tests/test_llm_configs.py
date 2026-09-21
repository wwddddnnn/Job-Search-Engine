"""S3a: local-only HTTP, controlled credentials, migration and failure contracts."""

from contextlib import redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_web
from job_search_assistant.app_services.llm_configs import LLMConfigService
from job_search_assistant.core.context import RequestContext
from job_search_assistant.core.errors import InfrastructureError
from job_search_assistant.core.secrets import MemorySecretStore
from job_search_assistant.infrastructure.files.secret_store import FileSecretStore
from job_search_assistant.infrastructure.sqlite.database import SQLiteDatabase
from job_search_assistant.infrastructure.sqlite.llm_store import SQLiteLLMStore

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"


class LLMHTTPTests(unittest.TestCase):
    # Reuse the loopback harness, without rerunning inherited WebTests.
    setUp = test_web.WebTests.setUp
    build_service = test_web.WebTests.build_service
    start_server = test_web.WebTests.start_server
    request = test_web.WebTests.request
    assert_error = test_web.WebTests.assert_error

    def body(self, **changes):
        return {"name": "Local test", "api_url": "https://example.invalid/v1", "model": "demo",
                "idempotency_key": str(uuid4()), **changes}

    def create(self, **changes):
        status, result, _ = self.request(
            "/api/llm/configs", method="POST", body=self.body(**changes),
        )
        self.assertEqual(200, status, result)
        return result

    def write(self, path, body, method="PUT"):
        return self.request(path, method=method, body={"idempotency_key": str(uuid4()), **body})

    def test_restart_edit_switch_delete_and_permissions(self):
        key = secrets.token_urlsafe(32)
        first = self.create(api_key=key)
        second = self.create(name="Other")
        self.assertTrue(first["has_key"])
        self.assertFalse(second["has_key"])
        secret_file = self.root / "secrets" / first["id"]
        self.assertEqual(0o700, stat.S_IMODE(secret_file.parent.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(secret_file.stat().st_mode))
        self.assertEqual(key, secret_file.read_text())
        path = "/api/llm/configs/" + first["id"]
        for fields in ({"name": "Renamed"}, {"api_url": "http://localhost:9000/v2"},
                       {"model": "new-model"}):
            self.assertEqual(200, self.write(path, fields)[0])
            self.assertEqual(key, secret_file.read_text())
        for identifier in (first["id"], second["id"], first["id"]):
            response = self.write("/api/llm/selection", {"config_id": identifier})
            self.assertEqual((200, {"config_id": identifier}), response[:2])
        restarted = self.build_service()
        rows = restarted.llm.configs()
        self.assertEqual(2, len(rows))
        row = next(row for row in rows if row["id"] == first["id"])
        self.assertEqual("Renamed", row["name"])
        self.assertEqual("new-model", row["model"])
        self.assertTrue(row["has_key"])
        self.assertEqual({"config_id": first["id"]}, restarted.llm.selection())
        self.assertEqual(key, FileSecretStore(self.root / "secrets").get(first["id"]))
        self.assertEqual(200, self.write(path, {}, "DELETE")[0])
        self.assertFalse(secret_file.exists())
        self.assertEqual({"config_id": None}, restarted.llm.selection())
        self.assertEqual([second], restarted.llm.configs())

    def test_mask_omission_clear_reset_and_no_leaks(self):
        key = secrets.token_urlsafe(32)
        captured = io.StringIO()
        with redirect_stderr(captured):
            config = self.create(api_key=key)
            path = "/api/llm/configs/" + config["id"]
            for mask in ("••••••••", "********", "sk-***abcd", "●●●●", "…"):
                response = self.write(path, {"api_key": mask})
                self.assert_error(response, 422, "validation_error")
                self.assertNotIn(key, json.dumps(response[1]))
                self.assertEqual(key, FileSecretStore(self.root / "secrets").get(config["id"]))
            self.assertEqual(200, self.write(path, {"name": "Preserved"})[0])
            self.assertEqual(key, FileSecretStore(self.root / "secrets").get(config["id"]))
            response = self.request("/api/llm/configs")
            self.assertNotIn(key, json.dumps(response[1]))
            self.assertEqual({"id", "name", "api_url", "model", "created_at", "updated_at",
                              "has_key"}, set(response[1][0]))
            with patch.object(self.service.llm, "configs", side_effect=InfrastructureError(key)):
                error = self.request("/api/llm/configs")
                self.assert_error(error, 500, "infrastructure_error")
                self.assertNotIn(key, json.dumps(error[1]))
            self.assertEqual(200, self.write(path, {"api_key": ""})[0])
            self.assertFalse(self.request("/api/llm/configs")[1][0]["has_key"])
            self.assertFalse((self.root / "secrets" / config["id"]).exists())
            new_key = secrets.token_urlsafe(32)
            self.assertEqual(200, self.write(path, {"api_key": new_key})[0])
            self.assertEqual(new_key, FileSecretStore(self.root / "secrets").get(config["id"]))
        self.assertNotIn(key, captured.getvalue())
        self.assertNotIn(new_key, captured.getvalue())
        db = SQLiteDatabase(self.root / "db.sqlite", MIGRATIONS)
        audits = [dict(row) for row in db.fetch_all("SELECT * FROM audit_events")]
        self.assertGreaterEqual(len(audits), 4)
        for row in audits:
            self.assertEqual("local-user", row["actor_id"])
            self.assertTrue(row["correlation_id"])
            self.assertTrue(row["action"].startswith("llm."))
        for value in (key, new_key):
            self.assertNotIn(value, json.dumps(audits))
            for file in self.root.glob("*.sqlite*"):
                self.assertNotIn(value.encode(), file.read_bytes())

    def test_guards_and_validation(self):
        path = "/api/llm/configs"
        for token in (None, "wrong"):
            self.assert_error(self.request(path, method="POST", body=self.body(),
                                          headers={"X-JSA-Token": token}),
                              403, "authorization_error")
        self.assert_error(self.request(path, headers={"Host": "evil.invalid"}),
                          403, "authorization_error")
        self.assert_error(self.request(path, method="POST", body=self.body(),
                                      headers={"Content-Type": "text/plain"}),
                          415, "unsupported_media_type")
        self.assert_error(self.request(path, method="POST", raw=b"x" * (2 * 1024**2 + 1)),
                          413, "payload_too_large")
        self.assert_error(self.request(path, method="POST", raw=b"{"), 400, "bad_request")
        for body in (self.body(unknown=True), [], "text", None,
                     self.body(api_key=None), self.body(api_url="https://user:pw@host"),
                     self.body(api_url="https://host?api_key=not-a-real-key")):
            self.assert_error(self.request(path, method="POST", raw=json.dumps(body).encode()),
                              422, "validation_error")
        self.assert_error(self.write("/api/llm/selection", {"config_id": "absent"}),
                          404, "not_found")

    def test_idempotency_replays_all_commands_and_conflicts(self):
        body = self.body(api_key=secrets.token_urlsafe(32))
        first = self.request("/api/llm/configs", method="POST", body=body)
        again = self.request("/api/llm/configs", method="POST", body=body)
        self.assertEqual(first[:2], again[:2])
        self.assertEqual(1, len(self.request("/api/llm/configs")[1]))
        restarted = self.start_server(self.build_service())
        replay = self.request("/api/llm/configs", method="POST", body=body, server=restarted)
        self.assertEqual(first[:2], replay[:2])
        self.assert_error(self.request("/api/llm/configs", method="POST",
                                       body={**body, "api_key": secrets.token_urlsafe(32)}),
                          409, "conflict")
        identifier = first[1]["id"]
        for method, path, fields in (
            ("PUT", "/api/llm/configs/" + identifier, {"name": "new"}),
            ("PUT", "/api/llm/selection", {"config_id": identifier}),
            ("DELETE", "/api/llm/configs/" + identifier, {}),
        ):
            payload = {**fields, "idempotency_key": str(uuid4())}
            a = self.request(path, method=method, body=payload)
            b = self.request(path, method=method, body=payload)
            self.assertEqual(200, a[0])
            self.assertEqual(a[:2], b[:2])
        db = SQLiteDatabase(self.root / "db.sqlite", MIGRATIONS)
        self.assertEqual(4, len(db.fetch_all("SELECT * FROM audit_events")))
        selection_audit = db.fetch_all(
            "SELECT target_id FROM audit_events WHERE action = 'llm.select'",
        )
        self.assertEqual("selection", selection_audit[0]["target_id"])


class LLMStorageTests(unittest.TestCase):
    def test_memory_store_and_transaction_compensation(self):
        with tempfile.TemporaryDirectory() as directory:
            db = SQLiteDatabase(Path(directory) / "db.sqlite", MIGRATIONS)
            db.migrate()
            store, credentials = SQLiteLLMStore(db), MemorySecretStore()
            service = LLMConfigService(store, credentials)
            context = RequestContext.create(actor_id="tester")
            key = secrets.token_urlsafe(32)
            config = service.command("create", {"name": "test", "api_url": "https://host",
                "model": "test", "api_key": key, "idempotency_key": "create"}, context=context)
            with patch("job_search_assistant.infrastructure.sqlite.llm_store."
                       "LLMTransaction.complete", side_effect=InfrastructureError()):
                for action, fields in (("delete", {}), ("update", {"api_key": ""})):
                    with self.assertRaises(InfrastructureError):
                        service.command(action, {**fields, "idempotency_key": action},
                                        config_id=config["id"], context=context)
                    self.assertEqual(key, credentials.get(config["id"]))
                    self.assertEqual([config], service.configs())
            service.command("delete", {"idempotency_key": "delete"},
                            config_id=config["id"], context=context)
            self.assertIsNone(credentials.get(config["id"]))

    def test_file_store_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = FileSecretStore(root / "secrets")
            outside = root / "outside"
            outside.write_text("not a credential")
            (root / "secrets" / "linked").symlink_to(outside)
            with self.assertRaises(InfrastructureError):
                store.get("linked")
            with self.assertRaises(InfrastructureError):
                store.get("../outside")
            self.assertEqual("not a credential", outside.read_text())

    def test_additive_upgrade_and_ignored_secret_directory(self):
        checksums = {
            "0001_foundation.sql":
                "f2a165da3a7f650a204beae8dc95a35931b63ab815645c8bd5499f20136b33b8",
            "0002_job_discovery.sql":
                "094008550bdaec897a04ffa35924caf24440e2f6aa39748f1428cf88774dea64",
            "0003_discovery_run_recovery.sql":
                "5d44b528bd8fe1f26667f9e11989133b8749705de65d6bf1872def795be68476",
            "0004_career_foundation.sql":
                "eefcd9c70b31e9cfd12651c79d8ff2914484440899d02045af1c6646bb200163",
            "0005_career_review.sql":
                "0a5124c53a33e994028f8277eb442965a6668604867f738732475d7632368c72",
            "0006_ui_settings.sql":
                "5798426f43eb94a03e985868d06fe64cf43703fd85eb88353832d123eed93ac8",
        }
        for name, checksum in checksums.items():
            self.assertEqual(checksum, hashlib.sha256((MIGRATIONS / name).read_bytes()).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "migrations"
            old.mkdir()
            for source in sorted(MIGRATIONS.glob("000[1-6]_*.sql")):
                shutil.copyfile(source, old / source.name)
            db = SQLiteDatabase(root / "db.sqlite", old)
            self.assertEqual(6, len(db.migrate()))
            before = [dict(row) for row in db.fetch_all("SELECT * FROM schema_migrations")]
            upgraded = SQLiteDatabase(root / "db.sqlite", MIGRATIONS)
            self.assertEqual(7, len(upgraded.migrate()))
            after = [dict(row) for row in upgraded.fetch_all(
                "SELECT * FROM schema_migrations WHERE version < '0007'",
            )]
            self.assertEqual(before, after)
            self.assertEqual("zh", upgraded.fetch_all("SELECT language FROM ui_settings")[0][0])
            self.assertEqual([], upgraded.fetch_all("SELECT * FROM llm_configs"))
            sql = (MIGRATIONS / "0007_llm_configs.sql").read_text().upper()
            self.assertNotIn("DROP ", sql)
            self.assertNotIn("ALTER ", sql)
            self.assertNotIn("API_KEY", sql)
        ignored = subprocess.run(
            ["git", "check-ignore", ".job-search-assistant/secrets/test", "custom/secrets/test"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        self.assertEqual(2, len(ignored.stdout.splitlines()))


class LLMInProcessHTTPTests(LLMHTTPTests):
    """Supplement real loopback coverage with the same handler over memory streams.

    This does not replace or skip the required socket integration tests.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = self.build_service()
        self.server = self.start_server(self.service)

    def start_server(self, service):
        from types import SimpleNamespace
        return SimpleNamespace(service=service, token=secrets.token_urlsafe(32), server_port=8000)

    def request(self, path, *, method="GET", body=None, raw=None, headers=None, server=None):
        from email.parser import Parser
        from job_search_assistant.adapters.web.server import RequestHandler

        server = server or self.server
        supplied = {"Host": "127.0.0.1:8000", "Content-Type": "application/json"}
        if method != "GET":
            supplied["X-JSA-Token"] = server.token
        supplied.update(headers or {})
        data = raw if raw is not None else json.dumps(body).encode()
        supplied["Content-Length"] = str(len(data))
        lines = [f"{method} {path} HTTP/1.1"] + [
            f"{key}: {value}" for key, value in supplied.items() if value is not None]
        wire = ("\r\n".join(lines) + "\r\n\r\n").encode() + data

        class Connection:
            def __init__(self):
                self.output = bytearray()

            def makefile(self, *args):
                return io.BytesIO(wire)

            def settimeout(self, timeout):
                pass

            def sendall(self, content):
                self.output.extend(content)

        connection = Connection()
        RequestHandler(connection, ("127.0.0.1", 12345), server)
        head, content = bytes(connection.output).split(b"\r\n\r\n", 1)
        status, *header_lines = head.decode().split("\r\n")
        return (int(status.split()[1]), json.loads(content),
                Parser().parsestr("\n".join(header_lines)))
