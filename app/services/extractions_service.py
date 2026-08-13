import json
import logging
import math
import re
from typing import Any
from uuid import UUID, uuid4

import boto3
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.schemas.extraction import (
    ExtractionListResponse,
    ExtractionProvider,
    ExtractionResponse,
)
from app.services.extraction_providers import (
    ExtractionConfigError,
    ExtractionProcessingError,
    run_extraction,
)
from app.services.storage_service import StorageConfigError, StorageOperationError, download_bytes


class DocumentNotFoundError(Exception):
    pass


logger = logging.getLogger(__name__)

_TPR_DOC_HINT_RE = re.compile(
    r"\b(tpr|vitals?|vital\s*chart|temperature|pulse|blood\s*pressure|respiratory|bp)\b",
    re.I,
)
_NURSING_TREATMENT_DOC_HINT_RE = re.compile(
    r"\b(nursing\s*treatment\s*chart|nursing\s*treatment|nursing\s*chart|treatment\s*chart)\b",
    re.I,
)
_HANDWRITING_HINT_RE = re.compile(r"\b(handwritten|hand\s*written|handwriting)\b", re.I)
_PRINTED_HINT_RE = re.compile(r"\b(printed|typed|computer[-\s]*generated|digital)\b", re.I)


def _normalize_json(value: Any, as_type: type) -> Any:
    if isinstance(value, as_type):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, as_type) else as_type()
        except json.JSONDecodeError:
            return as_type()
    return as_type()


def _to_response(row: dict[str, Any]) -> ExtractionResponse:
    row["extracted_entities"] = _normalize_json(row.get("extracted_entities"), dict)
    row["evidence_refs"] = _normalize_json(row.get("evidence_refs"), list)

    raw_response = row.get("raw_response")
    if isinstance(raw_response, dict):
        row["raw_response"] = raw_response
    elif isinstance(raw_response, str):
        try:
            parsed_raw = json.loads(raw_response)
            row["raw_response"] = parsed_raw if isinstance(parsed_raw, dict) else None
        except json.JSONDecodeError:
            row["raw_response"] = None
    else:
        row["raw_response"] = None

    return ExtractionResponse.model_validate(row)

def _sanitize_json_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _sanitize_json_payload(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_json_payload(v) for v in value]
    if isinstance(value, str):
        # PostgreSQL jsonb cannot store NUL bytes; strip them defensively.
        return value.replace("\x00", "")
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
    return value


def _as_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _handwriting_signal_blob(file_name: Any, extracted_entities: Any) -> str:
    entities = _as_json_dict(extracted_entities)
    parts: list[str] = [str(file_name or "")]
    try:
        payload = json.dumps(entities, ensure_ascii=False)
    except Exception:
        payload = str(entities)
    if payload:
        parts.append(payload[:24000])
    return "\n".join(parts).lower()


def _detect_document_handwriting_kind(file_name: Any, extracted_entities: Any) -> str:
    entities = _as_json_dict(extracted_entities)
    for key, value in entities.items():
        key_norm = re.sub(r"[^a-z0-9]+", "", str(key or "").strip().lower())
        if not key_norm:
            continue
        if "handwrit" not in key_norm and key_norm not in {
            "texttype",
            "writingtype",
            "scriptstyle",
            "ocrtexttype",
        }:
            continue

        if isinstance(value, bool):
            return "handwritten" if value else "printed"

        value_text = str(value or "").strip().lower()
        if _HANDWRITING_HINT_RE.search(value_text):
            return "handwritten"
        if _PRINTED_HINT_RE.search(value_text):
            return "printed"

    blob = _handwriting_signal_blob(file_name, entities)
    hand_hits = len(_HANDWRITING_HINT_RE.findall(blob))
    printed_hits = len(_PRINTED_HINT_RE.findall(blob))
    if hand_hits > 0 and hand_hits > printed_hits:
        return "handwritten"
    if printed_hits > 0 and printed_hits > hand_hits:
        return "printed"
    if hand_hits > 0 and printed_hits > 0:
        return "mixed"
    return "unknown"


def _doc_matches_hint(file_name: Any, extracted_entities: Any, hint_re: re.Pattern[str]) -> bool:
    file_text = str(file_name or "")
    if hint_re.search(file_text):
        return True

    entities = _as_json_dict(extracted_entities)
    for key in (
        "document_type",
        "document_category",
        "chart_type",
        "document_name",
        "clinical_findings",
        "text_preview",
    ):
        if hint_re.search(str(entities.get(key) or "")):
            return True

    try:
        payload = json.dumps(entities, ensure_ascii=False)
    except Exception:
        payload = str(entities)
    return bool(hint_re.search((payload or "")[:12000]))


def _evaluate_tpr_vs_nursing_handwriting(doc_rows: list[dict[str, Any]]) -> dict[str, Any]:
    prepared: list[dict[str, Any]] = []
    for row in doc_rows:
        file_name = str(row.get("file_name") or "")
        extracted_entities = row.get("extracted_entities")
        prepared.append(
            {
                "file_name": file_name,
                "is_tpr": _doc_matches_hint(file_name, extracted_entities, _TPR_DOC_HINT_RE),
                "is_nursing_treatment": _doc_matches_hint(file_name, extracted_entities, _NURSING_TREATMENT_DOC_HINT_RE),
                "handwriting_kind": _detect_document_handwriting_kind(file_name, extracted_entities),
            }
        )

    tpr_docs = [d for d in prepared if d.get("is_tpr")]
    nursing_docs = [d for d in prepared if d.get("is_nursing_treatment")]
    considered = [d for d in prepared if d.get("is_tpr") or d.get("is_nursing_treatment")]

    analysis: dict[str, Any] = {
        "flagged": False,
        "status": "not_available",
        "summary": "TPR chart or Nursing Treatment Chart not identified",
        "tpr_documents": len(tpr_docs),
        "nursing_treatment_documents": len(nursing_docs),
        "documents_considered": len(considered),
        "document_names_considered": [str(d.get("file_name") or "") for d in considered[:15]],
    }
    if not tpr_docs:
        analysis["summary"] = "TPR chart not identified"
        return analysis
    if not nursing_docs:
        analysis["summary"] = "Nursing Treatment Chart not identified"
        return analysis
    if not considered:
        analysis["status"] = "unknown"
        analysis["summary"] = "No chart documents available for handwriting comparison"
        return analysis

    known_kinds = [
        str(d.get("handwriting_kind") or "")
        for d in considered
        if str(d.get("handwriting_kind") or "") in {"handwritten", "printed", "mixed"}
    ]
    if len(known_kinds) != len(considered):
        analysis["status"] = "unknown"
        analysis["summary"] = "Insufficient handwriting metadata in TPR/Nursing charts"
        return analysis

    distinct_kinds = set(known_kinds)
    if len(distinct_kinds) == 1:
        only_kind = known_kinds[0]
        if only_kind == "printed":
            analysis["status"] = "not_applicable"
            analysis["summary"] = "TPR and Nursing Treatment charts are printed/computer generated"
            return analysis
        analysis["flagged"] = True
        analysis["status"] = "likely_same"
        analysis["summary"] = "TPR and Nursing Treatment charts appear in the same handwriting"
        return analysis

    analysis["status"] = "different"
    analysis["summary"] = "Different writing style detected between TPR and Nursing Treatment charts"
    return analysis


def _ensure_claim_report_uploads_table(db: Session) -> None:
    # DDL is handled by startup migrations (app/db/migrations.py).
    # Keep as no-op for backward compatibility with existing call sites.
    return None


def _auto_mark_fraudulent_for_same_handwriting(db: Session, claim_id: UUID, actor_id: str | None) -> dict[str, Any]:
    doc_rows = db.execute(
        text(
            """
            SELECT
                cd.id AS document_id,
                COALESCE(cd.file_name, '') AS file_name,
                de.extracted_entities
            FROM claim_documents cd
            LEFT JOIN LATERAL (
                SELECT dex.extracted_entities
                FROM document_extractions dex
                WHERE dex.document_id = cd.id
                ORDER BY dex.created_at DESC
                LIMIT 1
            ) de ON TRUE
            WHERE cd.claim_id = :claim_id
            """
        ),
        {"claim_id": str(claim_id)},
    ).mappings().all()

    analysis = _evaluate_tpr_vs_nursing_handwriting([dict(r) for r in doc_rows])
    analysis["tag_applied"] = False
    if not bool(analysis.get("flagged")):
        return analysis

    _ensure_claim_report_uploads_table(db)

    updated = db.execute(
        text(
            """
            INSERT INTO claim_report_uploads (
                claim_id,
                report_export_status,
                tagging,
                subtagging,
                opinion,
                qc_status,
                updated_by
            )
            VALUES (
                :claim_id,
                'pending',
                'Fraudulent',
                'Circumstantial evidence suggesting of possible fraud',
                :opinion,
                'no',
                :updated_by
            )
            ON CONFLICT (claim_id) DO UPDATE
            SET
                tagging = EXCLUDED.tagging,
                subtagging = EXCLUDED.subtagging,
                opinion = CASE
                    WHEN NULLIF(TRIM(COALESCE(claim_report_uploads.opinion, '')), '') IS NULL
                        THEN EXCLUDED.opinion
                    ELSE claim_report_uploads.opinion
                END,
                updated_by = EXCLUDED.updated_by,
                updated_at = NOW()
            WHERE NULLIF(TRIM(COALESCE(claim_report_uploads.tagging, '')), '') IS NULL
            RETURNING claim_id
            """
        ),
        {
            "claim_id": str(claim_id),
            "opinion": "Auto-flagged during extraction: TPR and Nursing Treatment charts appear in the same handwriting.",
            "updated_by": actor_id or "system:auto-handwriting-check",
        },
    ).mappings().first()

    analysis["tag_applied"] = bool(updated)
    return analysis



def _emit_workflow_event(
    db: Session,
    claim_id: UUID,
    event_type: str,
    actor_id: str | None,
    payload: dict[str, Any],
) -> None:
    db.execute(
        text(
            """
            INSERT INTO workflow_events (claim_id, actor_type, actor_id, event_type, event_payload)
            VALUES (:claim_id, 'user', :actor_id, :event_type, CAST(:event_payload AS jsonb))
            """
        ),
        {
            "claim_id": str(claim_id),
            "actor_id": actor_id,
            "event_type": event_type,
            "event_payload": json.dumps(payload),
        },
    )


def run_document_extraction(
    db: Session,
    document_id: UUID,
    provider: ExtractionProvider,
    actor_id: str | None,
    force_refresh: bool = False,
) -> ExtractionResponse:
    doc = db.execute(
        text(
            """
            SELECT id, claim_id, storage_key, file_name, mime_type, metadata
            FROM claim_documents
            WHERE id = :document_id
            """
        ),
        {"document_id": str(document_id)},
    ).mappings().first()

    if doc is None:
        raise DocumentNotFoundError

    claim_id = doc["claim_id"]
    doc_metadata = _normalize_json(doc.get("metadata"), dict)
    resolved_s3_bucket = str(doc_metadata.get("bucket") or doc_metadata.get("legacy_s3_bucket") or "").strip() or None
    storage_key = str(doc.get("storage_key") or "").strip()

    if force_refresh:
        db.execute(
            text("DELETE FROM document_extractions WHERE claim_id = :claim_id AND document_id = :document_id"),
            {"claim_id": str(claim_id), "document_id": str(document_id)},
        )

    db.execute(
        text("UPDATE claim_documents SET parse_status = 'processing' WHERE id = :document_id"),
        {"document_id": str(document_id)},
    )
    db.commit()

    try:
        payload = download_bytes(storage_key)
        extraction_data = run_extraction(
            provider=provider,
            document_name=doc["file_name"],
            mime_type=doc["mime_type"],
            payload=payload,
            storage_key=storage_key or None,
            s3_bucket=resolved_s3_bucket,
        )

        extraction_version = f"{extraction_data['extraction_version']}-{uuid4().hex[:8]}"

        row = db.execute(
            text(
                """
                INSERT INTO document_extractions (
                    claim_id,
                    document_id,
                    extraction_version,
                    model_name,
                    extracted_entities,
                    evidence_refs,
                    confidence,
                    created_by
                )
                VALUES (
                    :claim_id,
                    :document_id,
                    :extraction_version,
                    :model_name,
                    CAST(:extracted_entities AS jsonb),
                    CAST(:evidence_refs AS jsonb),
                    :confidence,
                    :created_by
                )
                RETURNING
                    id,
                    claim_id,
                    document_id,
                    extraction_version,
                    model_name,
                    extracted_entities,
                    evidence_refs,
                    confidence,
                    created_by,
                    created_at
                """
            ),
            {
                "claim_id": str(claim_id),
                "document_id": str(document_id),
                "extraction_version": extraction_version,
                "model_name": extraction_data["model_name"],
                "extracted_entities": json.dumps(_sanitize_json_payload(extraction_data["extracted_entities"]), ensure_ascii=False, allow_nan=False),
                "evidence_refs": json.dumps(_sanitize_json_payload(extraction_data["evidence_refs"]), ensure_ascii=False, allow_nan=False),
                "confidence": extraction_data.get("confidence"),
                "created_by": actor_id or extraction_data["provider"],
            },
        ).mappings().one()

        db.execute(
            text("UPDATE claim_documents SET parse_status = 'succeeded', parsed_at = NOW() WHERE id = :document_id"),
            {"document_id": str(document_id)},
        )

        _emit_workflow_event(
            db=db,
            claim_id=claim_id,
            event_type="document_extracted",
            actor_id=actor_id,
            payload={
                "document_id": str(document_id),
                "extraction_id": str(row["id"]),
                "provider": extraction_data["provider"],
                "model_name": extraction_data["model_name"],
            },
        )
        try:
            handwriting_fraud = _auto_mark_fraudulent_for_same_handwriting(db, claim_id, actor_id)
            if bool(handwriting_fraud.get("flagged")) and bool(handwriting_fraud.get("tag_applied")):
                _emit_workflow_event(
                    db=db,
                    claim_id=claim_id,
                    event_type="claim_fraud_auto_flagged",
                    actor_id=actor_id,
                    payload={
                        "reason": "same_handwriting_tpr_nursing_treatment_chart",
                        "tag_applied": bool(handwriting_fraud.get("tag_applied")),
                        "analysis": handwriting_fraud,
                    },
                )
        except Exception as fraud_exc:
            logger.exception(
                "Auto-fraud handwriting check failed for claim_id=%s document_id=%s error=%s",
                claim_id,
                document_id,
                fraud_exc,
            )

        db.commit()
        response_row = dict(row)
        raw_response = extraction_data.get("raw_response")
        if isinstance(raw_response, dict):
            response_row["raw_response"] = _sanitize_json_payload(raw_response)
        return _to_response(response_row)
    except (StorageConfigError, StorageOperationError, ExtractionConfigError, ExtractionProcessingError, SQLAlchemyError, Exception) as exc:
        db.rollback()
        try:
            db.execute(
                text("UPDATE claim_documents SET parse_status = 'failed' WHERE id = :document_id"),
                {"document_id": str(document_id)},
            )
            _emit_workflow_event(
                db=db,
                claim_id=claim_id,
                event_type="document_extraction_failed",
                actor_id=actor_id,
                payload={
                    "document_id": str(document_id),
                    "provider": provider.value,
                    "error": str(exc),
                },
            )
            db.commit()
        except SQLAlchemyError:
            db.rollback()

        if isinstance(exc, SQLAlchemyError):
            raise ExtractionProcessingError(f"database write failed: {exc}") from exc
        if isinstance(exc, (StorageConfigError, StorageOperationError, ExtractionConfigError, ExtractionProcessingError)):
            raise
        raise ExtractionProcessingError(f"unexpected extraction error: {exc}") from exc


# ─────────────────────────────────────────────────────────────────────────────
# Direct extraction pipeline — three explicit phases, DB connection only held
# during fast DB reads/writes, never during S3 download or OpenAI call.
# ─────────────────────────────────────────────────────────────────────────────

def _pipeline_load_doc(document_id: UUID, force_refresh: bool) -> dict[str, Any]:
    """Phase 1 — DB only: load document metadata, mark status=processing, release connection."""
    with SessionLocal() as db:
        doc = db.execute(
            text(
                """
                SELECT id, claim_id, storage_key, file_name, mime_type, metadata
                FROM claim_documents WHERE id = :document_id
                """
            ),
            {"document_id": str(document_id)},
        ).mappings().first()

        if doc is None:
            raise DocumentNotFoundError

        claim_id = doc["claim_id"]
        doc_metadata = _normalize_json(doc.get("metadata"), dict)
        s3_bucket = str(doc_metadata.get("bucket") or doc_metadata.get("legacy_s3_bucket") or "").strip() or None
        storage_key = str(doc.get("storage_key") or "").strip()

        if force_refresh:
            db.execute(
                text("DELETE FROM document_extractions WHERE claim_id = :claim_id AND document_id = :document_id"),
                {"claim_id": str(claim_id), "document_id": str(document_id)},
            )

        db.execute(
            text("UPDATE claim_documents SET parse_status = 'processing' WHERE id = :document_id"),
            {"document_id": str(document_id)},
        )
        db.commit()

    return {
        "document_id": str(document_id),
        "claim_id": claim_id,
        "storage_key": storage_key,
        "file_name": doc["file_name"],
        "mime_type": doc["mime_type"],
        "s3_bucket": s3_bucket,
    }


def run_s3_openai_pipeline(
    storage_key: str,
    file_name: str,
    mime_type: str,
    provider: ExtractionProvider,
    s3_bucket: str | None = None,
) -> dict[str, Any]:
    """
    Phase 2 — No DB: Extract from S3.
    Hybrid approach:
    - OpenAI/auto: Try S3-direct extraction first for PDFs/images.
    - Fallback: AWS Textract for better structured data extraction.
    - Last resort: Local extraction if Textract fails.
    """
    from app.services.extraction_s3_direct import extract_via_s3_presigned_url, S3DirectExtractionError

    safe_mime = str(mime_type or "application/pdf").strip().lower()
    is_image = safe_mime.startswith("image/")
    is_pdf = safe_mime == "application/pdf" or str(file_name or "").lower().endswith(".pdf")

    logger.info(f"HYBRID_EXTRACTION_START: file_name={file_name}, raw_mime_type={mime_type}, safe_mime={safe_mime}, is_image={is_image}, provider={provider.value}")

    if (is_image or is_pdf) and provider in (ExtractionProvider.openai, ExtractionProvider.auto):
        logger.info(f"Using S3-direct extraction for {file_name} with provider={provider.value}")
        try:
            result = extract_via_s3_presigned_url(
                s3_bucket=s3_bucket or settings.s3_bucket,
                storage_key=storage_key,
                document_name=file_name,
                mime_type=mime_type,
            )

            # Check if extraction has critical data
            entities = result.get("extracted_entities", {})
            has_investigations = bool(entities.get("all_investigation_reports_with_values") or entities.get("deranged_investigation_reports"))
            has_tpr = bool(entities.get("daily_tpr_chart_min_max"))
            has_medicines = bool(entities.get("medicine_used"))
            has_diagnosis = bool(entities.get("diagnosis"))
            has_clinical = bool(entities.get("clinical_findings"))

            # Check for corrupted/garbled text in medicine field
            medicine_str = str(entities.get("medicine_used", ""))
            medicine_corrupted = has_medicines and (
                len(medicine_str) > 500 or
                "mojalla" in medicine_str.lower() or
                "dharuwa" in medicine_str.lower() or
                "hazipur" in medicine_str.lower() or
                "enclave" in medicine_str.lower()
            )

            logger.info(f"S3_EXTRACTION_CHECK: {file_name}, has_diagnosis={has_diagnosis}, has_investigations={has_investigations}, has_tpr={has_tpr}, has_medicines={has_medicines}, medicine_corrupted={medicine_corrupted}, has_clinical={has_clinical}")

            # Critical fields needed for complete extraction
            missing_investigations = not has_investigations
            missing_tpr = not has_tpr
            missing_medicines = not has_medicines

            # If ANY critical field is missing OR data is corrupted, use AWS Textract
            if missing_investigations or missing_tpr or missing_medicines or medicine_corrupted:
                logger.warning(f"S3-direct incomplete/corrupted: investigations={has_investigations}, tpr={has_tpr}, medicines={has_medicines}, corrupted={medicine_corrupted}. Falling back to AWS Textract.")
                # Fall through to AWS Textract for better structured extraction
            else:
                return result

        except S3DirectExtractionError as e:
            logger.warning(f"S3-direct extraction failed: {e}. Falling back to AWS Textract for {file_name}")
            # Fall through to AWS Textract

    # Fallback extraction order: OpenAI (best context) → Textract (structured) → Local OCR (last resort)
    file_bytes = download_bytes(storage_key)

    # Try 1: OpenAI with local file (better at understanding context, filtering noise)
    logger.info(f"S3-direct failed for {file_name}. Trying local OpenAI extraction (best context understanding)")
    try:
        openai_result = run_extraction(
            provider=ExtractionProvider.openai,
            document_name=file_name,
            mime_type=mime_type,
            payload=file_bytes,
            storage_key=storage_key or None,
            s3_bucket=s3_bucket,
        )
        logger.info(f"Local OpenAI extraction succeeded for {file_name}")
        entities = openai_result.get("extracted_entities", {})
        logger.info(f"HYBRID_EXTRACTION_FINAL: file_name={file_name}, method={openai_result.get('provider', 'unknown')}, has_investigations={bool(entities.get('all_investigation_reports_with_values'))}, has_tpr={bool(entities.get('daily_tpr_chart_min_max'))}, has_medicines={bool(entities.get('medicine_used'))}")
        return openai_result
    except Exception as openai_err:
        logger.warning(f"Local OpenAI failed for {file_name}: {openai_err}. Trying AWS Textract.")

    # Try 2: AWS Textract for structured data extraction
    try:
        textract_result = run_extraction(
            provider=ExtractionProvider.aws_textract,
            document_name=file_name,
            mime_type=mime_type,
            payload=file_bytes,
            storage_key=storage_key or None,
            s3_bucket=s3_bucket,
        )
        logger.info(f"AWS Textract extraction succeeded for {file_name}")
        entities = textract_result.get("extracted_entities", {})
        logger.info(f"HYBRID_EXTRACTION_FINAL: file_name={file_name}, method={textract_result.get('provider', 'unknown')}, has_investigations={bool(entities.get('all_investigation_reports_with_values'))}, has_tpr={bool(entities.get('daily_tpr_chart_min_max'))}, has_medicines={bool(entities.get('medicine_used'))}")
        return textract_result
    except Exception as textract_err:
        logger.warning(f"AWS Textract failed for {file_name}: {textract_err}. Falling back to local extraction.")

    # Try 3: Local OCR extraction (last resort)
    logger.info(f"Using local extraction (last resort) for {file_name}")
    result = run_extraction(
        provider=provider,
        document_name=file_name,
        mime_type=mime_type,
        payload=file_bytes,
        storage_key=storage_key or None,
        s3_bucket=s3_bucket,
    )
    entities = result.get("extracted_entities", {})
    logger.info(f"HYBRID_EXTRACTION_FINAL: file_name={file_name}, method={result.get('provider', 'unknown')}, has_investigations={bool(entities.get('all_investigation_reports_with_values'))}, has_tpr={bool(entities.get('daily_tpr_chart_min_max'))}, has_medicines={bool(entities.get('medicine_used'))}")
    return result


def _pipeline_save_result(
    document_id: UUID,
    claim_id: Any,
    extraction_data: dict[str, Any],
    actor_id: str | None,
) -> ExtractionResponse:
    """Phase 3 — DB only: insert extraction record, update status=succeeded, emit events."""
    with SessionLocal() as db:
        try:
            extraction_version = f"{extraction_data['extraction_version']}-{uuid4().hex[:8]}"

            row = db.execute(
                text(
                    """
                    INSERT INTO document_extractions (
                        claim_id, document_id, extraction_version, model_name,
                        extracted_entities, evidence_refs, confidence, created_by
                    )
                    VALUES (
                        :claim_id, :document_id, :extraction_version, :model_name,
                        CAST(:extracted_entities AS jsonb), CAST(:evidence_refs AS jsonb),
                        :confidence, :created_by
                    )
                    RETURNING id, claim_id, document_id, extraction_version, model_name,
                              extracted_entities, evidence_refs, confidence, created_by, created_at
                    """
                ),
                {
                    "claim_id": str(claim_id),
                    "document_id": str(document_id),
                    "extraction_version": extraction_version,
                    "model_name": extraction_data["model_name"],
                    "extracted_entities": json.dumps(_sanitize_json_payload(extraction_data["extracted_entities"]), ensure_ascii=False, allow_nan=False),
                    "evidence_refs": json.dumps(_sanitize_json_payload(extraction_data["evidence_refs"]), ensure_ascii=False, allow_nan=False),
                    "confidence": extraction_data.get("confidence"),
                    "created_by": actor_id or extraction_data["provider"],
                },
            ).mappings().one()

            db.execute(
                text("UPDATE claim_documents SET parse_status = 'succeeded', parsed_at = NOW() WHERE id = :document_id"),
                {"document_id": str(document_id)},
            )

            _emit_workflow_event(
                db=db,
                claim_id=claim_id,
                event_type="document_extracted",
                actor_id=actor_id,
                payload={
                    "document_id": str(document_id),
                    "extraction_id": str(row["id"]),
                    "provider": extraction_data["provider"],
                    "model_name": extraction_data["model_name"],
                },
            )

            try:
                handwriting_fraud = _auto_mark_fraudulent_for_same_handwriting(db, claim_id, actor_id)
                if bool(handwriting_fraud.get("flagged")) and bool(handwriting_fraud.get("tag_applied")):
                    _emit_workflow_event(
                        db=db,
                        claim_id=claim_id,
                        event_type="claim_fraud_auto_flagged",
                        actor_id=actor_id,
                        payload={
                            "reason": "same_handwriting_tpr_nursing_treatment_chart",
                            "tag_applied": bool(handwriting_fraud.get("tag_applied")),
                            "analysis": handwriting_fraud,
                        },
                    )
            except Exception as fraud_exc:
                logger.exception(
                    "Auto-fraud handwriting check failed for claim_id=%s document_id=%s error=%s",
                    claim_id, document_id, fraud_exc,
                )

            db.commit()
            response_row = dict(row)
            raw_response = extraction_data.get("raw_response")
            if isinstance(raw_response, dict):
                response_row["raw_response"] = _sanitize_json_payload(raw_response)
            return _to_response(response_row)

        except (SQLAlchemyError, Exception) as exc:
            db.rollback()
            _pipeline_mark_failed(document_id, claim_id, extraction_data.get("provider", ""), actor_id, exc)
            if isinstance(exc, SQLAlchemyError):
                raise ExtractionProcessingError(f"database write failed: {exc}") from exc
            raise


def _pipeline_mark_failed(
    document_id: UUID,
    claim_id: Any,
    provider_value: str,
    actor_id: str | None,
    exc: Exception,
) -> None:
    """DB only: update status=failed and emit failure event. Called on any pipeline error."""
    with SessionLocal() as db:
        try:
            db.execute(
                text("UPDATE claim_documents SET parse_status = 'failed' WHERE id = :document_id"),
                {"document_id": str(document_id)},
            )
            _emit_workflow_event(
                db=db,
                claim_id=claim_id,
                event_type="document_extraction_failed",
                actor_id=actor_id,
                payload={"document_id": str(document_id), "provider": provider_value, "error": str(exc)},
            )
            db.commit()
        except Exception:
            db.rollback()


def run_document_extraction_releasing_db(
    document_id: UUID,
    provider: ExtractionProvider,
    actor_id: str | None,
    force_refresh: bool = False,
) -> ExtractionResponse:
    """
    Extraction pipeline — DB connection released between each phase:
      Phase 1 (DB):  load document info, mark status=processing
      Phase 2 (I/O): S3 download → OpenAI extraction  (no DB held)
      Phase 3 (DB):  save result, mark status=succeeded
    """
    doc_info = _pipeline_load_doc(document_id, force_refresh)

    try:
        extraction_data = run_s3_openai_pipeline(
            storage_key=doc_info["storage_key"],
            file_name=doc_info["file_name"],
            mime_type=doc_info["mime_type"],
            provider=provider,
            s3_bucket=doc_info["s3_bucket"],
        )
    except Exception as exc:
        _pipeline_mark_failed(document_id, doc_info["claim_id"], provider.value, actor_id, exc)
        if isinstance(exc, (StorageConfigError, StorageOperationError, ExtractionConfigError, ExtractionProcessingError)):
            raise
        raise ExtractionProcessingError(f"unexpected extraction error: {exc}") from exc

    return _pipeline_save_result(document_id, doc_info["claim_id"], extraction_data, actor_id)


def list_document_extractions(db: Session, document_id: UUID, limit: int, offset: int) -> ExtractionListResponse:
    params = {"document_id": str(document_id), "limit": limit, "offset": offset}

    exists = db.execute(
        text("SELECT 1 FROM claim_documents WHERE id = :document_id"),
        params,
    ).first()
    if exists is None:
        raise DocumentNotFoundError

    total = db.execute(
        text("SELECT COUNT(*) FROM document_extractions WHERE document_id = :document_id"),
        params,
    ).scalar_one()

    rows = db.execute(
        text(
            """
            SELECT
                id,
                claim_id,
                document_id,
                extraction_version,
                model_name,
                extracted_entities,
                evidence_refs,
                confidence,
                created_by,
                created_at
            FROM document_extractions
            WHERE document_id = :document_id
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).mappings().all()

    return ExtractionListResponse(total=total, items=[_to_response(dict(r)) for r in rows])









