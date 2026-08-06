from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field


class ParseStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    succeeded = "succeeded"
    failed = "failed"


class DocumentResponse(BaseModel):
    id: UUID
    claim_id: UUID
    storage_key: str
    file_name: str
    mime_type: str
    file_size_bytes: int | None
    checksum_sha256: str | None
    parse_status: ParseStatus
    page_count: int | None
    retention_class: str
    uploaded_by: str | None
    uploaded_at: datetime
    parsed_at: datetime | None
    metadata: dict


class DocumentListResponse(BaseModel):
    total: int
    items: list[DocumentResponse]


class DocumentParseStatusUpdateRequest(BaseModel):
    parse_status: ParseStatus
    actor_id: str | None = Field(default=None, max_length=100)
    note: str | None = None


class DocumentDownloadUrlResponse(BaseModel):
    document_id: UUID
    storage_key: str
    download_url: str
    expires_in: int


class DocumentBulkDeleteRequest(BaseModel):
    document_ids: list[UUID] = Field(min_length=1, max_length=200)
    actor_id: str | None = Field(default=None, max_length=100)


class DocumentBulkDeleteResponse(BaseModel):
    claim_id: UUID
    requested: int
    deleted: int
    failed: int
    not_found: int
    deleted_document_ids: list[UUID]
    failed_document_ids: list[UUID]
    not_found_document_ids: list[UUID]


class DocumentMergeUploadResponse(BaseModel):
    document: DocumentResponse
    source_file_count: int
    accepted_file_count: int
    skipped_file_count: int
    accepted_files: list[str]
    skipped_files: list[str]
    merged_source_total_size_bytes: int
    merged_output_size_bytes: int
    merged_saved_size_bytes: int
    merge_compression_ratio: float
