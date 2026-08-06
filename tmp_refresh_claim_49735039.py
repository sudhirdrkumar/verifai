import json
from uuid import UUID

from sqlalchemy import text

from app.db.session import SessionLocal
from app.schemas.extraction import ExtractionProvider
from app.services.claim_structuring_service import generate_claim_structured_data
from app.services.extractions_service import run_document_extraction


def main() -> None:
    external_claim_id = "49735039"
    actor_id = "sudhir"

    db = SessionLocal()
    try:
        claim = db.execute(
            text(
                """
                SELECT id
                FROM claims
                WHERE external_claim_id = :external_claim_id
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ),
            {"external_claim_id": external_claim_id},
        ).mappings().first()
        if not claim:
            print("NO_CLAIM")
            return

        claim_id = UUID(str(claim["id"]))
        doc = db.execute(
            text(
                """
                SELECT id
                FROM claim_documents
                WHERE claim_id = :claim_id
                ORDER BY uploaded_at DESC
                LIMIT 1
                """
            ),
            {"claim_id": str(claim_id)},
        ).mappings().first()
        if not doc:
            print("NO_DOCUMENT")
            return

        document_id = UUID(str(doc["id"]))
        extraction = run_document_extraction(
            db=db,
            claim_id=claim_id,
            document_id=document_id,
            provider=ExtractionProvider.auto,
            actor_id=actor_id,
            force_refresh=True,
        )
        structured = generate_claim_structured_data(
            db=db,
            claim_id=claim_id,
            actor_id=actor_id,
            use_llm=True,
            force_refresh=True,
        )
        db.commit()

        print(
            json.dumps(
                {
                    "extraction_provider": extraction.provider,
                    "extraction_model": extraction.model_name,
                    "structured_source": structured.get("source"),
                    "structured_company_name": structured.get("company_name"),
                    "structured_insured_name": structured.get("insured_name"),
                    "structured_hospital_name": structured.get("hospital_name"),
                    "structured_treating_doctor": structured.get("treating_doctor"),
                    "structured_treating_doctor_registration_number": structured.get(
                        "treating_doctor_registration_number"
                    ),
                    "llm_error": (structured.get("raw_payload") or {}).get("llm_error"),
                },
                default=str,
                ensure_ascii=False,
            )
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
