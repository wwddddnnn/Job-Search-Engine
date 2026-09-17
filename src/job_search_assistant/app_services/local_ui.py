"""Application facade and composition for the local read/import UI (S2a only)."""

from dataclasses import asdict, dataclass
from pathlib import Path

from job_search_assistant.app_services.career import GetCareerProfileSnapshot, ImportResumeDocument
from job_search_assistant.app_services.career_review import CareerReviewService
from job_search_assistant.app_services.foundation import build_foundation
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

    def profile(self):
        profile_id = self.queries.profile_id()
        if profile_id is None:
            return None
        return asdict(GetCareerProfileSnapshot(self.career).execute(profile_id=profile_id))

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
