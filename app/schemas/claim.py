from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field


class ClaimStatus(str, Enum):
    ready_for_assignment = "ready_for_assignment"
    waiting_for_documents = "waiting_for_documents"
    in_review = "in_review"
    needs_qc = "needs_qc"
    completed = "completed"
    withdrawn = "withdrawn"


class CreateClaimRequest(BaseModel):
    external_claim_id: str = Field(min_length=1, max_length=100)
    patient_name: str | None = Field(default=None, max_length=255)
    patient_identifier: str | None = Field(default=None, max_length=100)
    status: ClaimStatus = ClaimStatus.waiting_for_documents
    assigned_doctor_id: str | None = Field(default=None, max_length=100)
    priority: int = Field(default=3, ge=1, le=5)
    source_channel: str | None = Field(default=None, max_length=100)
    tags: list[str] = Field(default_factory=list)


class ClaimStatusUpdateRequest(BaseModel):
    status: ClaimStatus
    actor_id: str | None = Field(default=None, max_length=100)
    note: str | None = None


class ClaimAssignmentRequest(BaseModel):
    assigned_doctor_id: str = Field(min_length=1, max_length=100)
    actor_id: str | None = Field(default=None, max_length=100)
    status: ClaimStatus | None = None


class ClaimResponse(BaseModel):
    id: UUID
    external_claim_id: str
    patient_name: str | None
    patient_identifier: str | None
    status: ClaimStatus
    assigned_doctor_id: str | None
    priority: int
    source_channel: str | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime


class ClaimListResponse(BaseModel):
    total: int
    items: list[ClaimResponse]


class ClaimReportSaveRequest(BaseModel):
    report_html: str = Field(min_length=1)
    report_status: str = Field(default="draft", max_length=30)
    actor_id: str | None = Field(default=None, max_length=100)
    report_source: str = Field(default="doctor", pattern="^(doctor|system)$")


class ClaimReportSaveResponse(BaseModel):
    id: UUID
    claim_id: UUID
    decision_id: UUID | None = None
    version_no: int
    report_status: str
    report_source: str
    created_by: str
    created_at: datetime
    html_size: int



class ClaimReportGrammarCheckRequest(BaseModel):
    report_html: str = Field(min_length=1)
    actor_id: str | None = Field(default=None, max_length=100)


class ClaimReportGrammarCheckResponse(BaseModel):
    corrected_html: str
    changed: bool
    checked_segments: int
    corrected_segments: int
    model: str | None = None
    notes: str | None = None
class ClaimStructuredDataRequest(BaseModel):
    use_llm: bool = True
    force_refresh: bool = True
    actor_id: str | None = Field(default=None, max_length=100)


class ClaimStructuredDataResponse(BaseModel):
    claim_id: UUID
    external_claim_id: str
    company_name: str
    claim_type: str
    insured_name: str
    hospital_name: str
    treating_doctor: str
    treating_doctor_registration_number: str
    doa: str
    dod: str
    diagnosis: str
    complaints: str
    findings: str
    investigation_finding_in_details: str
    medicine_used: str
    high_end_antibiotic_for_rejection: str
    deranged_investigation: str
    claim_amount: str
    conclusion: str
    recommendation: str
    raw_payload: dict = Field(default_factory=dict)
    source: str
    confidence: float | None = None
    created_at: datetime
    updated_at: datetime



class ClaimConclusionGenerateRequest(BaseModel):
    report_html: str = Field(min_length=1)
    actor_id: str | None = Field(default=None, max_length=100)
    rerun_rules: bool = True
    force_source_refresh: bool = False
    use_ai: bool = True


class ClaimConclusionGenerateResponse(BaseModel):
    claim_id: UUID
    conclusion: str
    recommendation: str | None = None
    triggered_rules_count: int = 0
    source: str = "rule_engine"

