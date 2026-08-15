from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field


class ExtractionProvider(str, Enum):
    auto = "auto"
    local = "local"
    openai = "openai"
    aws_textract = "aws_textract"


class RunExtractionRequest(BaseModel):
    provider: ExtractionProvider = ExtractionProvider.auto
    actor_id: str | None = Field(default=None, max_length=100)
    force_refresh: bool = False


class ExtractionJobStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class ExtractionJobResponse(BaseModel):
    job_id: UUID
    document_id: UUID
    claim_id: UUID
    provider: ExtractionProvider
    status: ExtractionJobStatus
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    message: str | None = None
    error_message: str | None = None
    result_extraction_id: UUID | None = None


class ExtractionJobListItem(BaseModel):
    job_id: UUID
    document_id: UUID
    claim_id: UUID
    external_claim_id: str | None = None
    file_name: str | None = None
    provider: ExtractionProvider
    status: ExtractionJobStatus
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None
    result_extraction_id: UUID | None = None


class ExtractionJobListResponse(BaseModel):
    total: int
    items: list[ExtractionJobListItem]


class ExtractionResponse(BaseModel):
    id: UUID
    claim_id: UUID
    document_id: UUID
    extraction_version: str
    model_name: str
    extracted_entities: dict
    evidence_refs: list[dict]
    confidence: float | None
    raw_response: dict | None = None
    created_by: str
    created_at: datetime


class ExtractionListResponse(BaseModel):
    total: int
    items: list[ExtractionResponse]
