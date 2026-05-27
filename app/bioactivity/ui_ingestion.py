from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from herg.config import DbConfig, HttpConfig, RunConfig
from herg.db import get_conn
from herg.normalize import clean_text
from herg.pipeline import IngestionStats, SourceAdapter, run_pipeline

from .endpoints import EndpointConfig, get_source_config, load_endpoint
from .source_adapters import build_source_adapter, normalize_source_name


AdapterBuilder = Callable[..., SourceAdapter]
PipelineRunner = Callable[..., IngestionStats]


@dataclass(frozen=True)
class UiIngestionRequest:
    endpoint_key: str
    source_name: str
    dry_run: bool = True
    max_records: int | None = 100
    commit_every: int = 500
    fail_fast: bool = False
    request_timeout_seconds: int = 45
    http_retries: int = 4


@dataclass(frozen=True)
class UiIngestionResult:
    endpoint_key: str
    source_name: str
    dry_run: bool
    processed: int
    stored: int
    updated: int
    skipped_invalid: int
    failed: int
    warnings: int
    duration_seconds: float
    ingestion_run_id: int | None = None


def _validate_request(request: UiIngestionRequest) -> tuple[str, str]:
    endpoint_key = clean_text(request.endpoint_key)
    if not endpoint_key:
        raise ValueError("endpoint_key is required.")
    source_name = normalize_source_name(request.source_name)
    if request.max_records is not None and request.max_records <= 0:
        raise ValueError("max_records must be > 0 when provided.")
    if request.commit_every <= 0:
        raise ValueError("commit_every must be > 0.")
    if request.request_timeout_seconds <= 0:
        raise ValueError("request_timeout_seconds must be > 0.")
    if request.http_retries < 0:
        raise ValueError("http_retries must be >= 0.")
    return endpoint_key, source_name


def run_ui_ingestion(
    request: UiIngestionRequest,
    *,
    db_config: DbConfig | None = None,
    adapter_builder: AdapterBuilder = build_source_adapter,
    pipeline_runner: PipelineRunner = run_pipeline,
) -> UiIngestionResult:
    endpoint_key, source_name = _validate_request(request)
    resolved_db_config = db_config or DbConfig.from_env()
    http_config = HttpConfig(
        request_timeout_seconds=request.request_timeout_seconds,
        http_retries=request.http_retries,
    )
    run_config = RunConfig(
        dry_run=request.dry_run,
        max_records=request.max_records,
        commit_every=request.commit_every,
        fail_fast=request.fail_fast,
    )

    with get_conn(db_config=resolved_db_config) as conn:
        endpoint: EndpointConfig = load_endpoint(conn, endpoint_key)

    source_config = get_source_config(endpoint, source_name)
    adapter = adapter_builder(
        endpoint=endpoint,
        source_name=source_name,
        source_config=source_config,
        http_config=http_config,
    )
    stats = pipeline_runner(
        adapter,
        resolved_db_config,
        run_config,
        endpoint_key=endpoint.endpoint_key,
    )

    return UiIngestionResult(
        endpoint_key=endpoint.endpoint_key,
        source_name=source_name,
        dry_run=request.dry_run,
        processed=stats.processed,
        stored=stats.stored,
        updated=stats.updated,
        skipped_invalid=stats.skipped_invalid,
        failed=stats.failed,
        warnings=stats.warnings,
        duration_seconds=stats.duration_seconds,
        ingestion_run_id=getattr(stats, "ingestion_run_id", None),
    )
