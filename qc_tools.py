from pydantic import BaseModel, Field


class ClaimRuleUpsertRequest(BaseModel):
    rule_id: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    scope: list[str] = Field(default_factory=list)
    conditions: str | None = None
    decision: str = Field(default="QUERY", max_length=20)
    remark_template: str | None = None
    required_evidence: list[str] = Field(default_factory=list)
    severity: str = Field(default="SOFT_QUERY", max_length=30)
    priority: int = Field(default=999, ge=1, le=9999)
    is_active: bool = True
    version: str = Field(default="1.0", max_length=20)


class DiagnosisCriteriaUpsertRequest(BaseModel):
    criteria_id: str = Field(min_length=1, max_length=32)
    diagnosis_name: str = Field(min_length=1, max_length=255)
    diagnosis_key: str | None = Field(default=None, max_length=160)
    aliases: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    decision: str = Field(default="QUERY", max_length=20)
    remark_template: str | None = None
    severity: str = Field(default="SOFT_QUERY", max_length=30)
    priority: int = Field(default=999, ge=1, le=9999)
    is_active: bool = True
    version: str = Field(default="1.0", max_length=20)


class SuggestionReviewRequest(BaseModel):
    status: str = Field(pattern="^(approved|rejected)$")
    approved_rule_id: str | None = Field(default=None, max_length=32)


class MedicineUpsertRequest(BaseModel):
    medicine_name: str = Field(min_length=1, max_length=200)
    components: str = Field(min_length=1)
    subclassification: str = Field(default="Supportive care", max_length=80)
    is_high_end_antibiotic: bool = False


class ExcelImportResponse(BaseModel):
    total_rows: int
    inserted: int
    updated: int
    skipped: int


class ResetUserPasswordRequest(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    role: str | None = Field(default=None, max_length=30)
    new_password: str = Field(min_length=8, max_length=200)


class CompletedReportUploadStatusRequest(BaseModel):
    report_export_status: str = Field(default="uploaded", max_length=30)
    tagging: str = Field(min_length=1, max_length=120)
    subtagging: str = Field(min_length=1, max_length=120)
    opinion: str = Field(min_length=1, max_length=4000)


class CompletedReportUploadStatusResponse(BaseModel):
    claim_id: str
    external_claim_id: str
    report_export_status: str
    tagging: str
    subtagging: str
    opinion: str
    updated_at: str



class CompletedReportQcStatusRequest(BaseModel):
    qc_status: str = Field(pattern="^(yes|no)$")


class CompletedReportQcStatusResponse(BaseModel):
    claim_id: str
    external_claim_id: str
    qc_status: str
    updated_at: str


class CompletedReportLatestHtmlResponse(BaseModel):
    claim_id: str
    external_claim_id: str
    version_no: int
    report_html: str
    report_status: str
    report_source: str = "doctor"
    created_by: str = ""
    created_at: str


class LegacyMigrationStartRequest(BaseModel):
    include_users: bool = True
    include_claims: bool = True
    raw_files_only: bool = True
    status_filter: str = Field(default="completed", pattern="^(all|pending|in_review|needs_qc|completed|withdrawn)$")
    batch_size: int = Field(default=200, ge=1, le=500)
    max_batches: int = Field(default=200, ge=1, le=1000)
