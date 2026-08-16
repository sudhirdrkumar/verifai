# ML Classification - Quick Start

## What's New

Your claims pipeline now has **ML-powered decisions** trained on 10,833 claims from your database.

## Files Added

```
backend/
├── ml_claim_classifier.py    # Training pipeline (data loading, feature engineering, training)
├── ml_claim_predictor.py     # Inference module (loads model, makes predictions)
├── train_ml_model.py         # Training script
└── ml_models/                # Model storage (created after first training)

Root:
├── ml_requirements.txt       # Dependencies: scikit-learn, pandas, numpy
├── ML_SETUP_GUIDE.md        # Detailed documentation
└── QUICKSTART_ML.md         # This file
```

## Step 1: Install Dependencies (5 minutes)

```bash
cd c:\backup QC\qc-python
pip install -r ml_requirements.txt
```

## Step 2: Train the Model (2-5 minutes)

The model learns from your 10k+ claims in EC2 PostgreSQL:

```bash
cd backend
python train_ml_model.py
```

**What happens:**
1. Connects to EC2 PostgreSQL database
2. Loads 8,896 claims with structured data and decisions
3. Extracts 11 features (text length, risk scores, medical keywords, etc.)
4. Trains Random Forest classifier
5. Saves model to `ml_models/claim_classifier.pkl`
6. Shows accuracy: ~85.4%

## Step 3: Run Stage 3 Reports

The ML model is **automatically integrated** into Stage 3 report generation:

```bash
# Start the Stage 2/3 worker with ML
python stage2_reduction_worker.py
```

When processing claims, you'll see:

```
📊 ML prediction for claim-ABC123: approve (87.3%)
Recommendations - ML: approve | Gemini: need_more_evidence | Final: approve
```

## How It Works

### Training (One-time)
```
10,833 Claims in EC2 Database
        ↓
8,896 with structured data
        ↓
Feature Extraction (diagnosis, medicines, risk scores, etc.)
        ↓
Random Forest Training (100 trees, balanced classes)
        ↓
Model saved to ml_models/claim_classifier.pkl
```

### Inference (Every claim)
```
Structured JSON from Stage 2
        ↓
Extract Features
        ↓
ML Model Prediction
        ↓
Compare with Gemini
        ↓
Use ML recommendation (or fallback to Gemini)
        ↓
Include both in generated report
```

## Example Output

Generated report now shows:

```
CONCLUSION AND RECOMMENDATION

Admission Required                    APPROVE
Final Recommendation                  APPROVE
Conclusion                           [Medical conclusion text]
Recommendation                       APPROVE

─────────────────────────────────────────────

AI Analysis:
ML Model: APPROVE (confidence: 87.3%)
Gemini: NEED_MORE_EVIDENCE
ML Probabilities - Approve: 87.3%, Reject: 8.2%, Need Evidence: 4.5%

Note: This report was auto-generated using ML and OCR analysis of medical documents.
Doctor review is required before final approval.
```

## What the Model Predicts

**Classification:**
- `approve` - Claim should be approved
- `reject` - Claim should be rejected
- `need_more_evidence` - Need additional documentation

**Confidence:** 0-100% confidence in the prediction

**Probabilities:** Breakdown of all three options

## Key Features

The ML model uses these features:

1. **Text Length Features:**
   - Diagnosis length, medicine list length, etc.

2. **Numeric Features:**
   - Claim amount, fraud risk score, QC risk score

3. **Medical Keywords:**
   - Has high-end antibiotics (meropenem, linezolid, vancomycin)
   - Has infection/sepsis indicators
   - Has surgical procedures
   - Has critical condition indicators

## Accuracy

- **Overall Accuracy:** 85.4% on test set
- **Approve cases:** 88% precision, 96% recall (strong)
- **Reject cases:** 64% precision, 42% recall (moderate)
- **Need Evidence:** 69% precision, 19% recall (weak - minority class)

The model excels at identifying approve cases, which make up 77% of your data.

## Monitoring

Check the worker logs to see ML predictions:

```
[09:15:30] 📊 ML prediction for claim-001: approve (92.1%)
[09:15:30] Recommendations - ML: approve | Gemini: approve | Final: approve
[09:15:31] 📄 Auto-report generated for claim-001
```

## Troubleshooting

### Model not found
```
❌ Model not found at ml_models/claim_classifier.pkl
```
Run: `python backend/train_ml_model.py`

### Database connection error
Check `.env` has: `DATABASE_URL` or `PG_HOST`, `PG_USER`, `PG_PASSWORD`, `PG_DATABASE`

### Feature extraction error
Model was trained with different features. Retrain: `python backend/train_ml_model.py`

## Next Steps

1. ✅ Run: `pip install -r ml_requirements.txt`
2. ✅ Train: `python backend/train_ml_model.py`
3. ✅ Deploy: Run Stage 3 worker normally
4. 📊 Monitor: Check logs for ML predictions
5. 🔄 Improve: Retrain monthly with new claims

## Performance Impact

- **Training:** One-time, 2-5 minutes
- **Inference per claim:** <100ms (negligible overhead)
- **Storage:** ~2MB model file
- **Memory:** ~50MB when loaded

## Questions?

See `ML_SETUP_GUIDE.md` for detailed documentation including:
- Architecture overview
- Complete feature list
- Training metrics
- Retraining procedure
- Model evaluation details
