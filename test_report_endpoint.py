import sys
sys.path.insert(0, '/home/ec2-user/qc-python')

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

engine = create_engine('postgresql://verifai:yYv5Ny7outZG7XKrgEJ8JUxJ@127.0.0.1:5432/qc_bkp_modern')
Session = sessionmaker(bind=engine)
db = Session()

claim_id = '139bc18c-b7b7-4491-a676-6563165fb8a0'

try:
    # Check for auto-generated Stage 3 report first
    auto_report = db.execute(
        text('''
            SELECT
                c.id AS claim_id,
                c.external_claim_id,
                mr.report_text,
                mr.status,
                mr.created_at
            FROM claims c
            JOIN medical_reports mr ON mr.claim_id = c.id
            WHERE c.id = :claim_id
            ORDER BY mr.created_at DESC
            LIMIT 1
        '''),
        {"claim_id": claim_id},
    ).mappings().first()

    print(f'auto_report type: {type(auto_report)}')

    if auto_report and auto_report.get("report_text"):
        # Format auto-generated report as HTML
        report_text = auto_report.get("report_text", "")
        # HTML escape the text
        report_text_escaped = report_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        report_html = f"<pre>{report_text_escaped}</pre>"
        row = {
            "claim_id": auto_report.get("claim_id"),
            "external_claim_id": auto_report.get("external_claim_id"),
            "version_no": 0,
            "report_html": report_html,
            "report_status": "completed",
            "created_by": "system",
            "report_source": "system",
            "created_at": auto_report.get("created_at"),
        }
        print('✅ Auto-report formatted successfully')
        print(f'report_html length: {len(report_html)}')
        print('SUCCESS')
    else:
        print('❌ No auto-report found')

except Exception as e:
    print(f'ERROR: {type(e).__name__}: {e}')
    import traceback
    traceback.print_exc()

db.close()
