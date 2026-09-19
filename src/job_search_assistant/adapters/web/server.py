"""Stdlib loopback-only JSON and static adapter; no domain persistence access."""

from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from job_search_assistant.core.context import RequestContext
from job_search_assistant.core.errors import (
    ApplicationError, AuthorizationError, ConflictError, InfrastructureError,
    InvalidStateError, NotFoundError, ValidationError,
)

MAX_BODY = 2 * 1024 * 1024
STATIC_ROOT = Path(__file__).parent / "static"
CONTENT_TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}
ERROR_STATUS = {
    ValidationError: 422, NotFoundError: 404, ConflictError: 409,
    InvalidStateError: 409, InfrastructureError: 500, AuthorizationError: 403,
}


class HTTPProblem(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


class LocalHTTPServer(ThreadingHTTPServer):
    """One token per server lifetime; bind only IPv4 loopback, including in tests."""

    daemon_threads = False
    block_on_close = True

    def __init__(self, address, service, *, incoming_dir):
        if address[0] != "127.0.0.1":
            raise ValueError("Only 127.0.0.1 is allowed.")
        self.service = service
        self.token = secrets.token_urlsafe(32)
        self.incoming_dir = Path(incoming_dir).resolve()
        self.import_lock = threading.Lock()
        super().__init__(address, RequestHandler)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "JSA"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # Resume content, token and untrusted request targets never enter console logs.
        pass

    def send_error(self, code, message=None, explain=None):
        self.context = RequestContext.create(source="local-http")
        self._error(code, "http_error", "HTTP request rejected.")

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def _handle(self):
        self.context = RequestContext.create(actor_id="local-user", source="local-http")
        try:
            hosts = self.headers.get_all("Host", [])
            allowed = {"127.0.0.1", f"127.0.0.1:{self.server.server_port}"}
            if len(hosts) != 1 or hosts[0] not in allowed:
                raise HTTPProblem(403, "authorization_error", "Host is not allowed.")
            raw_target = self.requestline.split()[1]
            try:
                target = urlsplit(raw_target)
                path = unquote(target.path, errors="strict")
            except (ValueError, UnicodeError):
                raise HTTPProblem(400, "bad_request", "Invalid request target.") from None
            if (target.scheme or target.netloc or not raw_target.startswith("/")
                    or raw_target.startswith("//") or "\\" in path or "\x00" in path
                    or ".." in path.split("/") or path.startswith("//")):
                raise HTTPProblem(404, "not_found", "Path is not available.")
            if target.query or target.fragment:
                raise HTTPProblem(400, "bad_request", "Query parameters are not supported.")
            body = None
            if self.command != "GET":
                tokens = self.headers.get_all("X-JSA-Token", [])
                if (len(tokens) != 1 or not tokens[0].isascii()
                        or not secrets.compare_digest(tokens[0], self.server.token)):
                    raise HTTPProblem(403, "authorization_error", "Write token is required.")
                body = self._body(review_command=path.startswith("/api/review/"))
            if path == "/api" or path.startswith("/api/"):
                self._json(200, self._route(path, body))
            elif self.command == "GET":
                self._static(path)
            else:
                raise HTTPProblem(404, "not_found", "Endpoint is not available.")
        except HTTPProblem as exc:
            self._error(exc.status, exc.code, exc.message)
        except ApplicationError as exc:
            status = next((value for kind, value in ERROR_STATUS.items()
                           if isinstance(exc, kind)), 500)
            # Details and storage paths are not part of the HTTP error contract.
            message = "Operation failed." if status == 500 else "Request could not be completed."
            self._error(status, exc.code, message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._error(500, "infrastructure_error", "Operation failed.")

    def _body(self, *, review_command=False):
        if self.headers.get("Transfer-Encoding"):
            raise HTTPProblem(400, "bad_request", "Transfer encoding is not supported.")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            raise HTTPProblem(400, "bad_request", "Content-Length is required.")
        if len(lengths[0]) > 10:
            raise HTTPProblem(413, "payload_too_large", "Request exceeds 2 MB.")
        length = int(lengths[0])
        if length > MAX_BODY:
            raise HTTPProblem(413, "payload_too_large", "Request exceeds 2 MB.")
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPProblem(415, "unsupported_media_type", "application/json is required.")
        try:
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError("Incomplete body")
            result = json.loads(data.decode("utf-8"), parse_constant=_reject_constant,
                                object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, RecursionError, TimeoutError):
            raise HTTPProblem(400, "bad_request", "Invalid JSON body.") from None
        # Review DTO shape errors belong to the application service (422); keep older routes
        # on their existing malformed-body contract (400).
        if not isinstance(result, dict) and not review_command:
            raise HTTPProblem(400, "bad_request", "JSON body must be an object.")
        return result

    @staticmethod
    def _fields(body, required=(), optional=()):
        if set(body) - set(required) - set(optional) or set(required) - set(body):
            raise ValidationError("Unknown or missing fields.")
        for name, value in body.items():
            if not isinstance(value, str) or (name != "content" and not value.strip()):
                raise ValidationError("Fields must be strings and identifiers must not be blank.")
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise ValidationError("Fields must contain valid UTF-8 text.") from None

    def _route(self, path, body):
        service = self.server.service
        if self.command == "GET":
            if path == "/api/session":
                return {"token": self.server.token, **service.settings(),
                        "profile": service.profile()}
            if path == "/api/documents":
                return service.documents()
            if path.startswith("/api/documents/") and path.count("/") == 3:
                return service.document(path.rsplit("/", 1)[1])
            if path == "/api/profile":
                return service.profile()
            if path == "/api/profile/versions":
                return service.history()
            if path.startswith("/api/profile/versions/") and path.count("/") == 4:
                return service.profile(path.rsplit("/", 1)[1])
            if path == "/api/draft":
                return service.draft()
            if path == "/api/settings/ui":
                return service.settings()
        if (self.command == "POST" and path.startswith("/api/review/")
                and path.count("/") == 3):
            return service.review_command(path.rsplit("/", 1)[1], body, context=self.context)
        if self.command == "POST" and path == "/api/documents":
            self._fields(body, ("filename", "content", "idempotency_key"))
            filename = body["filename"]
            if (not filename.lower().endswith(".md") or "/" in filename or "\\" in filename
                    or any(ord(char) < 32 for char in filename)):
                raise ValidationError("A Markdown filename is required.")
            try:
                content = body["content"].encode("utf-8")
            except UnicodeError:
                raise ValidationError("Content must be valid UTF-8.") from None
            # Stable staging reference preserves S1's file_ref-based idempotency, including
            # changed-content conflicts. Serialize staging and always remove the temporary file.
            digest = sha256(filename.encode("utf-8") + b"\0" + content).hexdigest()
            with self.server.import_lock:
                self.server.incoming_dir.mkdir(parents=True, exist_ok=True)
                staged = self.server.incoming_dir / f"{digest}.md"
                try:
                    with staged.open("xb") as handle:
                        handle.write(content)
                except FileExistsError:
                    if staged.is_symlink() or staged.read_bytes() != content:
                        raise InfrastructureError("Staging file is unavailable.")
                try:
                    return service.import_document(
                        file_ref=staged, filename=filename, context=self.context,
                        idempotency_key=body["idempotency_key"],
                    )
                finally:
                    staged.unlink(missing_ok=True)
        if self.command == "POST" and path == "/api/draft":
            self._fields(body, optional=("display_name", "idempotency_key"))
            return service.open_draft(
                display_name=body.get("display_name"), context=self.context,
                idempotency_key=body.get("idempotency_key", str(uuid4())),
            )
        if self.command == "PUT" and path == "/api/settings/ui":
            self._fields(body, ("language",), ("idempotency_key",))
            return service.save_settings(
                language=body["language"], context=self.context,
                idempotency_key=body.get("idempotency_key", str(uuid4())),
            )
        raise HTTPProblem(404, "not_found", "Endpoint is not available.")

    def _static(self, path):
        root = STATIC_ROOT.resolve()
        candidate = (root / ("index.html" if path == "/" else path[1:])).resolve()
        if (not candidate.is_relative_to(root) or not candidate.is_file()
                or candidate.suffix not in CONTENT_TYPES):
            self._send(404, b"Not found", "text/plain")
            return
        self._send(200, candidate.read_bytes(), CONTENT_TYPES[candidate.suffix])

    def _error(self, status, code, message):
        self._json(status, {"error": {"code": code, "message": message,
                                      "correlation_id": self.context.correlation_id}})

    def _json(self, status, payload):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json")

    def _send(self, status, content, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                         "style-src 'self'; object-src 'none'; base-uri 'none'; "
                         "frame-ancestors 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(content)


def _reject_constant(value):
    raise ValueError("Non-finite JSON")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result
