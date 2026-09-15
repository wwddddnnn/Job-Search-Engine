"""Conservative normalization of JobsPipe job payloads.

The normalizer creates a queryable projection. It never replaces the complete
provider object, which is retained separately as a raw payload.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from job_search_assistant.core.errors import ValidationError
from job_search_assistant.discovery.types import NORMALIZER_VERSION, NormalizedJob


_TRACKING_QUERY_KEYS = {"gclid", "fbclid", "mc_cid", "mc_eid", "ref", "referrer", "source"}


class JobsPipeJobNormalizer:
    """Build a normalized job projection from a single JobsPipe data item."""

    version = NORMALIZER_VERSION

    def normalize(self, raw_job: Mapping[str, Any]) -> NormalizedJob:
        """Validate core identity fields and preserve optional provider fields safely."""
        external_id = _require_external_id(raw_job.get("id"))
        title = _optional_text(raw_job.get("job_title"))
        if title is None:
            raise ValidationError(
                "JobsPipe job payload is missing job_title.",
                details={"provider": "jobspipe", "external_id": external_id, "field": "job_title"},
            )
        normalized_title = _optional_text(raw_job.get("normalized_title")) or _normalize_title(title)
        company_object = raw_job.get("company_object")
        company_mapping = company_object if isinstance(company_object, Mapping) else {}
        company_name = _optional_text(raw_job.get("company")) or _optional_text(company_mapping.get("name"))
        source_url = _optional_url(raw_job.get("source_url"))
        canonical_url = canonicalize_url(raw_job.get("url")) or canonicalize_url(source_url)
        description = _optional_text(raw_job.get("description"))
        provider_sources = _mapping_sequence(raw_job.get("sources"))
        work_arrangement = _work_arrangement(raw_job)
        raw_snapshot = _raw_snapshot_for_hash(raw_job)

        return NormalizedJob(
            external_id=external_id,
            title=title,
            normalized_title=normalized_title,
            company_name=company_name,
            company_domain=_company_domain(raw_job, company_mapping, canonical_url, source_url),
            location_text=_optional_text(raw_job.get("location")),
            country_code=_country_code(raw_job.get("country_code")),
            is_remote=_optional_bool(raw_job.get("remote")),
            work_arrangement=work_arrangement,
            seniority=_optional_text(raw_job.get("seniority")),
            description=description,
            description_hash=_text_hash(description),
            canonical_url=canonical_url,
            source_url=source_url,
            provider_posted_at_raw=_optional_text(raw_job.get("date_posted")),
            provider_posted_at=parse_provider_datetime(raw_job.get("date_posted")),
            provider_last_seen_at_raw=_optional_text(raw_job.get("last_seen_at")),
            provider_last_seen_at=parse_provider_datetime(raw_job.get("last_seen_at")),
            provider_verified_at_raw=_optional_text(raw_job.get("verified_at")),
            provider_verified_at=parse_provider_datetime(raw_job.get("verified_at")),
            salary_string=_optional_text(raw_job.get("salary_string")),
            salary_currency=_currency_code(raw_job.get("salary_currency")),
            min_annual_salary=_optional_number(raw_job.get("min_annual_salary")),
            max_annual_salary=_optional_number(raw_job.get("max_annual_salary")),
            provider_sources=provider_sources,
            content_hash=_mapping_hash(raw_snapshot),
            normalizer_version=self.version,
        )


def canonicalize_url(value: object) -> str | None:
    """Remove fragments and common tracking params while preserving a usable source URL."""
    raw_url = _optional_url(value)
    if raw_url is None:
        return None
    parts = urlsplit(raw_url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return raw_url
    retained_query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_QUERY_KEYS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            path,
            urlencode(retained_query, doseq=True),
            "",
        )
    )


def parse_provider_datetime(value: object) -> datetime | None:
    """Parse unambiguous ISO/common provider timestamps while retaining the raw string separately."""
    raw_value = _optional_text(value)
    if raw_value is None:
        return None
    candidates = [raw_value, raw_value.replace("Z", "+00:00")]
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        except ValueError:
            continue
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw_value, pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _raw_snapshot_for_hash(raw_job: Mapping[str, Any]) -> dict[str, Any]:
    """Hash job content, not provider observation timestamps.

    ``last_seen_at`` and ``verified_at`` belong to a source's observation
    timeline.  Keeping them out of the snapshot hash means a repeated sighting
    updates ``job_sources.last_seen_at`` without manufacturing a new immutable
    job snapshot.
    """
    observation_keys = {"last_seen_at", "verified_at"}
    return {str(key): value for key, value in raw_job.items() if str(key) not in observation_keys}


def _mapping_hash(value: Mapping[str, Any]) -> str:
    try:
        serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError("JobsPipe job payload must be JSON serializable.", details={"reason": str(exc)}) from exc
    return sha256(serialized.encode("utf-8")).hexdigest()


def _text_hash(value: str | None) -> str | None:
    return sha256(value.encode("utf-8")).hexdigest() if value is not None else None


def _require_external_id(value: object) -> str:
    if isinstance(value, bool) or value is None:
        raise ValidationError(
            "JobsPipe job payload is missing id.",
            details={"provider": "jobspipe", "field": "id"},
        )
    identifier = str(value).strip()
    if not identifier:
        raise ValidationError(
            "JobsPipe job payload has a blank id.",
            details={"provider": "jobspipe", "field": "id"},
        )
    return identifier


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized_value = " ".join(value.split())
    return normalized_value or None


def _optional_url(value: object) -> str | None:
    return _optional_text(value)


def _normalize_title(value: str) -> str:
    return " ".join(value.casefold().split())


def _country_code(value: object) -> str | None:
    normalized_value = _optional_text(value)
    if normalized_value is None:
        return None
    return normalized_value.upper() if len(normalized_value) == 2 and normalized_value.isalpha() else normalized_value


def _currency_code(value: object) -> str | None:
    normalized_value = _optional_text(value)
    if normalized_value is None:
        return None
    return normalized_value.upper() if len(normalized_value) == 3 and normalized_value.isalpha() else normalized_value


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.casefold() == "true":
            return True
        if value.casefold() == "false":
            return False
    return None


def _optional_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _mapping_sequence(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _work_arrangement(raw_job: Mapping[str, Any]) -> str | None:
    direct_value = _optional_text(raw_job.get("work_arrangement"))
    if direct_value is not None:
        return direct_value.casefold()
    if _optional_bool(raw_job.get("remote")) is True:
        return "remote"
    return None


def _company_domain(
    raw_job: Mapping[str, Any],
    company_object: Mapping[str, Any],
    canonical_url: str | None,
    source_url: str | None,
) -> str | None:
    for value in (
        raw_job.get("company_domain"),
        company_object.get("domain"),
        company_object.get("website"),
    ):
        text_value = _optional_text(value)
        if text_value is None:
            continue
        parts = urlsplit(text_value if "://" in text_value else f"https://{text_value}")
        if parts.netloc:
            return parts.netloc.casefold()
    for url in (canonical_url, source_url):
        if url is None:
            continue
        hostname = urlsplit(url).hostname
        if hostname is not None:
            return hostname.casefold()
    return None
