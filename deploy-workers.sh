#!/bin/bash

# Deployment script for multi-stage OCR workers on EC2

set -e

PROJECT_DIR="/home/ec2-user/qc-python"
cd $PROJECT_DIR

# Activate venv
source .venv/bin/activate

# Install dependencies
pip install -r backend/requirements.txt

# Install PM2 if not already installed
npm install -g pm2 || true

# Stop existing workers
pm2 stop stage1-ocr stage2-reducer 2>/dev/null || true
pm2 delete stage1-ocr stage2-reducer 2>/dev/null || true

# Start Redis (if not running)
if ! pgrep -x "redis-server" > /dev/null; then
    echo "Starting Redis..."
    redis-server --daemonize yes --port 6379
    sleep 2
fi

# Start Stage 1 workers (3 instances for parallel OCR)
echo "Starting Stage 1 OCR workers..."
pm2 start backend/stage1_ocr_worker.py -i 3 --name "stage1-ocr" --interpreter python

# Start Stage 2 reducer (1 instance)
echo "Starting Stage 2 Reducer..."
pm2 start backend/stage2_reduction_worker.py -i 1 --name "stage2-reducer" --interpreter python

# Restart FastAPI backend
echo "Restarting FastAPI backend..."
pm2 restart "uvicorn app.main:app*" || true

# Save PM2 config
pm2 save

echo "✅ Multi-stage pipeline deployed successfully!"
pm2 list
