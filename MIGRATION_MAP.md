# QC-BKP Legacy -> Modern Data Mapping

This mapping follows `QC-BKP_Modernization_Blueprint.pdf` and helps migrate the old
`C:\xampp\htdocs\QC-BKP\sql\schema.sql` model to the new PostgreSQL schema in `db/schema.sql`.

## Core mapping

- `users` -> `users` (single login roles: super_admin/doctor/user)
- `auth_logs` -> `auth_logs`
- `excel_case_uploads` + `case_assignments` -> `claims`
- `case_documents` -> `claim_documents`
- `openai_analysis_results` -> `decision_results`
- `openai_analysis_jobs` -> `workflow_events` (job lifecycle events)
- `openai_claim_rules` -> `openai_claim_rules`
- `openai_diagnosis_criteria` -> `openai_diagnosis_criteria`
- exported report content -> `report_versions`
- QC override/outcome signals -> `feedback_labels`
- prompt/rule governance tables -> `rule_registry`
- model selection/versioning metadata -> `model_registry`

## Why this redesign

- Keeps deterministic rules and model outputs separately traceable
- Adds immutable event history for auditability (`workflow_events`)
- Supports decision packets (`decision_results.decision_payload`) for explainable automation
- Prepares training labels and learning loop via `feedback_labels`

## Database creation and migration

Run from `C:\QC-Python` after PostgreSQL is running:

```powershell
python scripts/create_database.py
python scripts/migrate_qc_kp.py
```

If PostgreSQL or legacy MySQL is not running, the scripts exit with clear connection messages.
