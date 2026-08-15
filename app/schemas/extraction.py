from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class ExtractionProvider(str, Enum):
    auto = "auto"
    local = "local"
    openai = "openai"
    aws_textract = "aws_textract"


class RunExtractionRequest(BaseModel):
    provider: ExtractionProvider = ExtractionProvider.auto
    actor_id: Optional[str] = Field(default=None, max_length=100)
    force_refresh: bool = False


class ExtractionResponse(BaseModel):
    id: UUID
    claim_id: UUID
    document_id: UUID
    extraction_version: str
    model_name: str
    extracted_entities: dict
    evidence_refs: list[dict]
    confidence: Optional[float]
    raw_response: Optional[dict] = None
    created_by: str
    created_at: datetime


class ExtractionListResponse(BaseModel):
    total: int
    items: list[ExtractionResponse]


class ExtractionJobStatus(str, Enum):
    queued = "queued"
    processing = "processing"
    succeeded = "succeeded"
    failed = "failed"


class ExtractionJobResponse(BaseModel):
    job_id: UUID
    document_id: UUID
    claim_id: UUID
    provider: ExtractionProvider
    status: ExtractionJobStatus
    queued_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error_message: Optional[str] = None
    result_extraction_id: Optional[UUID] = None

    @property
    def id(self) -> UUID:
        return self.job_id


class ExtractionJobListItem(BaseModel):
    id: UUID
    claim_id: UUID
    document_id: UUID
    status: str
    queued_at: Optional[datetime] = None
    provider: str


class ExtractionJobListResponse(BaseModel):
    total: int
    items: list[ExtractionJobListItem]
