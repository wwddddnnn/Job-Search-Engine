"""Immutable, content-addressed storage for Career source documents and text."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping

from job_search_assistant.core.errors import InfrastructureError, NotFoundError, ValidationError


_CONTENT_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ORIGINAL_NAME = "original.bin"
_TEXT_NAME = "extracted-text.json"


class FileSystemDocumentStorage:
    """Store immutable document artifacts below ``.job-search-assistant/documents``.

    Each artifact is addressed by the SHA-256 hash of its own content and lives
    at ``<root>/<content_hash>/``.  Text artifacts are a single JSON file with
    ``text`` and ``locator_map`` fields, so a ``ResumeText.locator_map_ref`` is
    intentionally the same opaque reference as ``ResumeText.text_ref``.
    Existing artifacts are never overwritten: a repeat write succeeds only if
    its bytes are identical, making duplicate imports safe and idempotent.
    """

    def __init__(self, root_dir: Path | str = ".job-search-assistant/documents") -> None:
        self._root_dir = Path(root_dir)

    @property
    def root_dir(self) -> Path:
        """Return the configured root, primarily for composition and diagnostics."""
        return self._root_dir

    def store_document(self, *, content: bytes, content_hash: str) -> str:
        """Persist original bytes under their verified SHA-256 address."""
        if not isinstance(content, bytes):
            raise ValidationError("Document content must be bytes.", details={"field": "content"})
        normalized_hash = _require_content_hash(content_hash)
        actual_hash = sha256(content).hexdigest()
        if actual_hash != normalized_hash:
            raise ValidationError(
                "Document content does not match its declared content hash.",
                details={
                    "declared_content_hash": normalized_hash,
                    "actual_content_hash": actual_hash,
                },
            )
        reference = f"{normalized_hash}/{_ORIGINAL_NAME}"
        self._store_immutable(reference, content)
        return reference

    def load_document(self, *, file_ref: str) -> bytes:
        """Load original document bytes from a reference returned by this adapter."""
        return self._load_bytes(file_ref=file_ref, artifact_type="document")

    def store_text(
        self,
        *,
        text: str,
        content_hash: str,
        locator_map: Mapping[str, Any] | None = None,
    ) -> str:
        """Persist immutable UTF-8 text together with its source-location metadata."""
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("Extracted text must be non-blank.", details={"field": "text"})
        normalized_hash = _require_content_hash(content_hash)
        actual_hash = sha256(text.encode("utf-8")).hexdigest()
        if actual_hash != normalized_hash:
            raise ValidationError(
                "Extracted text does not match its declared content hash.",
                details={
                    "declared_content_hash": normalized_hash,
                    "actual_content_hash": actual_hash,
                },
            )
        if locator_map is not None and not isinstance(locator_map, Mapping):
            raise ValidationError(
                "locator_map must be an object.",
                details={"field": "locator_map"},
            )
        payload = {"text": text, "locator_map": _json_value(locator_map or {})}
        try:
            serialized = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "locator_map must contain JSON-compatible values.",
                details={"field": "locator_map", "reason": str(exc)},
            ) from exc
        reference = f"{normalized_hash}/{_TEXT_NAME}"
        self._store_immutable(reference, serialized)
        return reference

    def load_text(self, *, text_ref: str) -> str:
        """Load the text portion of an immutable text-and-locator artifact."""
        payload = self._load_text_payload(text_ref)
        text = payload.get("text")
        if not isinstance(text, str):
            raise InfrastructureError(
                "Stored extracted-text artifact is malformed.",
                details={"text_ref": text_ref, "field": "text"},
            )
        return text

    def load_locator_map(self, *, text_ref: str) -> Mapping[str, Any]:
        """Load the locator map stored in the same immutable artifact as its text."""
        payload = self._load_text_payload(text_ref)
        locator_map = payload.get("locator_map")
        if not isinstance(locator_map, Mapping):
            raise InfrastructureError(
                "Stored extracted-text artifact is malformed.",
                details={"text_ref": text_ref, "field": "locator_map"},
            )
        return dict(locator_map)

    def _load_text_payload(self, text_ref: str) -> Mapping[str, Any]:
        raw = self._load_bytes(file_ref=text_ref, artifact_type="extracted text")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InfrastructureError(
                "Stored extracted-text artifact is not valid UTF-8 JSON.",
                details={"text_ref": text_ref},
            ) from exc
        if not isinstance(payload, Mapping):
            raise InfrastructureError(
                "Stored extracted-text artifact is malformed.",
                details={"text_ref": text_ref},
            )
        return payload

    def _store_immutable(self, reference: str, content: bytes) -> None:
        path = self._path_for_reference(reference)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(content)
        except FileExistsError:
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise InfrastructureError(
                    "Existing document artifact could not be read.",
                    details={"reference": reference},
                ) from exc
            if existing != content:
                raise InfrastructureError(
                    "A content-addressed artifact already exists with different content.",
                    details={"reference": reference},
                )
        except OSError as exc:
            raise InfrastructureError(
                "Document artifact could not be stored.",
                details={"reference": reference},
            ) from exc

    def _load_bytes(self, *, file_ref: str, artifact_type: str) -> bytes:
        path = self._path_for_reference(file_ref)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise NotFoundError(artifact_type, file_ref) from exc
        except OSError as exc:
            raise InfrastructureError(
                "Document artifact could not be read.",
                details={"reference": file_ref, "artifact_type": artifact_type},
            ) from exc

    def _path_for_reference(self, reference: str) -> Path:
        if not isinstance(reference, str) or not reference.strip():
            raise ValidationError(
                "Document reference must be non-blank.",
                details={"field": "reference"},
            )
        relative_path = Path(reference)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValidationError("Document reference is invalid.", details={"field": "reference"})
        root = self._root_dir.resolve()
        path = (root / relative_path).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValidationError(
                "Document reference is invalid.",
                details={"field": "reference"},
            ) from exc
        return path


def _require_content_hash(value: str) -> str:
    if not isinstance(value, str) or _CONTENT_HASH_PATTERN.fullmatch(value) is None:
        raise ValidationError(
            "content_hash must be a lowercase SHA-256 hexadecimal digest.",
            details={"field": "content_hash"},
        )
    return value


def _json_value(value: Any) -> Any:
    """Convert frozen domain mappings and tuples to JSON-compatible containers."""
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value
