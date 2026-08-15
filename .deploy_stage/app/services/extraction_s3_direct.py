"""
Extract directly from S3 using presigned URLs - no download to EC2.
OpenAI fetches the file directly from S3, avoiding memory overhead.
"""
import logging
from typing import Any
from uuid import UUID
import io

import httpx
import boto3
from botocore.exceptions import ClientError, BotoCoreError

try:
    from PyPDF2 import PdfReader, PdfWriter
except ImportError:
    PdfReader = None
    PdfWriter = None

from app.core.config import settings
from app.services.storage_service import download_bytes as storage_download_bytes

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


def _download_s3_object_bytes(bucket: str, key: str) -> bytes:
    if not bucket or not key:
        raise S3DirectExtractionError("Bucket and key are required")
    try:
        # Reuse the app's S3 helper so configured credentials are honored.
        return storage_download_bytes(key)
    except Exception as exc:
        raise S3DirectExtractionError(f"Failed to download S3 object bytes: {exc}") from exc


def _split_large_pdf(pdf_bytes: bytes, max_pages: int = 90) -> list[bytes]:
    """Split large PDF into chunks of max_pages each."""
    if not PdfReader or not PdfWriter:
        logger.warning("PyPDF2 not available, cannot split PDF")
        return [pdf_bytes]

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        num_pages = len(reader.pages)

        if num_pages <= max_pages:
            return [pdf_bytes]  # No split needed

        logger.info(f"PDF has {num_pages} pages, splitting into chunks of {max_pages}")
        chunks = []

        for start_page in range(0, num_pages, max_pages):
            end_page = min(start_page + max_pages, num_pages)
            writer = PdfWriter()

            # Copy pages to new PDF
            for page_num in range(start_page, end_page):
                writer.add_page(reader.pages[page_num])

            # Write to bytes
            output = io.BytesIO()
            writer.write(output)
            output.seek(0)
            chunks.append(output.getvalue())

        logger.info(f"Split PDF into {len(chunks)} chunks")
        return chunks

    except Exception as e:
        logger.warning(f"PDF split failed: {e}, using original")
        return [pdf_bytes]


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

    For large PDFs (>100 pages):
    - Downloads once and splits locally
    - Extracts from each chunk
    - Combines results
    """
    logger.info(f"Starting S3-direct extraction: {document_name} ({mime_type})")

    # Generate presigned URL
    try:
        presigned_url = generate_s3_presigned_url(s3_bucket, storage_key)
    except S3DirectExtractionError as exc:
        raise S3DirectExtractionError(f"Cannot create presigned URL: {exc}") from exc

    # For PDFs, check size first
    safe_mime = str(mime_type or "").strip().lower()
    is_pdf = safe_mime == "application/pdf" or str(document_name or "").lower().endswith(".pdf")

    if is_pdf:
        try:
            logger.info(f"PDF_PREPROCESS: Downloading PDF bytes directly from S3 for: {document_name}")
            pdf_bytes = _download_s3_object_bytes(s3_bucket, storage_key)
            logger.info(f"PDF_PREPROCESS: Downloaded {len(pdf_bytes)} bytes for {document_name}")
            # Reuse the main OpenAI extraction pipeline, but feed it the PDF bytes we
            # already fetched directly from S3. That keeps the direct-S3 fix while
            # preserving the richer parsing, normalization, and fallback logic.
            from app.schemas.extraction import ExtractionProvider
            from app.services.extraction_providers import run_extraction

            logger.info(f"PDF_PREPROCESS: Handing S3-downloaded PDF bytes to core OpenAI extractor for: {document_name}")
            return run_extraction(
                provider=ExtractionProvider.openai,
                document_name=document_name,
                mime_type=mime_type,
                payload=pdf_bytes,
                storage_key=storage_key,
                s3_bucket=s3_bucket,
            )

        except Exception as e:
            logger.warning(f"PDF pre-processing/byte extraction failed: {e}, falling back to URL extraction")

    # Prepare OpenAI request with S3 URL
    try:
        if is_pdf:
            logger.info(f"S3DIRECT_PDF_DIRECT: downloading PDF {document_name} from S3 for core OpenAI extraction")
            pdf_bytes = _download_s3_object_bytes(s3_bucket, storage_key)
            from app.schemas.extraction import ExtractionProvider
            from app.services.extraction_providers import run_extraction

            return run_extraction(
                provider=ExtractionProvider.openai,
                document_name=document_name,
                mime_type=mime_type,
                payload=pdf_bytes,
                storage_key=storage_key,
                s3_bucket=s3_bucket,
            )
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


def _call_openai_with_pdf_bytes(
    document_name: str,
    mime_type: str,
    pdf_bytes: bytes,
) -> dict[str, Any]:
    """
    Call OpenAI Vision API with PDF bytes directly (for chunked processing).
    Used when PDF needs to be split into multiple chunks.
    """
    if not settings.openai_api_key:
        raise S3DirectExtractionError("OPENAI_API_KEY not configured")

    import base64

    safe_name = (document_name or "document").strip() or "document"
    safe_mime = (mime_type or "application/pdf").strip().lower()

    logger.info(f"OPENAI_PDF_BYTES: processing {safe_name} ({len(pdf_bytes)} bytes)")

    user_prompt = (
        "Extract structured data from this medical PDF page/chunk. Return strict JSON only.\n"
        "CRITICAL: Extract ALL investigations and TPR values if present.\n"
        "Medicine bills and medication lines are handled separately from Textract/text parsing.\n"
        "Focus on: diagnosis, clinical findings, lab results, and vitals.\n\n"
        "JSON schema:\n"
        "{\n"
        '  "extracted_entities": {\n'
        '    "diagnosis": "",\n'
        '    "chief_complaints_at_admission": "",\n'
        '    "all_investigation_reports_with_values": [],\n'
        '    "daily_tpr_chart_min_max": "",\n'
        '    "clinical_findings": ""\n'
        "  }\n"
        "}\n"
    )

    user_content = [{"type": "input_text", "text": user_prompt}]

    # Add PDF as base64 file for the OpenAI Responses API.
    pdf_base64 = base64.standard_b64encode(pdf_bytes).decode('utf-8')
    data_uri = f"data:{safe_mime or 'application/pdf'};base64,{pdf_base64}"
    user_content.append({
        "type": "input_file",
        "filename": safe_name,
        "file_data": data_uri,
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
        "input": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": "You are a medical-claim extraction service. Return strict JSON only.",
                    }
                ],
            },
            {
                "role": "user",
                "content": user_content,
            }
        ],
        "temperature": 0,
    }

    try:
        with httpx.Client(timeout=OPENAI_API_TIMEOUT) as client:
            response = client.post(
                f"{base_url}/responses",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()

        result = response.json()
        model_output = _extract_openai_response_text(result)
        extracted = _parse_json_entities(model_output)
        entities = extracted.get("extracted_entities", extracted) if isinstance(extracted, dict) else {}

        logger.info(f"OPENAI_PDF_BYTES_RESPONSE: document={safe_name}, extracted_keys={list(entities.keys()) if isinstance(entities, dict) else 'EMPTY'}")

        return {
            "provider": "openai-pdf-bytes",
            "model_name": "gpt-4o-mini",
            "extraction_version": "openai-v2-pdf-bytes",
            "extracted_entities": entities if isinstance(entities, dict) else {},
            "evidence_refs": extracted.get("evidence_refs", []) if isinstance(extracted, dict) else [],
            "confidence": float(extracted.get("confidence", 0.80) or 0.80) if isinstance(extracted, dict) else 0.80,
            "raw_response": {"model_output_text": model_output, "responses_body": result},
        }

    except Exception as exc:
        raise S3DirectExtractionError(f"OpenAI PDF bytes call failed: {exc}") from exc


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

    # Build user content with S3 URL instead of embedded file for images.
    user_prompt = (
        "Extract structured data from this medical claim document for a health-claim assessment sheet. Return strict JSON only.\n"
        "CRITICAL: Extract ALL investigation reports and TPR/vitals data if present in document.\n"
        "Medicine bills and medication lines are handled separately from Textract/text parsing.\n"
        "If investigation/TPR not found, set to empty array/string (not null).\n"
        "Keep complaints, diagnosis, clinical findings, investigations, TPR/vitals, and conclusion fields separate.\n"
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

    user_content = [{"type": "input_text", "text": user_prompt}]

    # Add S3 URL as file reference
    if is_image:
        user_content.append({
            "type": "input_image",
            "image_url": s3_url,
        })
    else:
        # Responses API does not accept arbitrary PDF URLs as a Chat-style document.
        # Images can use the presigned URL directly.
        logger.info(f"S3DIRECT_IMAGE_URL: sending {safe_name} via presigned URL to OpenAI Responses")

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
        "input": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": "You are a medical-claim extraction service. Return strict JSON only.",
                    }
                ],
            },
            {
                "role": "user",
                "content": user_content,
            }
        ],
        "temperature": 0,
    }

    try:
        with httpx.Client(timeout=OPENAI_API_TIMEOUT) as client:
            response = client.post(
                f"{base_url}/responses",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()

        result = response.json()
        model_output = _extract_openai_response_text(result)
        extracted = _parse_json_entities(model_output)

        # Check for critical missing fields
        entities = extracted.get("extracted_entities", extracted) if isinstance(extracted, dict) else {}
        has_investigations = bool(entities.get("all_investigation_reports_with_values") or entities.get("deranged_investigation_reports"))
        has_tpr = bool(entities.get("daily_tpr_chart_min_max"))
        has_medicines = bool(entities.get("medicine_used"))

        logger.info(f"S3DIRECT_EXTRACTION_REPORT: document={safe_name}, has_investigations={has_investigations}, has_tpr={has_tpr}, has_medicines={has_medicines}")
        if not has_investigations or not has_tpr or not has_medicines:
            logger.warning(f"S3DIRECT_MISSING_CRITICAL_FIELDS: document={safe_name}, raw_length={len(model_output)}, first_500_chars={model_output[:500]}")

        return {
            "provider": "openai-s3-direct",
            "model_name": "gpt-4o-mini",
            "extraction_version": "openai-v2-s3-direct",
            "extracted_entities": entities if isinstance(entities, dict) else {},
            "evidence_refs": extracted.get("evidence_refs", []) if isinstance(extracted, dict) else [],
            "confidence": float(extracted.get("confidence", 0.85) or 0.85) if isinstance(extracted, dict) else 0.85,
            "raw_response": {
                "model_output_text": model_output,
                "s3_url_used": True,
                "responses_body": result,
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
        # Try to extract a complete JSON object from text.
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass

    return {}


def _extract_openai_response_text(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    direct = body.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks: list[str] = []
    output = body.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    chunks.append(text.strip())
    return "\n".join(chunks).strip()
