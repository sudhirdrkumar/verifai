#!/bin/bash
# Start backend on EC2 - run from /home/ec2-user/qc-python/backend directory

cd /home/ec2-user/qc-python/backend
nohup /home/ec2-user/qc-python/.venv/bin/python -m uvicorn app.main:app \
    --host 127.0.0.1 \
    --port 8001 \
    --workers 2 > /tmp/api.log 2>&1 &

echo "Backend started on port 8001 - check /tmp/api.log for status"
