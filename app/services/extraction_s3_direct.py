"""
Extract directly from S3 using presigned URLs - no download to EC2.
OpenAI fetches the file directly from S3, avoiding memory overhead.
"""
import logging
from typing import Any
from uuid import UUID

import httpx
import boto3
from botocore.exceptions import ClientError, BotoCoreError

from app.core.config import settings

logger = logging.getLogger(__name__)

# Configuration
PRESIGNED_URL_EXPIRY = 3600  # 1 hour
OPENAI_API_TIMEOUT = 120  # 2 minutes


class S3DirectExtractionError(Exception):
    pass


def generate_s3_presigned_url(
    bucket: str,
    key: str,
    expiry_seconds: int = PRESIGNED_URL_EXPIRY,
) -> str:
    """
    Generate a presigned URL for S3 object.
    URL can be used directly by OpenAI to download the file.
    """
    if not bucket or not key:
        raise S3DirectExtractionError("Bucket and key are required")

    try:
        s3_client = boto3.client("s3", region_name=settings.s3_region)
        url = s3_client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expiry_seconds,
        )
        logger.info(f"Generated presigned URL for s3://{bucket}/{key}")
        return url
    except (ClientError, BotoCoreError) as exc:
        raise S3DirectExtractionError(f"Failed to generate presigned URL: {exc}") from exc


def extract_via_s3_presigned_url(
    s3_bucket: str,
    storage_key: str,
    document_name: str,
    mime_type: str,
) -> dict[str, Any]:
    """
    Extract from PDF/image using S3 presigned URL.
    OpenAI fetches file directly from S3, no EC2 download needed.

    Reduces:
    - Network I/O (no EC2 ↔ S3 transfer)
    - Memory usage (no file buffering on EC2)
    - Processing time (OpenAI fetches directly)
    - EC2 load (frees connection pool sooner)
    """
    logger.info(f"Starting S3-direct extraction: {document_name} ({mime_type})")

    # Generate presigned URL
    try:
        presigned_url = generate_s3_presigned_url(s3_bucket, storage_key)
    except S3DirectExtractionError as exc:
        raise S3DirectExtractionError(f"Cannot create presigned URL: {exc}") from exc

    # Prepare OpenAI request with S3 URL
    try:
        result = _call_openai_with_s3_url(
            document_name=document_name,
            mime_type=mime_type,
            s3_url=presigned_url,
        )
        logger.info(f"S3-direct extraction completed for {document_name}")
        return result
    except Exception as exc:
        logger.error(f"S3-direct extraction failed: {exc}")
        raise


def _call_openai_with_s3_url(
    document_name: str,
    mime_type: str,
    s3_url: str,
) -> dict[str, Any]:
    """
    Call OpenAI Vision API with S3 presigned URL.
    OpenAI downloads the file directly from S3.
    """
    if not settings.openai_api_key:
        raise S3DirectExtractionError("OPENAI_API_KEY not configured")

    safe_name = (document_name or "document").strip() or "document"
    safe_mime = (mime_type or "application/pdf").strip().lower()
    is_image = safe_mime.startswith("image/")

    logger.info(f"S3DIRECT_DEBUG: _call_openai_with_s3_url called: document={safe_name}, raw_mime={mime_type}, safe_mime={safe_mime}, is_image={is_image}")

    # Build user content with S3 URL instead of embedded file
    user_prompt = (
        "Extract structured data from this medical claim document for a health-claim assessment sheet. Return strict JSON only.\n"
        "Keep complaints, diagnosis, clinical findings, investigations, TPR/vitals, medicines, and conclusion fields separate.\n"
        "Do not put patient name as hospital/vendor/doctor. Use '-' for unknown values.\n\n"
        "JSON schema:\n"
        "{\n"
        '  "extracted_entities": {\n'
        '    "name": "",\n'
        '    "patient_name": "",\n'
        '    "hospital_name": "",\n'
        '    "pharmacy_name": "",\n'
        '    "treating_doctor": "",\n'
        '    "doctor_registration_number": "",\n'
        '    "admission_date": "",\n'
        '    "discharge_date": "",\n'
        '    "claim_amount": "",\n'
        '    "diagnosis": "",\n'
        '    "chief_complaints_at_admission": "",\n'
        '    "major_diagnostic_finding": "",\n'
        '    "alcoholism_history": "",\n'
        '    "clinical_findings": "",\n'
        '    "all_investigation_reports_with_values": [],\n'
        '    "date_wise_investigation_reports": [],\n'
        '    "deranged_investigation_reports": [],\n'
        '    "daily_tpr_chart_min_max": "",\n'
        '    "medicine_used": "",\n'
        '    "bill_amount": "",\n'
        '    "detailed_conclusion": "",\n'
        '    "recommendation": ""\n'
        "  },\n"
        '  "evidence_refs": [{"type":"text","field":"","snippet":""}],\n'
        '  "confidence": 0.0\n'
        "}\n\n"
        f"Document: {safe_name}\n"
        f"MIME type: {safe_mime}\n"
    )

    user_content = [{"type": "text", "text": user_prompt}]

    # Add S3 URL as file reference
    if is_image:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": s3_url}
        })
    else:
        # For PDFs, send as URL to OpenAI directly
        logger.info(f"S3DIRECT_PDF: sending PDF {safe_name} via URL to OpenAI")
        user_content.append({
            "type": "document",
            "document": {
                "type": "application/pdf",
                "source": {
                    "type": "url",
                    "url": s3_url
                }
            }
        })

    base_url = (
        settings.openai_base_url.rstrip("/")
        if settings.openai_base_url
        else "https://api.openai.com/v1"
    )

    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {
                "role": "user",
                "content": user_content,
            }
        ],
        "max_tokens": 4096,
        "temperature": 0,
    }

    try:
        with httpx.Client(timeout=OPENAI_API_TIMEOUT) as client:
            response = client.post(
                f"{base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()

        result = response.json()
        model_output = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        extracted = _parse_json_entities(model_output)

        logger.info(f"S3DIRECT_OPENAI_RESPONSE: document={safe_name}, model_output_length={len(model_output)}, extracted_keys={list(extracted.keys()) if extracted else 'EMPTY'}")
        if not extracted or not any(extracted.values()):
            logger.warning(f"S3DIRECT_EMPTY_EXTRACTION: document={safe_name}, raw_response={model_output[:500]}")

        return {
            "provider": "openai-s3-direct",
            "model_name": "gpt-4o-mini",
            "extraction_version": "openai-v2-s3-direct",
            "extracted_entities": extracted,
            "evidence_refs": [],
            "confidence": 0.85,  # Direct S3 extraction has good confidence
            "raw_response": {
                "model_output_text": model_output,
                "s3_url_used": True,
            },
        }

    except httpx.TimeoutException as exc:
        raise S3DirectExtractionError(
            f"OpenAI API timeout after {OPENAI_API_TIMEOUT}s: {exc}"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise S3DirectExtractionError(
            f"OpenAI API error {exc.response.status_code}: {exc.response.text}"
        ) from exc
    except Exception as exc:
        raise S3DirectExtractionError(f"OpenAI API call failed: {exc}") from exc


def _parse_json_entities(text: str) -> dict:
    """Extract JSON entities from model output."""
    import json
    import re

    try:
        # Try direct JSON parse first
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to extract JSON from text
        match = re.search(r"\{[^{}]*\}", text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

    return {}
