from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TeamRightWorksCaseIntakeRequest(BaseModel):
    external_claim_id: str = Field(min_length=1, max_length=100)
    patient_name: str | None = Field(default=None, max_length=255)
    patient_identifier: str | None = Field(default=None, max_length=100)
    assigned_doctor_id: str | None = Field(default=None, max_length=100)
    status: str | None = Field(default=None, max_length=40)
    priority: int = Field(default=3, ge=1, le=5)
    source_channel: str | None = Field(default="teamrightworks.in", max_length=100)
    tags: list[str] | None = None

    legacy_payload: dict[str, Any] | None = None
    raw_files_only: bool = False

    report_html: str | None = None
    report_status: str = Field(default="completed", max_length=30)
    doctor_username: str | None = Field(default=None, max_length=100)
    doctor_opinion: str | None = Field(default=None, max_length=20000)

    recommendation: str | None = Field(default=None, max_length=40)
    explanation_summary: str | None = Field(default=None, max_length=20000)
    decision_payload: dict[str, Any] | None = None

    auditor_label: str | None = Field(default=None, max_length=40)
    auditor_notes: str | None = Field(default=None, max_length=4000)

    event_occurred_at: datetime | None = None
    sync_ref: str | None = Field(default=None, max_length=160)


class TeamRightWorksCaseIntakeResponse(BaseModel):
    ok: bool = True
    claim_id: str
    external_claim_id: str
    created_claim: bool
    report_version_no: int | None = None
    decision_id: str | None = None
    feedback_label_saved: bool = False
    message: str

