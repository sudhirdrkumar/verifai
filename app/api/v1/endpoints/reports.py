from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.db.session import get_db
from app.services.report_generation_service import report_service
from app.schemas.report import MedicalReportResponse, ReportFormat
import logging
import uuid
import json

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports", tags=["Reports"])

REPORT_EDITOR_BASE_URL = "https://verifai.in/qc/public/report-editor.html"


@router.post("/generate/{claim_id}")
def generate_report(claim_id: str, db: Session = Depends(get_db)):
    """
    Auto-generate a medical report draft for a claim from its structured data.
    Creates a draft in the report editor system without manual doctor intervention.
    """
    result = report_service.create_report_for_claim(db, claim_id)

    if not result:
        raise HTTPException(
            status_code=404,
            detail=f"No structured data found for claim {claim_id}. Please ensure Stage 2 processing is complete."
        )

    # Get claim UUID for the report editor
    claim_uuid_result = db.execute(text("""
        SELECT id FROM claims WHERE id = :claim_id LIMIT 1
    """), {"claim_id": claim_id}).scalar()

    return {
        "status": "success",
        "claim_id": claim_id,
        "message": "Report auto-generated successfully",
        "report_link": f"{REPORT_EDITOR_BASE_URL}?claim_id={claim_id}&claim_uuid={claim_uuid_result}&title=Claim+Report+-+{claim_id}",
        "ready_for_review": True
    }


@router.get("/open/{claim_id}")
def open_report_in_editor(claim_id: str, db: Session = Depends(get_db)):
    """
    Open the auto-generated report in the report editor.
    Doctor can view and make any final adjustments if needed.
    """
    # Verify report exists
    report = db.execute(text("""
        SELECT id, claim_id FROM medical_reports WHERE claim_id = :claim_id
    """), {"claim_id": claim_id}).mappings().first()

    if not report:
        raise HTTPException(
            status_code=404,
            detail=f"Report not found for claim {claim_id}. Generate report first."
        )

    # Get claim UUID
    claim_uuid_result = db.execute(text("""
        SELECT id FROM claims WHERE id = :claim_id LIMIT 1
    """), {"claim_id": claim_id}).scalar()

    # Return redirect link to the report editor
    return {
        "status": "ready",
        "claim_id": claim_id,
        "report_url": f"{REPORT_EDITOR_BASE_URL}?claim_id={claim_id}&claim_uuid={claim_uuid_result}&title=Claim+Report+-+{claim_id}",
        "message": "Open report in editor for final review and adjustments"
    }


@router.get("/{claim_id}/data")
def get_report_data(claim_id: str, db: Session = Depends(get_db)):
    """
    Get the AI-extracted and structured data for a claim.
    This data is used to populate the report editor.
    """
    # Get medical_reports data
    report = db.execute(text("""
        SELECT
            claim_id, hospital_name, treating_doctor, diagnosis,
            complaints, medicine_used, claim_amount, conclusion,
            status, created_at
        FROM medical_reports
        WHERE claim_id = :claim_id
    """), {"claim_id": claim_id}).mappings().first()

    if not report:
        raise HTTPException(
            status_code=404,
            detail=f"Report not found for claim {claim_id}"
        )

    return {
        "claim_id": report['claim_id'],
        "status": report['status'],
        "ai_extracted_data": {
            "facility": {
                "hospital_name": report['hospital_name'] or "Not extracted",
                "treating_doctor": report['treating_doctor'] or "Not extracted"
            },
            "clinical": {
                "diagnosis": report['diagnosis'] or "Not extracted",
                "complaints": report['complaints'] or "Not extracted",
                "medicine_used": report['medicine_used'] or "Not extracted"
            },
            "financial": {
                "claim_amount": report['claim_amount'] or "Not extracted"
            },
            "conclusion": report['conclusion'] or "Pending doctor review"
        },
        "generated_at": report['created_at'],
        "message": "AI-generated report ready for doctor review and finalization"
    }


@router.get("/")
def list_reports(db: Session = Depends(get_db)):
    """
    List all auto-generated reports ready for doctor review.
    Shows status of all claim reports in the system.
    """
    reports = db.execute(text("""
        SELECT claim_id, status, created_at, hospital_name, treating_doctor
        FROM medical_reports
        ORDER BY created_at DESC
        LIMIT 100
    """)).mappings().all()

    return {
        "total_auto_generated_reports": len(reports),
        "reports": [
            {
                "claim_id": r['claim_id'],
                "status": r['status'],
                "hospital": r['hospital_name'] or "Not extracted",
                "doctor": r['treating_doctor'] or "Not extracted",
                "generated_at": r['created_at'],
                "ready_for_doctor_review": r['status'] == 'generated'
            }
            for r in reports
        ],
        "message": "All reports are auto-generated by ML and ready for doctor final review"
    }


@router.post("/{claim_id}/finalize")
def finalize_report(claim_id: str, db: Session = Depends(get_db)):
    """
    Mark a report as finalized by the doctor.
    This means the doctor has reviewed the auto-generated report and approved it.
    """
    result = db.execute(text("""
        UPDATE medical_reports
        SET status = 'finalized', updated_at = NOW()
        WHERE claim_id = :claim_id
        RETURNING id, claim_id, status
    """), {"claim_id": claim_id}).mappings().first()

    if not result:
        raise HTTPException(
            status_code=404,
            detail=f"Report not found for claim {claim_id}"
        )

    db.commit()

    return {
        "status": "success",
        "claim_id": result['claim_id'],
        "report_status": result['status'],
        "message": "Report finalized by doctor. Ready for claim processing."
    }
