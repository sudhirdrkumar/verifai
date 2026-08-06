from uuid import UUID
import re

from sqlalchemy import text
from sqlalchemy.orm import Session


def parse_assigned_doctors(value: str | None) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part and part.strip()]


def _normalize_doctor_token(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "", raw)


def doctor_matches_assignment(assigned_doctor_id: str | None, doctor_username: str) -> bool:
    doctors = parse_assigned_doctors(assigned_doctor_id)
    target = _normalize_doctor_token(doctor_username)
    if not target:
        return False
    return any(_normalize_doctor_token(doc) == target for doc in doctors)


def doctor_can_access_claim(db: Session, claim_id: UUID, doctor_username: str) -> bool | None:
    row = db.execute(
        text("SELECT assigned_doctor_id FROM claims WHERE id = :claim_id"),
        {"claim_id": str(claim_id)},
    ).mappings().first()
    if row is None:
        return None
    assigned = row.get("assigned_doctor_id")
    return doctor_matches_assignment(str(assigned or ""), doctor_username)


def doctor_can_access_document(db: Session, document_id: UUID, doctor_username: str) -> bool | None:
    row = db.execute(
        text(
            """
            SELECT c.assigned_doctor_id
            FROM claim_documents d
            JOIN claims c ON c.id = d.claim_id
            WHERE d.id = :document_id
            """
        ),
        {"document_id": str(document_id)},
    ).mappings().first()
    if row is None:
        return None
    assigned = row.get("assigned_doctor_id")
    return doctor_matches_assignment(str(assigned or ""), doctor_username)