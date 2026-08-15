-- Create medical_reports table for storing generated reports
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

-- Create index for faster lookups
CREATE INDEX IF NOT EXISTS idx_medical_reports_claim_id ON medical_reports(claim_id);
CREATE INDEX IF NOT EXISTS idx_medical_reports_status ON medical_reports(status);
CREATE INDEX IF NOT EXISTS idx_medical_reports_created_at ON medical_reports(created_at DESC);
