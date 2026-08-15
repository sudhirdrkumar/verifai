# Automated Report Generation System Setup

## Overview
This system automatically generates medical reports from AI-extracted claim data, eliminating manual doctor work during report creation.

## Architecture Flow

```
Claim Queue
    ↓
Stage 1: OCR Extraction (AWS Textract)
    ↓
Stage 2: AI Structuring (Gemini 3.5 Flash)
    ↓
Stage 3: AUTO Report Generation (NEW)
    ↓
Report Editor (Doctor Final Review Only)
    ↓
Finalization → Claim Processing
```

## Installation Steps

### 1. Create Database Table
Run the SQL migration to create the medical_reports table:

```bash
# On the EC2 server
PGPASSWORD='Dhoom*2690' psql -h 127.0.0.1 -U postgres -d qc_bkp_modern < /home/ec2-user/qc-python/backend/create_medical_reports_table.sql
```

Or manually:
```sql
-- Create medical_reports table
CREATE TABLE IF NOT EXISTS medical_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id UUID NOT NULL UNIQUE,
    hospital_name VARCHAR(255),
    treating_doctor VARCHAR(255),
    diagnosis TEXT,
    complaints TEXT,
    medicine_used TEXT,
    claim_amount VARCHAR(50),
    conclusion TEXT,
    report_html TEXT,
    report_text TEXT,
    status VARCHAR(50) DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (claim_id) REFERENCES claims(id) ON DELETE CASCADE
);

-- Create indexes
CREATE INDEX idx_medical_reports_claim_id ON medical_reports(claim_id);
CREATE INDEX idx_medical_reports_status ON medical_reports(status);
CREATE INDEX idx_medical_reports_created_at ON medical_reports(created_at DESC);
```

### 2. Deploy New Files

Copy these files to the server:
```bash
# Schema
scp app/schemas/report.py ec2-user@15.207.135.22:/home/ec2-user/qc-python/app/schemas/

# Service
scp app/services/report_generation_service.py ec2-user@15.207.135.22:/home/ec2-user/qc-python/app/services/

# Endpoint
scp app/api/v1/endpoints/reports.py ec2-user@15.207.135.22:/home/ec2-user/qc-python/app/api/v1/endpoints/

# Update Router
scp app/api/router.py ec2-user@15.207.135.22:/home/ec2-user/qc-python/app/api/

# Update Stage 2 Worker
scp backend/stage2_reduction_worker.py ec2-user@15.207.135.22:/home/ec2-user/qc-python/backend/
```

### 3. Restart Services

On the server:
```bash
# Kill old processes
pkill -f stage2_reduction_worker

# Restart backend (it will pick up new endpoints)
# Or for FastAPI auto-reload, it should detect changes automatically

# Restart Stage 2 worker with auto-report generation
cd /home/ec2-user/qc-python
GEMINI_API_KEY=REDACTED
REDIS_HOST='127.0.0.1' \
DATABASE_URL='postgresql://postgres:Dhoom*2690@127.0.0.1:5432/qc_bkp_modern' \
nohup ./.venv/bin/python3 backend/stage2_reduction_worker.py > /tmp/s2.log 2>&1 &
```

## API Endpoints

### 1. Auto-Generate Report
```bash
POST /api/v1/reports/generate/{claim_id}

Response:
{
  "status": "success",
  "claim_id": "51742859",
  "message": "Report auto-generated successfully",
  "report_link": "https://verifai.in/qc/public/report-editor.html?claim_id=...",
  "ready_for_review": true
}
```

### 2. Open Report in Editor
```bash
GET /api/v1/reports/open/{claim_id}

Response:
{
  "status": "ready",
  "claim_id": "51742859",
  "report_url": "https://verifai.in/qc/public/report-editor.html?...",
  "message": "Open report in editor for final review and adjustments"
}
```

### 3. Get Report Data (for populating editor)
```bash
GET /api/v1/reports/{claim_id}/data

Response:
{
  "claim_id": "51742859",
  "status": "generated",
  "ai_extracted_data": {
    "facility": {
      "hospital_name": "Aadya Hospital",
      "treating_doctor": "Dr. Prachi Tiwari Badjatya"
    },
    "clinical": {
      "diagnosis": "Bilateral Bronchopneumonia",
      "complaints": "Respiratory distress",
      "medicine_used": "Not specified"
    },
    "financial": {
      "claim_amount": "30000.00"
    },
    "conclusion": "AI conclusion here"
  },
  "generated_at": "2026-08-15T03:35:10Z"
}
```

### 4. List All Reports
```bash
GET /api/v1/reports/

Response:
{
  "total_auto_generated_reports": 5,
  "reports": [
    {
      "claim_id": "51742859",
      "status": "generated",
      "hospital": "Aadya Hospital",
      "doctor": "Dr. Prachi Tiwari Badjatya",
      "generated_at": "2026-08-15T03:35:10Z",
      "ready_for_doctor_review": true
    }
  ]
}
```

### 5. Finalize Report (Doctor Approval)
```bash
POST /api/v1/reports/{claim_id}/finalize

Response:
{
  "status": "success",
  "claim_id": "51742859",
  "report_status": "finalized",
  "message": "Report finalized by doctor. Ready for claim processing."
}
```

## Workflow

### Current (Before Report Generation)
1. Doctor manually queues claim for extraction
2. Stage 1: AWS Textract extracts OCR text
3. Stage 2: Gemini AI structures data
4. ❌ Doctor must manually create report (takes time)
5. Doctor submits claim

### New (With Automated Reports)
1. ✅ Doctor queues claim for extraction
2. ✅ Stage 1: AWS Textract extracts OCR text
3. ✅ Stage 2: Gemini AI structures data
4. ✅ **Stage 3: AUTO Report Generation (NO doctor action)**
5. Doctor reviews pre-filled report editor (30 seconds)
6. Doctor clicks Finalize
7. ✅ Claim ready for processing

## Key Features

✅ **Automatic Report Generation** - No manual doctor work
✅ **AI-Powered Content** - Uses Gemini extracted data
✅ **Pre-filled Editor** - Doctor just reviews & finalizes
✅ **Status Tracking** - Monitor report generation status
✅ **Integration** - Works with existing report-editor.html
✅ **Zero Doctor Intervention** - Until final review

## Testing

Test the complete workflow:

```bash
# 1. Queue a claim
curl -X POST "http://localhost:8001/api/v1/claims/{claim_id}/process"

# 2. Wait 150 seconds for processing...

# 3. Check if report was auto-generated
curl "http://localhost:8001/api/v1/reports/{claim_id}/data"

# 4. Get link to review in editor
curl "http://localhost:8001/api/v1/reports/open/{claim_id}"

# 5. Doctor reviews and finalizes
curl -X POST "http://localhost:8001/api/v1/reports/{claim_id}/finalize"
```

## Status Codes

- **pending** → Report waiting to be generated
- **generated** → Report auto-generated, ready for doctor review
- **finalized** → Doctor reviewed and approved, ready for claim processing

## Notes

- Reports are automatically generated by Stage 2 worker immediately after Gemini processing
- Doctor only needs to spend 30 seconds reviewing the pre-filled report
- No coding required for doctors - just review and click "Finalize"
- All data comes from ML extraction - no manual data entry
