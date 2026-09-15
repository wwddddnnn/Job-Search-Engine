"""Job Discovery domain.

This module owns canonical search intent, durable search runs, provider source
identity, raw provider evidence, normalized job snapshots, and deduplication.
"""

from job_search_assistant.discovery.normalizer import JobsPipeJobNormalizer, canonicalize_url, parse_provider_datetime
from job_search_assistant.discovery.provider import (
    JOBSPIPE_BASE_URL,
    HttpResponse,
    JobSearchProvider,
    JobsPipeProvider,
    JsonHttpClient,
    UrllibJsonHttpClient,
)
from job_search_assistant.discovery.store import (
    DiscoveryStore,
    NormalizationOutcome,
    RunClaim,
    RunResumeState,
    SearchRun,
    SearchRunReservation,
    StoredSearchConfig,
)
from job_search_assistant.discovery.types import (
    NORMALIZER_VERSION,
    NormalizedJob,
    ProviderError,
    ProviderSearchPage,
    SearchConfig,
    SearchRunStatus,
    SearchTriggerType,
    WorkArrangement,
)

__all__ = [
    "JOBSPIPE_BASE_URL",
    "NORMALIZER_VERSION",
    "HttpResponse",
    "JobSearchProvider",
    "JobsPipeJobNormalizer",
    "JobsPipeProvider",
    "JsonHttpClient",
    "DiscoveryStore",
    "NormalizationOutcome",
    "NormalizedJob",
    "ProviderError",
    "ProviderSearchPage",
    "SearchConfig",
    "SearchRun",
    "SearchRunReservation",
    "SearchRunStatus",
    "SearchTriggerType",
    "RunClaim",
    "RunResumeState",
    "StoredSearchConfig",
    "UrllibJsonHttpClient",
    "WorkArrangement",
    "canonicalize_url",
    "parse_provider_datetime",
]
