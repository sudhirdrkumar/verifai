from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional
from enum import Enum


class ReportStatus(str, Enum):
    pending = "pending"
    generated = "generated"
    failed = "failed"


class ReportFormat(str, Enum):
    html = "html"
    pdf = "pdf"
    json = "json"


class MedicalReportBase(BaseModel):
    claim_id: str = Field(..., description="Associated claim ID")
    hospital_name: Optional[str] = Field(None, description="Hospital name")
    treating_doctor: Optional[str] = Field(None, description="Treating doctor name")
    diagnosis: Optional[str] = Field(None, description="Medical diagnosis")
    complaints: Optional[str] = Field(None, description="Patient complaints")
    medicine_used: Optional[str] = Field(None, description="Medicines prescribed")
    claim_amount: Optional[str] = Field(None, description="Claim amount")
    conclusion: Optional[str] = Field(None, description="AI conclusion")
    ml_summary: Optional[str] = Field(None, description="Machine learning summary")


class MedicalReportResponse(MedicalReportBase):
    id: str
    claim_id: str
    status: ReportStatus
    report_html: Optional[str] = Field(None, description="Generated HTML report")
    report_text: Optional[str] = Field(None, description="Generated text report")
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ReportGenerationRequest(BaseModel):
    claim_id: str = Field(..., description="Claim ID to generate report for")
    format: ReportFormat = Field(default=ReportFormat.html, description="Report format")
