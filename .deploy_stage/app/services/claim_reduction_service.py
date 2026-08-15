from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.checklist_pipeline import run_claim_checklist_pipeline
from app.services.claim_structuring_service import save_claim_structured_data_fields
from app.services.redis_service import get_redis_client

logger = logging.getLogger(__name__)

CLAIM_REDUCTION_QUEUE = "queue:claim_reduction"
CLAIM_REDUCTION_LOCK_PREFIX = "claim:reduction:queued:"
CLAIM_TRACKER_PREFIX = "claim:tracker:"
CLAIM_FILES_PREFIX = "claim:files:"


def _parse_json_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text_value = str(raw or "").strip()
    if not text_value:
        return {}
    if text_value.startswith("```"):
        text_value = re.sub(r"^```(?:json)?\s*", "", text_value, flags=re.I)
        text_value = re.sub(r"\s*```$", "", text_value).strip()
    try:
        parsed = json.loads(text_value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    start = text_value.find("{")
    end = text_value.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text_value[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _txt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return str(value).strip()


def _join_nonempty(values: list[str], max_len: int = 12000) -> str:
    text_value = "\n\n--- NEXT DOCUMENT RECORD ---\n\n".join([v for v in values if v.strip()]).strip()
    if len(text_value) <= max_len:
        return text_value
    return text_value[:max_len]


def _gemini_generate(prompt: str) -> dict[str, Any]:
    api_key = str(settings.gemini_api_key or "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    try:
        import google.generativeai as genai  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"google-generativeai is not installed: {exc}") from exc

    model_name = str(settings.gemini_flash_model or "gemini-2.5-flash").strip() or "gemini-2.5-flash"
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name=model_name)
    response = model.generate_content(
        prompt,
        generation_config={
            "temperature": 0.1,
            "response_mime_type": "application/json",
        },
    )
    payload = getattr(response, "text", "") or ""
    return _parse_json_payload(payload)


def _build_prompt(claim: dict[str, Any], documents: list[dict[str, Any]]) -> str:
    claim_no = _txt(claim.get("external_claim_id"))
    patient = _txt(claim.get("patient_name"))
    hospital = _txt(claim.get("hospital_name"))
    joined_docs: list[str] = []
    for doc in documents:
        doc_name = _txt(doc.get("file_name"))
        extracted_entities = doc.get("extracted_entities") or {}
        raw_response = doc.get("raw_response") or {}
        evidence_refs = doc.get("evidence_refs") or []
        parts = [
            f"FILE NAME: {doc_name}",
            f"EXTRACTED ENTITIES: {json.dumps(extracted_entities, ensure_ascii=False, default=str)}",
            f"RAW RESPONSE: {json.dumps(raw_response, ensure_ascii=False, default=str)}",
            f"EVIDENCE REFS: {json.dumps(evidence_refs, ensure_ascii=False, default=str)}",
        ]
        joined_docs.append("\n".join(parts))

    combined = _join_nonempty(joined_docs, max_len=24000)
    return (
        "You are VerifAI's claim reducer. Read the full claim packet and return strict JSON only.\n"
        "Combine all files for the same claim into one structured medical-claim summary.\n"
        "Do not repeat the prompt. Do not wrap the JSON in markdown.\n\n"
        "Return this JSON object with these keys:\n"
        "{\n"
        '  "company_name": string,\n'
        '  "claim_type": string,\n'
        '  "insured_name": string,\n'
        '  "hospital_name": string,\n'
        '  "treating_doctor": string,\n'
        '  "treating_doctor_registration_number": string,\n'
        '  "doa": string,\n'
        '  "dod": string,\n'
        '  "diagnosis": string,\n'
        '  "complaints": string,\n'
        '  "findings": string,\n'
        '  "investigation_finding_in_details": string,\n'
        '  "medicine_used": string,\n'
        '  "high_end_antibiotic_for_rejection": string,\n'
        '  "deranged_investigation": string,\n'
        '  "claim_amount": string,\n'
        '  "conclusion": string,\n'
        '  "recommendation": string\n'
        "}\n\n"
        f"Claim number: {claim_no}\n"
        f"Patient: {patient}\n"
        f"Hospital: {hospital}\n\n"
        f"Combined documents:\n{combined}"
    )


def _normalize_fields(payload: dict[str, Any], claim: dict[str, Any]) -> dict[str, str]:
    def pick(*keys: str, default: str = "-") -> str:
        for key in keys:
            value = _txt(payload.get(key))
            if value and value != "-":
                return value
        return default

    company_name = pick("company_name", default="Medi Assist Insurance TPA Pvt. Ltd.")
    claim_type = pick("claim_type", default=_txt(claim.get("claim_type")) or "-")
    insured_name = pick("insured_name", default=_txt(claim.get("patient_name")) or "-")
    diagnosis = pick("diagnosis", default="-")
    recommendation = pick("recommendation", default="QUERY")

    return {
        "company_name": company_name,
        "claim_type": claim_type,
        "insured_name": insured_name,
        "hospital_name": pick("hospital_name", default="-"),
        "treating_doctor": pick("treating_doctor", default="-"),
        "treating_doctor_registration_number": pick("treating_doctor_registration_number", default="-"),
        "doa": pick("doa", default="-"),
        "dod": pick("dod", default="-"),
        "diagnosis": diagnosis,
        "complaints": pick("complaints", default="-"),
        "findings": pick("findings", default="-"),
        "investigation_finding_in_details": pick("investigation_finding_in_details", default="-"),
        "medicine_used": pick("medicine_used", default="-"),
        "high_end_antibiotic_for_rejection": pick("high_end_antibiotic_for_rejection", default="No"),
        "deranged_investigation": pick("deranged_investigation", default="-"),
        "claim_amount": pick("claim_amount", default="-"),
        "conclusion": pick("conclusion", default="-"),
        "recommendation": recommendation,
    }


def _collect_claim_documents(db, claim_id: UUID) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    claim = db.execute(
        text(
            """
            SELECT id, external_claim_id, patient_name, patient_identifier, status, tags, claim_type, hospital_name
            FROM claims
            WHERE id = :claim_id
            LIMIT 1
            """
        ),
        {"claim_id": str(claim_id)},
    ).mappings().first()
    if not claim:
        raise RuntimeError(f"claim not found: {claim_id}")

    docs = db.execute(
        text(
            """
            SELECT
                cd.id,
                cd.file_name,
                cd.mime_type,
                cd.uploaded_at,
                COALESCE(de.extracted_entities, '{}'::jsonb) AS extracted_entities,
                COALESCE(de.raw_response, '{}'::jsonb) AS raw_response,
                COALESCE(de.evidence_refs, '[]'::jsonb) AS evidence_refs
            FROM claim_documents cd
            LEFT JOIN LATERAL (
                SELECT extracted_entities, raw_response, evidence_refs
                FROM document_extractions de
                WHERE de.document_id = cd.id
                ORDER BY de.created_at DESC
                LIMIT 1
            ) de ON TRUE
            WHERE cd.claim_id = :claim_id
            ORDER BY cd.uploaded_at ASC, cd.created_at ASC
            """
        ),
        {"claim_id": str(claim_id)},
    ).mappings().all()

    documents: list[dict[str, Any]] = []
    for row in docs:
        documents.append(
            {
                "id": str(row["id"]),
                "file_name": row["file_name"],
                "mime_type": row["mime_type"],
                "extracted_entities": row["extracted_entities"] if isinstance(row["extracted_entities"], dict) else {},
                "raw_response": row["raw_response"] if isinstance(row["raw_response"], dict) else {},
                "evidence_refs": row["evidence_refs"] if isinstance(row["evidence_refs"], list) else [],
            }
        )
    return dict(claim), documents


def maybe_queue_claim_reduction(claim_id: UUID, document_id: UUID | None = None) -> bool:
    redis_client = get_redis_client()
    if redis_client is None:
        return False

    with SessionLocal() as db:
        total = int(
            db.execute(
                text("SELECT COUNT(*) FROM claim_documents WHERE claim_id = :claim_id"),
                {"claim_id": str(claim_id)},
            ).scalar_one()
            or 0
        )
        completed = int(
            db.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM claim_documents
                    WHERE claim_id = :claim_id AND parse_status = 'succeeded'
                    """
                ),
                {"claim_id": str(claim_id)},
            ).scalar_one()
            or 0
        )

    tracker_key = f"{CLAIM_TRACKER_PREFIX}{claim_id}"
    files_key = f"{CLAIM_FILES_PREFIX}{claim_id}"
    redis_client.hset(tracker_key, mapping={"total": total, "completed": completed, "updated_at": datetime.now(timezone.utc).isoformat()})
    if document_id is not None:
        redis_client.sadd(files_key, str(document_id))

    if total <= 0 or completed < total:
        return False

    lock_key = f"{CLAIM_REDUCTION_LOCK_PREFIX}{claim_id}"
    if not redis_client.set(lock_key, "1", nx=True, ex=86400):
        return False

    redis_client.lpush(CLAIM_REDUCTION_QUEUE, str(claim_id))
    return True


class ClaimReductionService:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        if not str(settings.gemini_api_key or "").strip():
            logger.info("Claim reduction worker not started: Redis or Gemini API key missing")
            return
        self._stop_event.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="claim-reduction-worker", daemon=True)
        self._worker.start()
        self._started = True
        logger.info("Claim reduction worker started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=10)
        self._started = False
        logger.info("Claim reduction worker stopped")

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                redis_client = get_redis_client()
                if redis_client is None:
                    time.sleep(5)
                    continue
                item = redis_client.brpop(CLAIM_REDUCTION_QUEUE, timeout=5)
                if not item:
                    continue
                _, raw_claim_id = item
                claim_id = UUID(str(raw_claim_id))
                self._process_claim(claim_id)
            except Exception as exc:
                logger.warning("Claim reduction worker error: %s", exc)
                time.sleep(2)

    def _process_claim(self, claim_id: UUID) -> None:
        redis_client = get_redis_client()
        if redis_client is None:
            return

        with SessionLocal() as db:
            claim, documents = _collect_claim_documents(db, claim_id)
            if not documents:
                return

        prompt = _build_prompt(claim, documents)
        payload = _gemini_generate(prompt)
        fields = _normalize_fields(payload, claim)
        raw_payload = {
            "source": "gemini_flash_reducer",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "claim": claim,
            "documents": documents,
            "model": str(settings.gemini_flash_model or "gemini-2.5-flash"),
            "gemini_payload": payload,
        }

        with SessionLocal() as db:
            saved = save_claim_structured_data_fields(
                db=db,
                claim_id=claim_id,
                actor_id="system:gemini-reducer",
                fields=fields,
                source="gemini_flash_reducer",
                confidence=0.72,
                raw_payload=raw_payload,
            )
            db.commit()

        with SessionLocal() as db:
            try:
                run_claim_checklist_pipeline(
                    db=db,
                    claim_id=claim_id,
                    actor_id="system:gemini-reducer",
                    force_source_refresh=False,
                )
                db.commit()
            except Exception as exc:
                logger.warning("Claim checklist refresh failed for %s: %s", claim_id, exc)

        redis_client.hset(
            f"{CLAIM_TRACKER_PREFIX}{claim_id}",
            mapping={
                "reduced_at": datetime.now(timezone.utc).isoformat(),
                "reduced_by": "gemini_flash_reducer",
                "saved_claim_id": str(saved.get("claim_id")) if isinstance(saved, dict) else str(claim_id),
            },
        )
        logger.info("Claim reduction completed for claim_id=%s", claim_id)


claim_reduction_service = ClaimReductionService()
