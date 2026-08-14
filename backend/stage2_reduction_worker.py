import os
import json
import redis
import psycopg2
import google.generativeai as genai
import logging
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DB_DSN = os.getenv('DATABASE_URL', "postgresql://admin:localpassword@127.0.0.1:5432/qc_bkp_modern_live")
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
genai.configure(api_key=GEMINI_API_KEY)

class ClaimStructureSchema(BaseModel):
    company_name: str = Field(default="", description="Insurance provider name")
    hospital_name: str = Field(default="", description="Hospital name")
    treating_doctor: str = Field(default="", description="Treating physician")
    admission_date: str = Field(default="", description="Admission date")
    discharge_date: str = Field(default="", description="Discharge date")
    diagnoses: list[str] = Field(default_factory=list, description="Medical diagnoses")
    total_billing_amount: float = Field(default=0.0, description="Total billing amount")
    clinical_narrative: str = Field(default="", description="Clinical overview")

def run_stage2_loop():
    logger.info("🧠 Stage 2 Synthesis Reducer Active")
    model = genai.GenerativeModel(model_name='gemini-2.5-flash')

    while True:
        try:
            _, raw_task = r.brpop("queue:stage2_claim_reduction", timeout=30)
            if not raw_task:
                continue

            task = json.loads(raw_task)
            claim_id = task['claim_id']

            logger.info(f"Processing Claim {claim_id} reduction")

            try:
                # Fetch all OCR texts for this claim
                conn = psycopg2.connect(DB_DSN)
                cur = conn.cursor()
                cur.execute("""
                    SELECT de.raw_response
                    FROM document_extractions de
                    JOIN claim_documents cd ON de.document_id = cd.id
                    WHERE cd.claim_id = %s
                    ORDER BY de.created_at ASC
                """, (claim_id,))

                rows = cur.fetchall()
                combined_text = "\n\n--- NEXT DOCUMENT ---\n\n".join([r[0] for r in rows if r[0]])

                if not combined_text.strip():
                    logger.warning(f"No text found for claim {claim_id}")
                    cur.close()
                    conn.close()
                    continue

                # Call Gemini for synthesis
                prompt = f"""You are a medical claims data synthesizer. Below are OCR texts from multiple documents for a single claim.
Extract and consolidate the following information into a structured JSON format.
De-duplicate diagnoses and aggregate billing amounts.

Combined Claim Documents:
{combined_text}

Return a JSON object with these fields: company_name, hospital_name, treating_doctor, admission_date, discharge_date, diagnoses (list), total_billing_amount, clinical_narrative."""

                result = model.generate_content(
                    prompt,
                    generation_config={"response_mime_type": "application/json", "response_schema": ClaimStructureSchema}
                )

                final_json = json.loads(result.text)

                # Save to database
                cur.execute("""
                    INSERT INTO claim_structured_data (claim_id, normalized_fields, created_at, updated_at)
                    VALUES (%s, %s, NOW(), NOW())
                    ON CONFLICT (claim_id) DO UPDATE SET normalized_fields = EXCLUDED.normalized_fields, updated_at = NOW()
                """, (claim_id, json.dumps(final_json)))

                conn.commit()
                cur.close()
                conn.close()

                logger.info(f"🎉 Claim {claim_id} consolidated successfully")

            except Exception as e:
                logger.error(f"❌ Stage 2 Error on Claim {claim_id}: {str(e)}")

        except Exception as e:
            logger.error(f"Worker loop error: {str(e)}")

if __name__ == "__main__":
    run_stage2_loop()
