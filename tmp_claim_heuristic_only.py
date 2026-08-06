import json

from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.claim_structuring_service import _find_values, _heuristic_fields, _load_context


def main() -> None:
    db = SessionLocal()
    try:
        claim_id = db.execute(
            text(
                """
                SELECT id
                FROM claims
                WHERE external_claim_id = :external_claim_id
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ),
            {"external_claim_id": "49735039"},
        ).scalar()
        if not claim_id:
            print("NO_CLAIM")
            return
        ctx = _load_context(db, claim_id)
        entity_docs = ctx.get("entity_docs") if isinstance(ctx.get("entity_docs"), list) else []
        print("INSURED_VALUES", json.dumps(_find_values(entity_docs, ["insured_name", "name", "patient_name", "insured", "beneficiary", "policy_holder_name"], 10), ensure_ascii=False))
        print("HOSPITAL_VALUES", json.dumps(_find_values(entity_docs, ["hospital_name", "hospital", "provider_hospital", "treating_hospital", "facility_name"], 10), ensure_ascii=False))
        print("DOCTOR_VALUES", json.dumps(_find_values(entity_docs, ["treating_doctor", "treating_doctor_name", "doctor_name", "attending_doctor", "consultant_doctor", "consulting_doctor", "admit_doctor", "admit_dr"], 10), ensure_ascii=False))
        print("REG_VALUES", json.dumps(_find_values(entity_docs, ["doctor_registration_number", "treating_doctor_registration_number", "doctor_reg_no", "registration_no", "registration_number", "mci_reg_no", "nmc_reg_no", "doctor_registration"], 10), ensure_ascii=False))
        heur = _heuristic_fields(ctx)
        print(json.dumps(heur, ensure_ascii=False))
    finally:
        db.close()


if __name__ == "__main__":
    main()
