"""Strict public DTOs for the operational HTTP boundary."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubmitRequest(APIModel):
    imdb_id: str | None = None
    query: str | None = None

    @model_validator(mode="after")
    def exactly_one_source(self) -> SubmitRequest:
        imdb = (self.imdb_id or "").strip()
        query = (self.query or "").strip()
        if bool(imdb) == bool(query):
            raise ValueError("Provide exactly one nonblank imdb_id or query")
        return self


class ActionRequest(APIModel):
    reconciliation: str | None = None
    remote_id: str | None = None


class ErrorBody(APIModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str


class ErrorEnvelope(APIModel):
    error: ErrorBody


class HealthResponse(APIModel):
    status: str
    dispatcher_ready: bool


class SummaryResponse(APIModel):
    total: int
    states: dict[str, int]


class JobErrorResponse(APIModel):
    code: str | None
    message: str | None
    retryable: bool


class JobResponse(APIModel):
    id: str
    source_imdb_id: str | None
    query: str
    label: str
    state: str
    current_stage: str | None
    next_action: str | None
    safe_error: JobErrorResponse | None
    artifact_summary: dict[str, JsonValue]
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    cancel_requested: bool


class JobPageResponse(APIModel):
    items: list[JobResponse]
    total: int
    limit: int
    offset: int


class RecordErrorResponse(APIModel):
    code: str | None
    message: str | None


class ProgressResponse(APIModel):
    numerator: int | None
    denominator: int | None
    unit: str | None


class StageResponse(APIModel):
    id: int
    job_id: str
    name: str
    parent_stage_id: int | None
    ordinal: int
    state: str
    retry_cycle: int
    max_auto_attempts: int
    progress: ProgressResponse
    started_at: str | None
    finished_at: str | None
    updated_at: str
    warnings: list[str]
    output_manifest: dict[str, JsonValue]
    safe_error: RecordErrorResponse | None
    retryable: bool
    next_action: str | None


class AttemptResponse(APIModel):
    id: int
    job_id: str
    stage_id: int
    candidate_id: str | None
    retry_cycle: int
    attempt_number: int
    max_attempts: int
    trigger: str
    started_at: str
    finished_at: str | None
    outcome: str
    retryable: bool
    output: dict[str, JsonValue]


class CandidateResponse(APIModel):
    id: str
    job_id: str
    provider: str
    provider_id: str
    provider_filename: str | None
    source_type: str
    language: str | None
    fps: float | None
    title: str | None
    year: int | None
    imdb_match: bool | None
    provider_rating: float | None
    provider_download_count: int | None
    discovery_cycle: int
    rank: int | None
    detected_encoding: str | None
    cue_count: int | None
    first_cue_seconds: float | None
    final_cue_seconds: float | None
    parsed_duration_seconds: float | None
    expected_runtime_seconds: float | None
    coverage_percent: float | None
    download_error: str | None
    parse_error: str | None
    status: str
    selected_at: str | None
    selection_method: str | None
    created_at: str
    updated_at: str
    rank_reasons: list[str]
    quality_reasons: list[str]
    rejection_reasons: list[str]
    artifact_available: bool


class DecisionResponse(APIModel):
    id: int
    job_id: str
    action: str
    target_stage: str | None
    candidate_id: str | None
    platform: str | None
    accepted: bool
    reason: str
    created_at: str


class EventResponse(APIModel):
    id: int
    job_id: str
    stage_id: int | None
    attempt_id: int | None
    severity: str
    type: str
    message: str
    data: dict[str, JsonValue]
    created_at: str


class PublishingAttemptResponse(APIModel):
    id: int
    job_id: str
    platform: str
    retry_cycle: int
    attempt_number: int
    max_attempts: int
    trigger: str
    started_at: str
    finished_at: str | None
    outcome: str
    retryable: bool
    safe_error: RecordErrorResponse | None
    remote_id: str | None
    metadata: dict[str, JsonValue]


class CostResponse(APIModel):
    id: int
    job_id: str
    category: str
    provider: str
    amount_usd: float
    units: int
    detail: dict[str, JsonValue]
    created_at: str


class ReleaseResponse(APIModel):
    id: int
    job_id: str
    platform: str
    remote_id: str | None
    status: str
    uploaded_at: str | None
    safe_error: RecordErrorResponse | None
    metadata: dict[str, JsonValue]
    updated_at: str


class RevenueResponse(APIModel):
    id: int
    job_id: str
    platform: str
    date: str
    views: int
    revenue_usd: float
    likes: int
    comments: int
    shares: int
    fetched_at: str


class DetailResponse(APIModel):
    run: JobResponse
    stages: list[StageResponse]
    attempts: list[AttemptResponse]
    candidates: list[CandidateResponse]
    events: list[EventResponse]
    decisions: list[DecisionResponse]
    publishing_attempts: list[PublishingAttemptResponse]
    costs: list[CostResponse]
    releases: list[ReleaseResponse]
    revenue: list[RevenueResponse]
    server_time: str
    last_event_id: int
    available_actions: list[str]


class EventPageResponse(APIModel):
    items: list[EventResponse]
    last_event_id: int


class ActionResponse(APIModel):
    run: JobResponse
    decision: DecisionResponse
    changed: bool


class UploadResponse(APIModel):
    candidate: CandidateResponse
    decision: DecisionResponse
