"""Provider port and JobsPipe adapter for canonical job discovery."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from job_search_assistant.core.errors import ValidationError
from job_search_assistant.discovery.types import ProviderError, ProviderSearchPage, SearchConfig, WorkArrangement


JOBSPIPE_BASE_URL = "https://api.jobspipe.dev"
JOBSPIPE_SEARCH_PATH = "/v1/jobs/search"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Minimal HTTP response shape required by a provider adapter."""

    status: int
    body: bytes


class JsonHttpClient(Protocol):
    """Outbound HTTP port; fake implementations keep tests network-independent."""

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> HttpResponse:
        """POST a JSON object and return a raw response."""


class JobSearchProvider(Protocol):
    """Canonical provider port independent of any external job API."""

    @property
    def name(self) -> str:
        """Return the stable provider identifier persisted in source records."""

    def search(self, config: SearchConfig, *, cursor: str | None = None) -> ProviderSearchPage:
        """Return one durable provider result page for a canonical configuration."""


class UrllibJsonHttpClient:
    """Standard-library HTTP client; adapter errors are translated by the provider."""

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> HttpResponse:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = Request(url=url, data=body, headers=dict(headers), method="POST")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - URL is provider configuration.
                return HttpResponse(status=int(response.status), body=response.read())
        except HTTPError as exc:
            return HttpResponse(status=int(exc.code), body=exc.read())
        except URLError as exc:
            raise ProviderError(
                "JobsPipe could not be reached.",
                provider="jobspipe",
                retryable=True,
                details={"reason": str(exc.reason)},
            ) from exc
        except OSError as exc:
            raise ProviderError(
                "JobsPipe request failed before a response was received.",
                provider="jobspipe",
                retryable=True,
                details={"reason": str(exc)},
            ) from exc


class JobsPipeProvider:
    """JobsPipe implementation of the provider-agnostic job search port."""

    name = "jobspipe"

    def __init__(
        self,
        *,
        api_key: str,
        http_client: JsonHttpClient | None = None,
        base_url: str = JOBSPIPE_BASE_URL,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_key.strip():
            raise ValidationError("JobsPipe API key must not be blank.", details={"field": "api_key"})
        if timeout_seconds <= 0:
            raise ValidationError("timeout_seconds must be positive.", details={"field": "timeout_seconds"})
        self._api_key = api_key.strip()
        self._http_client = http_client or UrllibJsonHttpClient()
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(
        cls,
        *,
        environment: Mapping[str, str] | None = None,
        http_client: JsonHttpClient | None = None,
    ) -> "JobsPipeProvider":
        """Create a provider from a local environment without persisting its secret."""
        values = environment if environment is not None else os.environ
        api_key = values.get("JOBSPIPE_API_KEY", "")
        if not api_key.strip():
            raise ValidationError(
                "JobsPipe is not configured. Set JOBSPIPE_API_KEY before running a live search.",
                details={"environment_variable": "JOBSPIPE_API_KEY"},
            )
        return cls(
            api_key=api_key,
            http_client=http_client,
            base_url=values.get("JOBSPIPE_BASE_URL", JOBSPIPE_BASE_URL),
        )

    def search(self, config: SearchConfig, *, cursor: str | None = None) -> ProviderSearchPage:
        """Request one JobsPipe result page and validate its documented envelope."""
        query = self.build_query(config, cursor=cursor)
        response = self._http_client.post_json(
            url=f"{self._base_url}{JOBSPIPE_SEARCH_PATH}",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            payload=query,
            timeout_seconds=self._timeout_seconds,
        )
        response_payload = _decode_json(response.body)
        if response.status != 200:
            raise ProviderError(
                "JobsPipe returned an unsuccessful search response.",
                provider=self.name,
                retryable=response.status in {429, 500, 502, 503, 504},
                http_status=response.status,
                details={"response": _safe_error_summary(response_payload)},
            )
        if not isinstance(response_payload, Mapping):
            raise ProviderError(
                "JobsPipe response must be a JSON object.",
                provider=self.name,
                retryable=False,
                http_status=response.status,
            )
        data = response_payload.get("data")
        metadata = response_payload.get("metadata", {})
        if not isinstance(data, list) or not all(isinstance(item, Mapping) for item in data):
            raise ProviderError(
                "JobsPipe response data must be an array of job objects.",
                provider=self.name,
                retryable=False,
                http_status=response.status,
            )
        if not isinstance(metadata, Mapping):
            raise ProviderError(
                "JobsPipe response metadata must be an object.",
                provider=self.name,
                retryable=False,
                http_status=response.status,
            )
        next_cursor = metadata.get("next_cursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise ProviderError(
                "JobsPipe metadata.next_cursor must be a string or null.",
                provider=self.name,
                retryable=False,
                http_status=response.status,
            )
        return ProviderSearchPage(
            provider=self.name,
            query=query,
            raw_response=response_payload,
            jobs=tuple(dict(item) for item in data),
            metadata=dict(metadata),
            next_cursor=next_cursor,
            http_status=response.status,
        )

    @staticmethod
    def build_query(config: SearchConfig, *, cursor: str | None = None) -> dict[str, Any]:
        """Translate product-level search intent to the supported JobsPipe filter subset."""
        query: dict[str, Any] = {
            "job_title_or": list(config.job_titles),
            "limit": config.limit,
            "include_total_results": True,
        }
        _add_nonempty(query, "job_location_or", config.locations)
        _add_nonempty(query, "job_country_code_or", config.country_codes)
        _add_nonempty(query, "region_or", config.region_codes)
        _add_nonempty(query, "job_seniority_or", config.seniority_levels)
        if config.work_arrangements:
            query["work_arrangement_or"] = [arrangement.value for arrangement in config.work_arrangements]
            if config.work_arrangements == (WorkArrangement.REMOTE,):
                query["remote"] = True
        if config.posted_within_days is not None:
            query["posted_at_max_age_days"] = config.posted_within_days
        if cursor is not None:
            normalized_cursor = cursor.strip()
            if not normalized_cursor:
                raise ValidationError("cursor must not be blank when supplied.", details={"field": "cursor"})
            query["cursor"] = normalized_cursor
        return query


def _add_nonempty(query: dict[str, Any], key: str, values: tuple[str, ...]) -> None:
    if values:
        query[key] = list(values)


def _decode_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError(
            "JobsPipe returned an invalid JSON response.",
            provider="jobspipe",
            retryable=False,
            details={"response_bytes": len(body)},
        ) from exc


def _safe_error_summary(payload: Any) -> Mapping[str, Any]:
    """Keep remote error diagnostics useful without echoing an entire untrusted payload."""
    if isinstance(payload, Mapping):
        summary: dict[str, Any] = {}
        for key in ("error", "message", "code", "detail"):
            value = payload.get(key)
            if isinstance(value, str):
                summary[key] = value[:500]
            elif isinstance(value, int | float | bool):
                summary[key] = value
        return summary
    return {"response_type": type(payload).__name__}
