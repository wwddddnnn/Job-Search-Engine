"""Domain value objects for provider-agnostic job discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence

from job_search_assistant.core.errors import InfrastructureError, ValidationError


NORMALIZER_VERSION = "jobspipe-v1"


class WorkArrangement(StrEnum):
    """Product-level work arrangement choices independent of any provider."""

    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"


class SearchTriggerType(StrEnum):
    """Origins of a search run; scheduled is reserved for a future phase."""

    MANUAL = "manual"
    SCHEDULED = "scheduled"


class SearchRunStatus(StrEnum):
    """Lifecycle states of a durable job search run."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIALLY_SUCCEEDED = "partially_succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class SearchConfig:
    """Canonical user search intent that intentionally hides provider filters."""

    name: str
    job_titles: tuple[str, ...]
    locations: tuple[str, ...] = ()
    country_codes: tuple[str, ...] = ()
    region_codes: tuple[str, ...] = ()
    work_arrangements: tuple[WorkArrangement, ...] = ()
    seniority_levels: tuple[str, ...] = ()
    posted_within_days: int | None = None
    limit: int = 25
    hard_constraints: Mapping[str, Any] = field(default_factory=dict)
    preferred_constraints: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _require_text(self.name, "name"))
        object.__setattr__(self, "job_titles", _normalize_text_values(self.job_titles, "job_titles"))
        if not self.job_titles:
            raise ValidationError("At least one job title is required.", details={"field": "job_titles"})
        object.__setattr__(self, "locations", _normalize_text_values(self.locations, "locations"))
        object.__setattr__(self, "country_codes", _normalize_country_codes(self.country_codes))
        object.__setattr__(self, "region_codes", _normalize_region_codes(self.region_codes))
        object.__setattr__(self, "work_arrangements", _normalize_work_arrangements(self.work_arrangements))
        object.__setattr__(self, "seniority_levels", _normalize_text_values(self.seniority_levels, "seniority_levels"))
        if self.posted_within_days is not None and self.posted_within_days <= 0:
            raise ValidationError(
                "posted_within_days must be a positive integer when provided.",
                details={"field": "posted_within_days"},
            )
        if not 1 <= self.limit <= 500:
            raise ValidationError("limit must be between 1 and 500.", details={"field": "limit"})
        object.__setattr__(self, "hard_constraints", dict(self.hard_constraints))
        object.__setattr__(self, "preferred_constraints", dict(self.preferred_constraints))

    def to_dict(self) -> dict[str, Any]:
        """Serialize the stable product-level configuration to JSON-compatible data."""
        return {
            "name": self.name,
            "job_titles": list(self.job_titles),
            "locations": list(self.locations),
            "country_codes": list(self.country_codes),
            "region_codes": list(self.region_codes),
            "work_arrangements": [value.value for value in self.work_arrangements],
            "seniority_levels": list(self.seniority_levels),
            "posted_within_days": self.posted_within_days,
            "limit": self.limit,
            "hard_constraints": dict(self.hard_constraints),
            "preferred_constraints": dict(self.preferred_constraints),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SearchConfig":
        """Deserialize and validate a configuration from stored or adapter data."""
        return cls(
            name=_require_payload_text(payload, "name"),
            job_titles=_as_text_sequence(payload.get("job_titles", ()), "job_titles"),
            locations=_as_text_sequence(payload.get("locations", ()), "locations"),
            country_codes=_as_text_sequence(payload.get("country_codes", ()), "country_codes"),
            region_codes=_as_text_sequence(payload.get("region_codes", ()), "region_codes"),
            work_arrangements=tuple(
                WorkArrangement(value) for value in _as_text_sequence(payload.get("work_arrangements", ()), "work_arrangements")
            ),
            seniority_levels=_as_text_sequence(payload.get("seniority_levels", ()), "seniority_levels"),
            posted_within_days=_as_optional_positive_int(payload.get("posted_within_days"), "posted_within_days"),
            limit=_as_int(payload.get("limit", 25), "limit"),
            hard_constraints=_as_mapping(payload.get("hard_constraints", {}), "hard_constraints"),
            preferred_constraints=_as_mapping(payload.get("preferred_constraints", {}), "preferred_constraints"),
        )


@dataclass(frozen=True, slots=True)
class NormalizedJob:
    """Provider-neutral normalized projection; the source JSON remains authoritative raw data."""

    external_id: str
    title: str
    normalized_title: str
    company_name: str | None
    company_domain: str | None
    location_text: str | None
    country_code: str | None
    is_remote: bool | None
    work_arrangement: str | None
    seniority: str | None
    description: str | None
    description_hash: str | None
    canonical_url: str | None
    source_url: str | None
    provider_posted_at_raw: str | None
    provider_posted_at: datetime | None
    provider_last_seen_at_raw: str | None
    provider_last_seen_at: datetime | None
    provider_verified_at_raw: str | None
    provider_verified_at: datetime | None
    salary_string: str | None
    salary_currency: str | None
    min_annual_salary: float | None
    max_annual_salary: float | None
    provider_sources: Sequence[Mapping[str, Any]]
    content_hash: str
    normalizer_version: str = NORMALIZER_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Serialize the normalized snapshot to stable JSON-compatible data."""
        return {
            "external_id": self.external_id,
            "title": self.title,
            "normalized_title": self.normalized_title,
            "company_name": self.company_name,
            "company_domain": self.company_domain,
            "location_text": self.location_text,
            "country_code": self.country_code,
            "is_remote": self.is_remote,
            "work_arrangement": self.work_arrangement,
            "seniority": self.seniority,
            "description": self.description,
            "description_hash": self.description_hash,
            "canonical_url": self.canonical_url,
            "source_url": self.source_url,
            "provider_posted_at_raw": self.provider_posted_at_raw,
            "provider_posted_at": _serialize_datetime(self.provider_posted_at),
            "provider_last_seen_at_raw": self.provider_last_seen_at_raw,
            "provider_last_seen_at": _serialize_datetime(self.provider_last_seen_at),
            "provider_verified_at_raw": self.provider_verified_at_raw,
            "provider_verified_at": _serialize_datetime(self.provider_verified_at),
            "salary_string": self.salary_string,
            "salary_currency": self.salary_currency,
            "min_annual_salary": self.min_annual_salary,
            "max_annual_salary": self.max_annual_salary,
            "provider_sources": [dict(source) for source in self.provider_sources],
            "content_hash": self.content_hash,
            "normalizer_version": self.normalizer_version,
        }


@dataclass(frozen=True, slots=True)
class ProviderSearchPage:
    """One provider response page with raw evidence and safe pagination metadata."""

    provider: str
    query: Mapping[str, Any]
    raw_response: Mapping[str, Any]
    jobs: Sequence[Mapping[str, Any]]
    metadata: Mapping[str, Any]
    next_cursor: str | None
    http_status: int


class ProviderError(InfrastructureError):
    """Normalizes remote provider/network errors without retaining credentials."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        retryable: bool,
        http_status: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        merged_details: dict[str, Any] = {
            "provider": provider,
            "retryable": retryable,
        }
        if http_status is not None:
            merged_details["http_status"] = http_status
        if details:
            merged_details.update(details)
        super().__init__(message, details=merged_details)
        self.code = "provider_error"


def content_hash(payload: Mapping[str, Any]) -> str:
    """Create a deterministic hash for persisted raw and normalized content."""
    try:
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Job payload must be JSON serializable.", details={"reason": str(exc)}) from exc
    return sha256(serialized.encode("utf-8")).hexdigest()


def _normalize_text_values(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    seen: set[str] = set()
    normalized_values: list[str] = []
    for value in values:
        normalized_value = _require_text(value, field_name)
        key = normalized_value.casefold()
        if key not in seen:
            seen.add(key)
            normalized_values.append(normalized_value)
    return tuple(normalized_values)


def _normalize_country_codes(values: Sequence[str]) -> tuple[str, ...]:
    normalized_values = _normalize_text_values(values, "country_codes")
    for value in normalized_values:
        if len(value) != 2 or not value.isalpha():
            raise ValidationError("country_codes must contain ISO alpha-2 codes.", details={"value": value})
    return tuple(value.upper() for value in normalized_values)


def _normalize_region_codes(values: Sequence[str]) -> tuple[str, ...]:
    normalized_values = _normalize_text_values(values, "region_codes")
    for value in normalized_values:
        if "-" not in value or len(value) < 4:
            raise ValidationError("region_codes must contain ISO 3166-2-style codes.", details={"value": value})
    return tuple(value.upper() for value in normalized_values)


def _normalize_work_arrangements(values: Sequence[WorkArrangement]) -> tuple[WorkArrangement, ...]:
    normalized_values: list[WorkArrangement] = []
    for value in values:
        arrangement = value if isinstance(value, WorkArrangement) else WorkArrangement(str(value))
        if arrangement not in normalized_values:
            normalized_values.append(arrangement)
    return tuple(normalized_values)


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} must be a non-blank string.", details={"field": field_name})
    return " ".join(value.split())


def _require_payload_text(payload: Mapping[str, Any], field_name: str) -> str:
    return _require_text(payload.get(field_name), field_name)


def _as_text_sequence(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValidationError(f"{field_name} must be an array of strings.", details={"field": field_name})
    return tuple(str(item) for item in value)


def _as_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field_name} must be an object.", details={"field": field_name})
    return value


def _as_optional_positive_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    return _as_int(value, field_name)


def _as_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field_name} must be an integer.", details={"field": field_name})
    return value


def _serialize_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
