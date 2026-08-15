"""
PHASE 5: ML-Powered Conclusion Generation using OpenAI GPT-4 Omni
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, status, Body

from app.api.deps.auth import require_roles
from app.schemas.auth import UserRole
from app.services.auth_service import AuthenticatedUser
from app.services.phase5_ml_openai import Phase5MLGenerator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/phase5", tags=["phase5-ml"])

_ml_generator: Phase5MLGenerator | None = None


def get_ml_generator() -> Phase5MLGenerator:
    global _ml_generator
    if _ml_generator is None:
        _ml_generator = Phase5MLGenerator()
    return _ml_generator


@router.post("/generate-conclusion")
async def generate_conclusion(
    claim_data: dict = Body(...),
    _current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user, UserRole.doctor, UserRole.auditor)),
):
    """
    Generate medical conclusion using OpenAI GPT-4 Omni.

    Request body should include:
    - chief_complaint: Patient's primary complaint
    - symptoms: Detailed symptoms
    - diagnosis: Clinical diagnosis
    - treatment_given: Treatment provided
    - investigations: Lab/investigation results (dict)
    - admission_date: Start date
    - discharge_date: End date
    - los_days: Length of stay
    - claim_amount: Claim amount in Rs.
    """
    try:
        logger.info(f"Received claim_data request with claim_id: {claim_data.get('claim_id')}")

        if not claim_data.get("claim_id"):
            raise ValueError("claim_id is required")

        logger.info(f"Generating conclusion for claim: {claim_data.get('claim_id')}")
        # Generate conclusion using OpenAI
        ml_generator = get_ml_generator()
        result = ml_generator.generate_conclusion(claim_data)
        logger.info(f"Successfully generated conclusion, tokens: {result.get('tokens_used')}")

        # TODO: Grammar check disabled temporarily for performance. Re-enable with async processing.
        conclusion_text = result.get("conclusion", "")
        # try:
        #     # Wrap conclusion in HTML for grammar checking
        #     conclusion_html = f"<p>{conclusion_text}</p>"
        #     grammar_result = grammar_check_report_html(conclusion_html)
        #     corrected_html = grammar_result.get("corrected_html", conclusion_html)
        #     # Extract text from corrected HTML
        #     import re
        #     conclusion_text = re.sub(r"<[^>]+>", "", corrected_html).strip()
        #     logger.info(f"Grammar check applied to conclusion: {grammar_result.get('corrected_segments', 0)} segments corrected")
        # except GrammarCheckError as exc:
        #     logger.warning(f"Grammar check failed for conclusion, using original: {exc}")
        # except Exception as exc:
        #     logger.warning(f"Unexpected error during grammar check: {exc}")

        response = {
            "success": True,
            "data": {
                "conclusion": conclusion_text,
                "confidence_score": result["confidence_score"],
                "recommendation": result["recommendation"],
                "tokens_used": result["tokens_used"],
                "model": result["model"],
                "draft_id": f"draft_{claim_data.get('claim_id')}_openai",
                "status": "pending_review"
            }
        }
        logger.info(f"Response ready, conclusion length: {len(conclusion_text)}")
        return response

    except ValueError as ve:
        logger.warning(f"Validation error: {ve}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ve)
        )
    except Exception as exc:
        logger.error(f"Error generating conclusion: {type(exc).__name__}: {exc}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate conclusion: {str(exc)}"
        )


@router.post("/review-conclusion")
async def review_conclusion(
    draft_id: str = Body(...),
    claim_id: str = Body(...),
    action: str = Body(...),
    doctor_id: str = Body(...),
    edited_conclusion: str = Body(None),
):
    """
    QC doctor reviews and approves/rejects ML-generated conclusion.

    Actions: approve, approve_with_edits, reject
    """
    try:
        if action not in ["approve", "approve_with_edits", "reject"]:
            raise ValueError(f"Invalid action: {action}")

        return {
            "success": True,
            "draft_id": draft_id,
            "claim_id": claim_id,
            "action": action,
            "doctor_id": doctor_id,
            "status": "reviewed",
            "final_conclusion": edited_conclusion if action == "approve_with_edits" else None,
            "timestamp": None  # Server will fill this
        }
    except Exception as exc:
        logger.error(f"Error reviewing conclusion: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to review conclusion: {str(exc)}"
        )


@router.get("/model-performance")
async def get_model_performance():
    """Get Phase 5 ML model performance metrics"""
    return {
        "success": True,
        "data": {
            "model": "OpenAI GPT-4 Omni",
            "status": "active",
            "performance": {
                "avg_confidence": 0.82,
                "total_conclusions": 0,
                "approval_rate": 0.0,
                "avg_response_time_ms": 2500
            }
        }
    }


@router.get("/ml-confidence/{claim_id}")
async def get_claim_with_ml_confidence(claim_id: str):
    """
    Get claim with ML confidence score.
    This would be called after QC reviews the claim.
    """
    return {
        "success": True,
        "data": {
            "claim_id": claim_id,
            "ml_model": "OpenAI GPT-4 Omni",
            "ml_status": "ready_for_analysis"
        }
    }
