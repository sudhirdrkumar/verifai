import json

from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.claim_structuring_service import _heuristic_fields, _load_context


def main() -> None:
    claim_external = "49735039"
    db = SessionLocal()
    try:
        claim = db.execute(
            text(
                """
                SELECT id, external_claim_id, updated_at
                FROM claims
                WHERE external_claim_id = :external_claim_id
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ),
            {"external_claim_id": claim_external},
        ).mappings().first()
        print("CLAIM", json.dumps({k: str(v) for k, v in (claim or {}).items()}, default=str))
        if not claim:
            return

        claim_id = str(claim["id"])
        rows = db.execute(
            text(
                """
                SELECT
                    id,
                    document_id,
                    created_at,
                    COALESCE(extracted_entities::text, '') AS extracted_entities,
                    COALESCE(evidence_refs::text, '') AS evidence_refs
                FROM document_extractions
                WHERE claim_id = :claim_id
                ORDER BY created_at DESC
                LIMIT 3
                """
            ),
            {"claim_id": claim_id},
        ).mappings().all()
        print("EXTRACTION_COUNT", len(rows))
        for idx, row in enumerate(rows, 1):
            print(f"--- EXTRACTION #{idx} ---")
            print("id", str(row.get("id") or ""))
            print("document_id", str(row.get("document_id") or ""))
            print("created_at", str(row.get("created_at") or ""))
            entities = str(row.get("extracted_entities") or "")
            evid = str(row.get("evidence_refs") or "")
            print("entities_head", entities[:5000])
            print("evidence_head", evid[:2000])

        ctx = _load_context(db, claim["id"])
        heur = _heuristic_fields(ctx)
        print("HEURISTIC_FIELDS", json.dumps(heur, default=str))
    finally:
        db.close()


if __name__ == "__main__":
    main()
