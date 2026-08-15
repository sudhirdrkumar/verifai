# VerifAI / QC-BKP System Architecture

This document describes the current backend and UI architecture of the QC-BKP
modernization project, with emphasis on the live claim workflow:
upload, extraction, structuring, checklist evaluation, and report generation.

## 1. System Overview

The application is a FastAPI-based claims processing platform backed by:

- PostgreSQL for the primary operational database
- Amazon S3 for document storage
- OpenAI for structured extraction, vision handling, and summary generation
- AWS Textract for OCR / fallback extraction
- Background workers for extraction, merge jobs, folder sync, and medicine rectification

The system serves two main experiences:

1. Public / internal API endpoints under `/api/v1`
2. QC-style browser UI under `/qc/*`

The codebase also keeps compatibility with a legacy QC-style flow, including
claim import, document processing, and report generation.

## 2. Runtime Topology

### Main runtime pieces

- `backend/app/main.py`
  - Creates the FastAPI app
  - Mounts QC static assets
  - Registers routers
  - Starts background services on startup

- `app/api/router.py`
  - Aggregates all v1 routers

- Background services started from app startup:
  - `extraction_queue_service`
  - `medicine_rectify_scheduler`
  - `folder_sync_scheduler`
  - `merge_jobs_scheduler`

### Request flow

```text
Browser
  -> FastAPI app (/api/v1, /qc/*)
  -> PostgreSQL
  -> S3
  -> OpenAI / Textract / OCR
  -> Background workers
```

## 3. Frontend Architecture

The QC UI is served from `frontend/qc/`:

- `login.html`
- `workspace.html`
- `public/workspace.js`
- `public/report-editor.html`
- `public/report-editor.js`
- `public/app.css`

### UI responsibilities

- Login and role-based workspace access
- Claim dashboard and claim detail views
- Document upload and upload status
- Document preview / list / selection
- Extraction trigger and extraction queue visibility
- Report preview, edit, and save actions

### Important UI behavior

- The browser talks to the API through JSON endpoints
- Static assets are cache-busted via versioned query strings
- The workspace can build a report from structured data or latest extraction history
- The report editor can save report HTML as a versioned report artifact

## 4. API Layer

The main routers are registered under `/api/v1`:

- `health`
- `auth`
- `claims`
- `documents`
- `extractions`
- `checklist`
- `phase5_ml`
- `folder_sync`
- `integrations`
- `user_tools`
- `admin_tools`

### High-level endpoint groups

- `claims`
  - claim CRUD
  - claim assignment
  - claim structured-data generation
  - report HTML save
  - report version retrieval

- `documents`
  - upload / list / download / parse-status
  - document merge job support
  - presigned upload support

- `extractions`
  - extraction submission
  - extraction job polling
  - extraction history

- `checklist`
  - claim checklist evaluation

- `phase5_ml`
  - ML-backed claim decision support

## 5. Core Data Model

The system uses PostgreSQL as the main source of truth for operational state.

### Primary tables

- `claims`
  - claim header, patient, hospital, dates, status, assignment

- `claim_documents`
  - uploaded documents, storage keys, parse status, merge metadata

- `document_extractions`
  - extracted entities, evidence refs, model name, raw response

- `extraction_jobs`
  - async extraction queue state

- `document_merge_jobs`
  - merge queue state for combining multiple source documents

- `claim_structured_data`
  - normalized claim fields used by report and decision logic

- `decision_results`
  - checklist / decision / recommendation outputs

- `report_versions`
  - versioned HTML reports

- `workflow_events`
  - event/audit trail for processing steps

- `feedback_labels`
  - human feedback and supervision labels

- `model_registry`
  - ML model metadata

- `rule_registry`
  - rule catalog metadata

## 6. Document Storage Architecture

### Storage backend

Documents are stored in S3 and referenced in PostgreSQL by storage key.

### Upload patterns

The codebase supports:

- direct document upload to the API
- direct browser-to-S3 upload via presigned URL flow
- merged PDF generation for document batches

For performance-sensitive flows, the preferred pattern is:

1. request presigned upload information
2. upload file directly to S3
3. notify backend that upload is complete
4. enqueue extraction asynchronously

This avoids sending large binary payloads through the app server.

## 7. Extraction Architecture

The extraction layer is split into orchestration, provider adapters, and queueing.

### Key services

- `app/services/extractions_service.py`
  - main orchestration
  - extraction persistence
  - post-extraction report build scheduling

- `app/services/extraction_queue_service.py`
  - async extraction queue worker
  - job creation and polling

- `app/services/extraction_providers.py`
  - provider-specific extraction logic
  - route classification
  - page / document heuristics

- `app/services/extraction_s3_direct.py`
  - direct S3 extraction path

- `app/services/extraction_streaming.py`
  - streaming / chunk-oriented processing helpers

### Current extraction strategy

The extraction path is designed to be cost-aware and layered:

1. OpenAI-first extraction for structured understanding
2. AWS Textract fallback for OCR and text detection
3. Local OCR fallback when needed

The routing logic can classify documents by signal type, for example:

- handwriting
- messy scanned pages
- tables
- forms
- investigation pages
- medicine pages

This makes it possible to send only the right pages to the most expensive model.

### Async queue

The extraction queue stores jobs in `extraction_jobs` and processes them in a
background worker. This keeps the UI responsive and allows the system to show:

- queued
- running
- succeeded
- failed

## 8. Structuring and Report Architecture

### Structured data service

`app/services/claim_structuring_service.py` normalizes the raw extraction output
into claim-level structured fields.

It produces a canonical structure that is used by:

- report generation
- checklist evaluation
- downstream ML logic

### Report building

There are two report-producing surfaces:

1. Browser-side report rendering in `frontend/qc/public/workspace.js`
2. Backend report version storage via `report_versions`

The report is typically built from:

- structured claim data
- extraction pairs / evidence lines
- decision outcome

The report includes the standard health claim assessment sections such as:

- company name
- claim number
- claim type
- insured
- hospital
- treating doctor
- doctor registration number
- admission / discharge
- diagnosis
- complaints
- major findings
- alcoholism history
- claimed amount
- clinical findings
- investigation reports
- medicine evidence
- conclusion and recommendation

## 9. ML and Decision Support

### ML service layer

- `app/services/ml_claim_model.py`
  - claim scoring / recommendation features

- `app/services/phase5_ml_openai.py`
  - OpenAI-assisted ML layer for claim intelligence

- `app/services/checklist_pipeline.py`
  - checklist evaluation and rule application

- `app/services/grammar_service.py`
  - text cleanup / normalization helpers for structured outputs

### Usage pattern

ML is used to:

- generate recommendation support
- create concise conclusion text
- validate clinical reasoning
- summarize evidence into a claim-level decision narrative

The cheaper lightweight model path is preferred for routine summarization,
with heavier vision or reasoning only where the document quality requires it.

## 10. Background Jobs and Schedulers

### Extraction queue worker

Handles async extraction jobs from `extraction_jobs`.

### Merge job scheduler

Handles combined PDF build jobs from `document_merge_jobs`.

### Folder sync scheduler

Keeps imported folders and claim data synchronized.

### Medicine rectification scheduler

Improves medicine parsing and claim medicine normalization over time.

## 11. External Integrations

### OpenAI

Used for:

- structured extraction
- vision extraction for messy pages
- report conclusion generation
- ML-backed claim synthesis

### AWS Textract

Used for:

- OCR
- table / form text extraction
- fallback when OpenAI is not enough

### S3

Used for:

- document uploads
- merged PDFs
- raw source file storage

### Legacy / external claim systems

The repository still keeps compatibility with legacy QC data flows and import
jobs so historical claims can be migrated and reprocessed.

## 12. Operational Flow

### A. Claim intake

1. Claim is created or imported
2. Claim metadata is saved in PostgreSQL
3. Documents are attached to the claim

### B. Upload

1. UI requests upload capability
2. File is uploaded to S3, preferably directly when presigned URLs are used
3. Backend records the upload in `claim_documents`

### C. Extraction

1. User or worker triggers extraction
2. Job is queued in `extraction_jobs`
3. Worker downloads file from S3
4. Provider routing selects OpenAI / Textract / OCR path
5. Extraction result is written to `document_extractions`

### D. Structuring

1. Claim-level structured data is generated
2. Relevant fields are normalized
3. Checklist and ML steps run on the structured claim view

### E. Report generation

1. UI or backend builds the report HTML
2. Report is stored as a version in `report_versions`
3. Doctor/reviewer can inspect or regenerate it

## 13. Current Design Principles

- Keep upload fast and avoid unnecessary server-side file relay
- Use async jobs for slow work
- Reserve expensive model usage for messy or handwritten pages
- Keep structured data canonical so report generation stays stable
- Separate extraction, structuring, checklist, and report generation
- Prefer idempotent job processing and versioned outputs

## 14. Key Files

- `backend/app/main.py`
- `app/api/router.py`
- `app/api/v1/endpoints/claims.py`
- `app/api/v1/endpoints/documents.py`
- `app/api/v1/endpoints/extractions.py`
- `app/services/extractions_service.py`
- `app/services/extraction_queue_service.py`
- `app/services/extraction_providers.py`
- `app/services/extraction_s3_direct.py`
- `app/services/claim_structuring_service.py`
- `app/services/checklist_pipeline.py`
- `app/services/phase5_ml_openai.py`
- `app/services/ml_claim_model.py`
- `app/services/merge_jobs_service.py`
- `app/services/merge_jobs_processor.py`
- `app/services/documents_service.py`
- `frontend/qc/workspace.html`
- `frontend/qc/public/workspace.js`
- `README.md`

## 15. Recommended Next Step

The most important optimization path for this system is to keep the report
builder thin and push heavy lifting into:

- page classification
- targeted extraction
- structured claim generation
- asynchronous queue processing

That gives the best balance of speed, quality, and token cost.
