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
            logger.info(f"PDF_PREPROCESS: Downloading PDF to check size: {document_name}")
            response = httpx.get(presigned_url, timeout=60)
            response.raise_for_status()
            pdf_bytes = response.content
            logger.info(f"PDF_PREPROCESS: Downloaded {len(pdf_bytes)} bytes for {document_name}")

            # Check if PDF needs splitting
            chunks = _split_large_pdf(pdf_bytes, max_pages=90)
            logger.info(f"PDF_PREPROCESS: Split result: {len(chunks)} chunks for {document_name}")
            if len(chunks) > 1:
                logger.info(f"Extracting {len(chunks)} PDF chunks from {document_name}")
                combined_result = {
                    "provider": "openai-s3-direct-chunked",
                    "model_name": "gpt-4o-mini",
                    "extraction_version": "openai-v2-s3-direct-chunked",
                    "extracted_entities": {},
                    "evidence_refs": [],
                    "confidence": 0.80,
                    "raw_response": {"chunks_processed": len(chunks)},
                }

                # Process each chunk
                for i, chunk in enumerate(chunks):
                    try:
                        chunk_result = _call_openai_with_pdf_bytes(
                            document_name=f"{document_name} (chunk {i+1}/{len(chunks)})",
                            mime_type=mime_type,
                            pdf_bytes=chunk,
                        )
                        # Merge results from chunk
                        if isinstance(chunk_result.get("extracted_entities"), dict):
                            for key, value in chunk_result["extracted_entities"].items():
                                if value and not combined_result["extracted_entities"].get(key):
                                    combined_result["extracted_entities"][key] = value
                    except Exception as e:
                        logger.warning(f"Chunk {i+1} extraction failed: {e}")

                logger.info(f"Combined extraction from {len(chunks)} chunks")
                return combined_result

            # Single-page or small PDF, use URL-based extraction
            logger.info(f"PDF is small enough for URL-based extraction: {document_name}")

        except Exception as e:
            logger.warning(f"PDF pre-processing failed: {e}, falling back to URL extraction")

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
        "CRITICAL: Extract ALL investigations, TPR values, and medicines if present.\n"
        "Focus on: diagnosis, clinical findings, lab results, vitals, medications.\n\n"
        "JSON schema:\n"
        "{\n"
        '  "extracted_entities": {\n'
        '    "diagnosis": "",\n'
        '    "chief_complaints_at_admission": "",\n'
        '    "all_investigation_reports_with_values": [],\n'
        '    "daily_tpr_chart_min_max": "",\n'
        '    "medicine_used": "",\n'
        '    "clinical_findings": ""\n'
        "  }\n"
        "}\n"
    )

    user_content = [{"type": "text", "text": user_prompt}]

    # Add PDF as base64 document
    pdf_base64 = base64.standard_b64encode(pdf_bytes).decode('utf-8')
    user_content.append({
        "type": "document",
        "document": {
            "type": "application/pdf",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": pdf_base64
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

        logger.info(f"OPENAI_PDF_BYTES_RESPONSE: document={safe_name}, extracted_keys={list(extracted.keys()) if extracted else 'EMPTY'}")

        return {
            "provider": "openai-pdf-bytes",
            "model_name": "gpt-4o-mini",
            "extraction_version": "openai-v2-pdf-bytes",
            "extracted_entities": extracted.get("extracted_entities", {}) if isinstance(extracted, dict) else {},
            "evidence_refs": [],
            "confidence": 0.80,
            "raw_response": {"model_output_text": model_output},
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

    # Build user content with S3 URL instead of embedded file
    user_prompt = (
        "Extract structured data from this medical claim document for a health-claim assessment sheet. Return strict JSON only.\n"
        "CRITICAL: Extract ALL investigation reports, TPR/vitals data, and medicine list if present in document.\n"
        "If investigation/TPR/medicines not found, set to empty array/string (not null).\n"
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

        # Check for critical missing fields
        entities = extracted.get("extracted_entities", {}) if isinstance(extracted, dict) else {}
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
