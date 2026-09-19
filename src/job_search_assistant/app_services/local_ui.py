"""Application facade and composition for the local review UI."""

from dataclasses import asdict, dataclass
from pathlib import Path

from job_search_assistant.app_services.career import GetCareerProfileSnapshot, ImportResumeDocument
from job_search_assistant.app_services.career_review import CareerReviewService
from job_search_assistant.app_services.foundation import build_foundation
from job_search_assistant.career.extraction import DraftEvidence
from job_search_assistant.career.review import ReviewSource, identifier, require_version
from job_search_assistant.core.errors import InfrastructureError, NotFoundError, ValidationError
from job_search_assistant.infrastructure.files import (
    FileSystemDocumentStorage, PlainTextResumeExtractor,
)
from job_search_assistant.infrastructure.sqlite import SQLiteCareerStore, SQLiteReviewStore
from job_search_assistant.infrastructure.sqlite.ui_store import SQLiteUIStore


@dataclass
class LocalUIService:
    """Expose safe DTOs; HTTP never receives persistence or controlled storage references."""

    queries: SQLiteUIStore
    career: SQLiteCareerStore
    storage: FileSystemDocumentStorage
    importer: ImportResumeDocument
    review: CareerReviewService

    def documents(self):
        return [self._document_summary(self.career.get_resume_document(document_id=identifier))
                for identifier in self.queries.document_ids()]

    @staticmethod
    def _document_summary(document):
        return {"id": document.id, "filename": document.metadata.get("filename", "resume.md"),
                "imported_at": document.uploaded_at.isoformat(), "status": document.status.value}

    def document(self, document_id):
        document = self.career.get_resume_document(document_id=document_id)
        try:
            # Read original bytes, not extracted text (which may remove a UTF-8 BOM).
            content = self.storage.load_document(file_ref=document.file_ref).decode("utf-8")
        except (NotFoundError, UnicodeError) as exc:
            raise InfrastructureError("Original document is unavailable as UTF-8 text.") from exc
        return {**self._document_summary(document), "content": content}

    def import_document(self, *, file_ref, filename, idempotency_key, context):
        return asdict(self.importer.execute(
            file_ref=file_ref, metadata={"filename": filename, "mime_type": "text/markdown"},
            idempotency_key=idempotency_key, context=context,
        ))

    def profile(self, version_id=None):
        profile_id = self.queries.profile_id()
        if profile_id is None:
            return None
        return asdict(GetCareerProfileSnapshot(self.career).execute(
            profile_id=profile_id, profile_version_id=version_id,
        ))

    def draft(self):
        profile_id = self.queries.profile_id()
        draft_id = self.queries.draft_id(profile_id) if profile_id else None
        return self.review.get_draft(draft_id=draft_id).to_dict() if draft_id else None

    def open_draft(self, *, display_name, context, idempotency_key):
        return self.review.create_draft(
            profile_id=self.queries.profile_id() or "local-profile",
            display_name=display_name or "My career", context=context,
            idempotency_key=idempotency_key,
        ).to_dict()

    def history(self):
        profile_id = self.queries.profile_id()
        return self.queries.profile_versions(profile_id) if profile_id else []

    def review_command(self, action, payload, *, context):
        """Validate the UI command DTO before dispatching to transactional review services."""
        if not isinstance(payload, dict):
            raise ValidationError("Review command must be an object.")
        shapes = {
            "create": ({"kind", "fields"}, {"parent_id", "selection"}),
            "edit": ({"item_id", "changes"}, set()),
            "decide": ({"item_id", "decision"}, set()),
            "delete": ({"item_id"}, set()), "restore": ({"item_id"}, set()),
            "preview": ({"base_version_id"}, set()),
            "publish": ({"base_version_id"}, set()),
        }
        if action not in shapes:
            raise NotFoundError("review_action", action)
        required, optional = shapes[action]
        required = required | {"draft_id", "expected_version"}
        if action != "preview":
            required = required | {"idempotency_key"}
        if set(payload) - required - optional or required - set(payload):
            raise ValidationError("Unknown or missing review fields.")
        def validate_text(value):
            if isinstance(value, str):
                try:
                    value.encode("utf-8")
                except UnicodeError as exc:
                    raise ValidationError("Text must be valid UTF-8.") from exc
            elif isinstance(value, dict):
                for key, child in value.items():
                    validate_text(key)
                    validate_text(child)
            elif isinstance(value, list):
                for child in value:
                    validate_text(child)

        validate_text(payload)
        values = dict(payload)
        for name in ("draft_id", "item_id", "idempotency_key", "kind", "decision"):
            if name in values:
                values[name] = identifier(values[name], name)
        require_version(values["expected_version"])
        for name in ("parent_id", "base_version_id"):
            if name in values and values[name] is not None:
                values[name] = identifier(values[name], name)
        for name in ("fields", "changes"):
            if name in values and not isinstance(values[name], dict):
                raise ValidationError("Item fields must be an object.")
        if action == "create":
            selection = values.pop("selection", None)
            values["sources"] = self._selection_sources(selection)
        methods = {
            "create": self.review.create_item, "edit": self.review.edit_item,
            "decide": self.review.decide_item, "delete": self.review.delete_item,
            "restore": self.review.restore_item, "preview": self.review.preview_publication,
            "publish": self.review.publish,
        }
        result = methods[action](**values, context=context)
        return result.to_dict() if hasattr(result, "to_dict") else result

    def _selection_sources(self, selection):
        if selection is None:
            return ()
        if not isinstance(selection, dict) or set(selection) != {"document_id", "start", "end"}:
            raise ValidationError("Selection requires document_id and code-point offsets.")
        document_id = identifier(selection["document_id"], "document_id")
        start, end = selection["start"], selection["end"]
        content = self.document(document_id)["content"]
        if (type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(content) or not content[start:end].strip()):
            raise ValidationError("Selection offsets are outside the original document.")
        return (ReviewSource(document_id, DraftEvidence(
            source_excerpt=content[start:end], source_locator=f"codepoint:{start}:{end}",
        )),)

    def settings(self):
        return {"language": self.queries.language()}

    def save_settings(self, *, language, context, idempotency_key):
        if language not in ("zh", "en"):
            raise ValidationError("language must be zh or en.")
        return self.queries.save_language(
            language=language, context=context, idempotency_key=idempotency_key,
        )


def build_local_ui(*, database_path, migrations_path):
    foundation = build_foundation(database_path=database_path, migrations_path=migrations_path)
    career = SQLiteCareerStore(foundation.database)
    storage = FileSystemDocumentStorage(Path(database_path).resolve().parent / "documents")
    return LocalUIService(
        SQLiteUIStore(foundation.database), career, storage,
        ImportResumeDocument(career, storage, PlainTextResumeExtractor(storage)),
        CareerReviewService(SQLiteReviewStore(foundation.database)),
    )
