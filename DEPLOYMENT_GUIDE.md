# Multi-Stage OCR Pipeline Deployment Guide

## Overview
This guide deploys the complete multi-stage processing pipeline:
- **Stage 1**: AWS Textract OCR (3 parallel workers)
- **Stage 2**: Gemini Flash synthesis (1 worker)
- **API**: FastAPI endpoint for bulk upload notifications

---

## Prerequisites on EC2

### 1. Connect to EC2
```bash
ssh -i 'C:\key\verif-ai.pem' ec2-user@15.207.135.22
```

### 2. Update system packages
```bash
sudo yum update -y
sudo yum install -y redis npm
```

### 3. Start Redis
```bash
sudo systemctl enable redis
sudo systemctl start redis
redis-cli ping  # Should return PONG
```

### 4. Install Node.js (for PM2)
```bash
sudo npm install -g pm2
pm2 startup
pm2 save
```

---

## Deployment Steps

### Step 1: Copy/Pull Updated Code
```bash
cd /home/ec2-user/qc-python

# Option A: Git pull (if configured)
git pull origin main

# Option B: Copy files from local machine
# Run from local terminal:
# scp -i 'C:\key\verif-ai.pem' backend/stage1_ocr_worker.py ec2-user@15.207.135.22:/home/ec2-user/qc-python/backend/
# scp -i 'C:\key\verif-ai.pem' backend/stage2_reduction_worker.py ec2-user@15.207.135.22:/home/ec2-user/qc-python/backend/
# scp -i 'C:\key\verif-ai.pem' backend/.env ec2-user@15.207.135.22:/home/ec2-user/qc-python/backend/
```

### Step 2: Update Database Tables
```bash
cd /home/ec2-user/qc-python
source .venv/bin/activate
python backend/create_ocr_tables.py
```

Expected output:
```
✅ Migration successful!
   Tables created: document_extractions, claim_structured_data
```

### Step 3: Install/Update Dependencies
```bash
cd /home/ec2-user/qc-python
source .venv/bin/activate
pip install -r backend/requirements.txt
```

### Step 4: Set Environment Variables
```bash
# Add to ~/.bashrc or ~/.bash_profile
GEMINI_API_KEY=REDACTED
export REDIS_HOST="127.0.0.1"
export REDIS_PORT="6379"
export AWS_REGION="ap-south-1"

source ~/.bashrc  # Reload
```

### Step 5: Start Workers with PM2
```bash
cd /home/ec2-user/qc-python
source .venv/bin/activate

# Start Stage 1 workers (3 parallel instances)
pm2 start backend/stage1_ocr_worker.py -i 3 --name "stage1-ocr" --interpreter python

# Start Stage 2 reducer (1 instance)
pm2 start backend/stage2_reduction_worker.py -i 1 --name "stage2-reducer" --interpreter python

# Save PM2 config
pm2 save

# Verify all running
pm2 status
```

Expected output:
```
┌─────┬──────────────┬─────────┬──────┬───────────┐
│ id  │ name         │ version │ mode │ status    │
├─────┼──────────────┼─────────┼──────┼───────────┤
│ 0-2 │ stage1-ocr   │ N/A     │ fork │ online    │
│ 3   │ stage2-reduce│ N/A     │ fork │ online    │
└─────┴──────────────┴─────────┴──────┴───────────┘
```

### Step 6: Restart FastAPI Backend
```bash
# Kill existing uvicorn
pkill -f "uvicorn.*8001"

# Start new instance
cd /home/ec2-user/qc-python
nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001 --workers 2 > /tmp/api.log 2>&1 &
```

---

## Verification

### Check Redis
```bash
redis-cli
> PING              # Should return PONG
> INFO             # Should show Redis stats
> KEYS *           # Should be empty initially
```

### Check Workers Logs
```bash
pm2 logs stage1-ocr     # Stream Stage 1 logs
pm2 logs stage2-reducer # Stream Stage 2 logs
pm2 monit              # Real-time monitor
```

### Test API Endpoint
```bash
curl -X POST http://127.0.0.1:8001/api/v1/claims/123/upload-complete \
  -H "Content-Type: application/json" \
  -d '{
    "claim_id": 123,
    "s3_bucket": "rightworks-docs",
    "files": [
      {"document_id": 1, "s3_key": "claims/file1.pdf"}
    ]
  }'
```

Expected response:
```json
{
  "status": "success",
  "message": "Successfully queued 1 documents for claim 123",
  "claim_id": 123,
  "file_count": 1
}
```

---

## Monitoring

### Watch Redis Queue Growth
```bash
watch -n 1 'redis-cli INFO stats | grep -E "total_commands_processed|keyspace"'
```

### Watch Worker Activity
```bash
pm2 monit
```

### Check Database for Results
```bash
psql -h 127.0.0.1 -U admin -d qc_bkp_modern_live
> SELECT claim_id, status FROM claim_structured_data LIMIT 5;
> SELECT COUNT(*) FROM document_extractions;
```

---

## Troubleshooting

### Workers not starting
```bash
# Check logs
pm2 logs

# Restart
pm2 restart all

# Check Python path
which python3
```

### Redis connection refused
```bash
redis-cli ping          # Test connection
sudo systemctl status redis  # Check service
sudo systemctl restart redis # Restart
```

### Database migration failed
```bash
# Check database connection
psql -h 127.0.0.1 -U admin -d qc_bkp_modern_live -c "SELECT 1;"

# Run migration again with verbose output
python backend/create_ocr_tables.py
```

### Gemini API errors
```bash
# Verify API key in .env
GEMINI_API_KEY=REDACTED

# Test Gemini connection
python -c "import google.generativeai as genai; genai.configure(api_key='YOUR_KEY'); print('OK')"
```

---

## Maintenance

### View all PM2 processes
```bash
pm2 status
```

### Restart all workers
```bash
pm2 restart all
```

### Stop workers (without stopping app)
```bash
pm2 stop stage1-ocr stage2-reducer
```

### View PM2 startup script
```bash
pm2 startup
```

---

## Rollback

If something goes wrong:

```bash
# Stop all workers
pm2 stop all

# Clear Redis (CAREFUL!)
# redis-cli FLUSHALL

# Restart from scratch
pm2 delete all
# Then re-run deployment steps
```

---

## Next Steps

1. ✅ Deploy workers to EC2
2. ✅ Verify all processes running
3. Update frontend to call `/claims/{claim_id}/upload-complete` endpoint
4. Test end-to-end pipeline with a real claim
5. Monitor performance and tune worker counts if needed
