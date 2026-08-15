# QC-BKP Modernization Scaffold (Python)

This project is a modernization foundation for `QC-BKP`, built from the
`QC-BKP_Modernization_Blueprint.pdf`.

## What this includes

- FastAPI service skeleton (`app/`) for a modular Python backend
- New PostgreSQL schema aligned with the blueprint decision-intelligence model
- One-command database bootstrap script (`scripts/create_database.py`)

## Project structure

- `app/main.py`: FastAPI app entrypoint
- `app/api/v1/endpoints/health.py`: API health endpoint
- `app/core/config.py`: environment-based configuration
- `app/db/session.py`: SQLAlchemy engine/session utilities
- `db/schema.sql`: new governed PostgreSQL schema
- `db/seed.sql`: initial seed records for registry tables
- `scripts/create_database.py`: create DB + apply schema + apply seeds

- `MIGRATION_MAP.md`: legacy MySQL-to-modern PostgreSQL mapping

## Setup

1. Create environment file:

   ```powershell
   Copy-Item .env.example .env
   ```

2. Update database credentials in `.env`.

3. Install dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

4. Create and initialize the new database:

   ```powershell
   python scripts/create_database.py
   ```

5. Run the API:

   ```powershell
   uvicorn app.main:app --reload
   ```

6. Verify:

   - Root: `http://127.0.0.1:8000/`
   - Docs: `http://127.0.0.1:8000/docs`
   - Health: `http://127.0.0.1:8000/api/v1/health`

## New core DB tables

- `claims`
- `claim_documents`
- `document_extractions`
- `decision_results`
- `report_versions`
- `workflow_events`
- `feedback_labels`
- `model_registry`
- `rule_registry`

These map directly to the blueprint's target service/data model (claims, documents,
extraction, decision packet, reporting, governance, and feedback loops).


## API endpoints (current)

- `GET /api/v1/health`
- `POST /api/v1/claims`
- `GET /api/v1/claims`
- `GET /api/v1/claims/{claim_id}`
- `PATCH /api/v1/claims/{claim_id}/status`
- `PATCH /api/v1/claims/{claim_id}/assign`


## S3 configuration

Set these in `.env` to use the same AWS/S3 bucket as legacy QC-BKP:

- `S3_REGION`
- `S3_BUCKET`
- `S3_ACCESS_KEY`
- `S3_SECRET_KEY`
- optional `S3_ENDPOINT_URL` for S3-compatible storage


## API endpoints (documents)

- `POST /api/v1/claims/{claim_id}/documents`
- `GET /api/v1/claims/{claim_id}/documents`
- `PATCH /api/v1/documents/{document_id}/parse-status`
- `GET /api/v1/documents/{document_id}/download-url`


## Extraction pipeline

- `POST /api/v1/documents/{document_id}/extract` with provider `auto | openai | local`
- `GET /api/v1/documents/{document_id}/extractions`

`auto` uses OpenAI when `OPENAI_API_KEY` is configured; otherwise falls back to local extraction.


## OCR-first preprocessing

Extraction now normalizes text in this order:

1. UTF-8 text decode for text-like files
2. PDF text extraction (embedded text)
3. OCR fallback (OCR.Space) for image/scanned docs

Configure OCR with `OCR_SPACE_API_KEY`, optional `OCR_SPACE_ENDPOINT`, and `OCR_SPACE_ENGINE` (default `2`, auto-fallback to `1` for tiny images).


## Monitor UI

Open `http://127.0.0.1:8000/monitor` to run concurrent health/API checks and a full smoke workflow from one frontend page.


## Claim checklist pipeline

- `POST /api/v1/claims/{claim_id}/checklist/evaluate`
- `GET /api/v1/claims/{claim_id}/checklist/latest`
- `POST /api/v1/checklist/ml/train` (super_admin, retrains supervised ML model)

Checklist source priority:
1. modern PostgreSQL checklist tables (`openai_claim_rules`, `openai_diagnosis_criteria`)
2. legacy QC-BKP MySQL checklist tables
3. built-in fallback checklist catalog

Set legacy DB credentials in `.env` via `LEGACY_DB_*` variables.

## Single login + role model

This app now uses one login flow with three roles, aligned with the legacy QC app and blueprint guidance:

- `super_admin`
- `doctor`
- `user` (operations)

Protected APIs require a bearer token from login.

### Auth endpoints

- `POST /api/v1/auth/login`
- `POST /api/v1/auth/logout`
- `GET /api/v1/auth/me`
- `POST /api/v1/auth/change-password`
- `GET /api/v1/auth/users` (super_admin only)
- `POST /api/v1/auth/users` (super_admin only)
- `GET /api/v1/auth/ifsc/verify/{ifsc_code}` (super_admin only)
- `GET /api/v1/auth/gst/verify/{gstin}` (authenticated roles)

### Optional bootstrap admin

Set in `.env` before running `scripts/create_database.py`:

- `BOOTSTRAP_ADMIN_USERNAME`
- `BOOTSTRAP_ADMIN_PASSWORD`


## Legacy QC-KP migration

Run after schema bootstrap to migrate data from legacy MySQL (`QC-KP`) into modern PostgreSQL:

```powershell
python scripts/migrate_qc_kp.py
```

Optional flags:

- `--skip-auth`
- `--skip-claims`
- `--skip-checklist`

Migrated groups:

- `users` and `auth_logs`
- `excel_case_uploads` -> `claims`
- `case_documents` -> `claim_documents`
- `openai_analysis_results` -> `decision_results`
- `openai_analysis_jobs` -> `workflow_events`
- `openai_claim_rules` and `openai_diagnosis_criteria` -> modern checklist tables

The checklist pipeline now reads checklist catalog from modern PostgreSQL first, then legacy MySQL, then seed fallback.

Train ML model manually:

```powershell
python scripts/train_claim_ml_model.py --force
```

## QC-style UI

Legacy-style QC flow/UI is available in FastAPI frontend routes:

- Login: `http://127.0.0.1:8000/qc/login`
- Super admin workspace: `http://127.0.0.1:8000/qc/admin/dashboard`
- Doctor workspace: `http://127.0.0.1:8000/qc/doctor/dashboard`
- User workspace: `http://127.0.0.1:8000/qc/user/dashboard`

The UI uses the same navigation pattern and visual theme from the QC folder (`public/app.css`) and is wired to the new role-based API.
