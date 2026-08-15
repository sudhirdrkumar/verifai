from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import text
import logging
from typing import Optional
import json

logger = logging.getLogger(__name__)


class ReportGenerationService:

    @staticmethod
    def generate_html_report(structured_data: dict) -> str:
        """Generate a professional HTML medical report from structured data."""

        hospital = structured_data.get('hospital_name', 'Not Specified')
        doctor = structured_data.get('treating_doctor', 'Not Specified')
        diagnosis = structured_data.get('diagnosis', 'Not Specified')
        complaints = structured_data.get('complaints', 'Not Specified')
        medicine = structured_data.get('medicine_used', 'Not Specified')
        claim_amount = structured_data.get('claim_amount', 'Not Specified')
        conclusion = structured_data.get('conclusion', 'Not Specified')

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; color: #333; }}
                .header {{ background: #2c3e50; color: white; padding: 20px; border-radius: 5px; }}
                .report-title {{ font-size: 24px; font-weight: bold; margin-bottom: 5px; }}
                .report-date {{ font-size: 12px; color: #ecf0f1; }}
                .section {{ margin: 20px 0; padding: 15px; background: #f8f9fa; border-left: 4px solid #3498db; }}
                .section-title {{ font-size: 14px; font-weight: bold; color: #2c3e50; margin-bottom: 10px; }}
                .field {{ margin: 10px 0; }}
                .field-label {{ font-weight: bold; color: #34495e; width: 150px; display: inline-block; }}
                .field-value {{ color: #555; }}
                .conclusion {{ background: #d5f4e6; padding: 15px; border-radius: 5px; border-left: 4px solid #27ae60; }}
                .footer {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid #bdc3c7; font-size: 12px; color: #7f8c8d; }}
                .ai-badge {{ display: inline-block; background: #9b59b6; color: white; padding: 5px 10px; border-radius: 3px; font-size: 11px; }}
            </style>
        </head>
        <body>
            <div class="header">
                <div class="report-title">🏥 Medical Claim Report</div>
                <div class="report-date">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
                <div class="ai-badge">AI-Generated Report</div>
            </div>

            <div class="section">
                <div class="section-title">📋 Facility & Provider Information</div>
                <div class="field">
                    <span class="field-label">Hospital:</span>
                    <span class="field-value">{hospital}</span>
                </div>
                <div class="field">
                    <span class="field-label">Treating Doctor:</span>
                    <span class="field-value">{doctor}</span>
                </div>
            </div>

            <div class="section">
                <div class="section-title">🔬 Clinical Details</div>
                <div class="field">
                    <span class="field-label">Diagnosis:</span>
                    <span class="field-value">{diagnosis}</span>
                </div>
                <div class="field">
                    <span class="field-label">Complaints:</span>
                    <span class="field-value">{complaints}</span>
                </div>
                <div class="field">
                    <span class="field-label">Medications:</span>
                    <span class="field-value">{medicine}</span>
                </div>
            </div>

            <div class="section">
                <div class="section-title">💰 Claim Details</div>
                <div class="field">
                    <span class="field-label">Claim Amount:</span>
                    <span class="field-value">₹{claim_amount}</span>
                </div>
            </div>

            <div class="conclusion">
                <div class="section-title">✅ AI Conclusion</div>
                <div class="field-value">{conclusion}</div>
            </div>

            <div class="footer">
                <p>This is an AI-generated report based on OCR extraction and Gemini AI analysis.</p>
                <p>Please review for accuracy before approving the claim.</p>
            </div>
        </body>
        </html>
        """
        return html

    @staticmethod
    def generate_text_report(structured_data: dict) -> str:
        """Generate a plain text medical report from structured data."""

        hospital = structured_data.get('hospital_name', 'Not Specified')
        doctor = structured_data.get('treating_doctor', 'Not Specified')
        diagnosis = structured_data.get('diagnosis', 'Not Specified')
        complaints = structured_data.get('complaints', 'Not Specified')
        medicine = structured_data.get('medicine_used', 'Not Specified')
        claim_amount = structured_data.get('claim_amount', 'Not Specified')
        conclusion = structured_data.get('conclusion', 'Not Specified')

        text_report = f"""
{'='*70}
MEDICAL CLAIM REPORT
{'='*70}
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Report Type: AI-Generated Report
{'='*70}

FACILITY & PROVIDER INFORMATION
{'-'*70}
Hospital:          {hospital}
Treating Doctor:   {doctor}

CLINICAL DETAILS
{'-'*70}
Diagnosis:         {diagnosis}
Complaints:        {complaints}
Medications:       {medicine}

CLAIM DETAILS
{'-'*70}
Claim Amount:      ₹{claim_amount}

AI CONCLUSION
{'-'*70}
{conclusion}

{'='*70}
Note: This report was generated using AI analysis of OCR-extracted
medical documents. Please review for accuracy before approving.
{'='*70}
        """.strip()

        return text_report

    @staticmethod
    def create_report_for_claim(db: Session, claim_id: str) -> Optional[dict]:
        """Create and save report for a claim based on structured data."""

        try:
            # Fetch structured data for the claim
            result = db.execute(text("""
                SELECT
                    id, claim_id, hospital_name, treating_doctor, diagnosis,
                    complaints, medicine_used, claim_amount, conclusion
                FROM claim_structured_data
                WHERE claim_id = :claim_id
                LIMIT 1
            """), {"claim_id": claim_id}).mappings().first()

            if not result:
                logger.warning(f"No structured data found for claim {claim_id}")
                return None

            structured_data = {
                'hospital_name': result.get('hospital_name'),
                'treating_doctor': result.get('treating_doctor'),
                'diagnosis': result.get('diagnosis'),
                'complaints': result.get('complaints'),
                'medicine_used': result.get('medicine_used'),
                'claim_amount': result.get('claim_amount'),
                'conclusion': result.get('conclusion'),
            }

            # Generate reports
            html_report = ReportGenerationService.generate_html_report(structured_data)
            text_report = ReportGenerationService.generate_text_report(structured_data)

            # Save to database
            db.execute(text("""
                INSERT INTO medical_reports (
                    claim_id, hospital_name, treating_doctor, diagnosis,
                    complaints, medicine_used, claim_amount, conclusion,
                    report_html, report_text, status, created_at, updated_at
                )
                VALUES (
                    :claim_id, :hospital_name, :treating_doctor, :diagnosis,
                    :complaints, :medicine_used, :claim_amount, :conclusion,
                    :report_html, :report_text, 'generated', NOW(), NOW()
                )
                ON CONFLICT (claim_id) DO UPDATE SET
                    report_html = EXCLUDED.report_html,
                    report_text = EXCLUDED.report_text,
                    status = 'generated',
                    updated_at = NOW()
            """), {
                'claim_id': claim_id,
                'hospital_name': structured_data['hospital_name'],
                'treating_doctor': structured_data['treating_doctor'],
                'diagnosis': structured_data['diagnosis'],
                'complaints': structured_data['complaints'],
                'medicine_used': structured_data['medicine_used'],
                'claim_amount': structured_data['claim_amount'],
                'conclusion': structured_data['conclusion'],
                'report_html': html_report,
                'report_text': text_report,
            })

            db.commit()
            logger.info(f"✅ Report generated for claim {claim_id}")

            return {
                'claim_id': claim_id,
                'status': 'generated',
                'html_preview': html_report[:200] + '...',
                'message': 'Report generated successfully'
            }

        except Exception as e:
            logger.error(f"Error generating report for claim {claim_id}: {str(e)}", exc_info=True)
            db.rollback()
            return None


    @staticmethod
    def create_report_for_claim_direct(cur, claim_id: str, structured_json: dict) -> Optional[dict]:
        """Create and save report directly from structured JSON (used by Stage 2 worker)."""

        try:
            structured_data = {
                'hospital_name': structured_json.get('hospital_name', 'Not Specified'),
                'treating_doctor': structured_json.get('treating_doctor', 'Not Specified'),
                'diagnosis': structured_json.get('diagnosis', 'Not Specified'),
                'complaints': structured_json.get('complaints', 'Not Specified'),
                'medicine_used': structured_json.get('medicine_used', 'Not Specified'),
                'claim_amount': structured_json.get('claim_amount', 'Not Specified'),
                'conclusion': structured_json.get('conclusion', 'Not Specified'),
            }

            # Generate reports
            html_report = ReportGenerationService.generate_html_report(structured_data)
            text_report = ReportGenerationService.generate_text_report(structured_data)

            # Save to database using existing cursor
            cur.execute("""
                INSERT INTO medical_reports (
                    claim_id, hospital_name, treating_doctor, diagnosis,
                    complaints, medicine_used, claim_amount, conclusion,
                    report_html, report_text, status, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (claim_id) DO UPDATE SET
                    report_html = EXCLUDED.report_html,
                    report_text = EXCLUDED.report_text,
                    status = 'generated',
                    updated_at = NOW()
            """, (
                claim_id,
                structured_data['hospital_name'],
                structured_data['treating_doctor'],
                structured_data['diagnosis'],
                structured_data['complaints'],
                structured_data['medicine_used'],
                structured_data['claim_amount'],
                structured_data['conclusion'],
                html_report,
                text_report,
                'generated'
            ))

            logger.info(f"✅ Report auto-generated for claim {claim_id}")
            return {'claim_id': claim_id, 'status': 'generated'}

        except Exception as e:
            logger.error(f"Error auto-generating report for claim {claim_id}: {str(e)}", exc_info=True)
            return None


# Singleton instance
report_service = ReportGenerationService()
