from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

engine = create_engine('postgresql://verifai:yYv5Ny7outZG7XKrgEJ8JUxJ@127.0.0.1:5432/qc_bkp_modern')
Session = sessionmaker(bind=engine)
db = Session()

claim_id = '139bc18c-b7b7-4491-a676-6563165fb8a0'

# Check for auto report
auto_report_row = db.execute(
    text("SELECT report_text FROM medical_reports WHERE claim_id = :claim_id ORDER BY created_at DESC LIMIT 1"),
    {"claim_id": claim_id},
).first()

print("=== Auto-Report Check ===")
if auto_report_row:
    print(f'✅ Auto-report found: {len(auto_report_row[0])} chars')
    print(f'First 150 chars: {auto_report_row[0][:150]}')
else:
    print('❌ No auto-report found')

print("\n=== Old Report Check ===")
# Check for old report
old_report_row = db.execute(
    text("SELECT report_markdown FROM report_versions WHERE claim_id = :claim_id ORDER BY version_no DESC LIMIT 1"),
    {"claim_id": claim_id},
).first()

if old_report_row and old_report_row[0]:
    print(f'✅ Old report found: {len(old_report_row[0])} chars')
    print(f'Starts with: {old_report_row[0][:100]}')
else:
    print('❌ No old report found')

db.close()
