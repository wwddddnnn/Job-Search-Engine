"""Local deterministic Career extraction adapter used until a real provider is configured."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from job_search_assistant.career.types import ExtractionRun
from job_search_assistant.core.errors import ValidationError


class DeterministicCareerExtractionProvider:
    """Return an injected draft fixture without network access or hidden fallback.

    A caller must explicitly configure ``payload``.  Leaving it unset models
    the production default before a real LLM adapter exists and raises a
    structured configuration validation error instead of pretending extraction
    succeeded with an empty result.
    """

    model = "deterministic-career-extraction-v1"

    def __init__(self, *, payload: Mapping[str, Any] | None = None) -> None:
        if payload is not None and not isinstance(payload, Mapping):
            raise ValidationError(
                "Deterministic extraction payload must be an object.",
                details={"field": "payload"},
            )
        self._payload = None if payload is None else deepcopy(dict(payload))

    def extract(self, *, extracted_text: str, run: ExtractionRun) -> Mapping[str, Any]:
        """Return a fresh deterministic payload for a durable extraction run."""
        if not isinstance(extracted_text, str) or not extracted_text.strip():
            raise ValidationError(
                "Extracted text must be non-blank.",
                details={"field": "extracted_text"},
            )
        if self._payload is None:
            raise ValidationError(
                "Career extraction provider is not configured.",
                details={"provider": "career_extraction"},
            )
        # Touch the immutable run contract so an accidental non-run caller does
        # not receive a fixture that could be mistaken for a real extraction.
        if not isinstance(run, ExtractionRun):
            raise ValidationError("run must be an ExtractionRun.", details={"field": "run"})
        return deepcopy(self._payload)
