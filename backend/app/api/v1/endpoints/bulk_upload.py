import json
import redis
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db

router = APIRouter(prefix="/claims", tags=["bulk-upload"])

r = redis.Redis(
    host=settings.redis_host or 'localhost',
    port=settings.redis_port or 6379,
    decode_responses=True
)

class FileMetadata(BaseModel):
    document_id: int
    s3_key: str

class BulkUploadNotification(BaseModel):
    claim_id: int
    s3_bucket: str
    files: list[FileMetadata]

@router.post("/{claim_id}/upload-complete")
async def handle_bulk_upload(claim_id: int, payload: BulkUploadNotification, db: Session = Depends(get_db)):
    """
    Called when browser finishes uploading all files for a claim.
    Initializes Redis tracking and queues Stage 1 OCR tasks.
    """
    total_files = len(payload.files)

    if total_files == 0:
        return {"status": "error", "message": "No files provided"}

    # Initialize Redis tracking for this claim
    r.hset(f"claim:tracker:{claim_id}", mapping={"total": total_files, "completed": 0})

    # Queue each file for Stage 1 OCR processing
    for file_metadata in payload.files:
        task_payload = {
            "claim_id": claim_id,
            "document_id": file_metadata.document_id,
            "s3_bucket": payload.s3_bucket,
            "s3_key": file_metadata.s3_key
        }
        r.lpush("queue:stage1_ocr_extraction", json.dumps(task_payload))
        r.hset(f"doc:state:{file_metadata.document_id}", "status", "QUEUED")

    return {
        "status": "success",
        "message": f"Successfully queued {total_files} documents for claim {claim_id}",
        "claim_id": claim_id,
        "file_count": total_files
    }

@router.get("/{claim_id}/processing-status")
async def get_processing_status(claim_id: int):
    """Get current processing status for a claim"""
    tracker = r.hgetall(f"claim:tracker:{claim_id}")

    if not tracker:
        return {"status": "not_found", "claim_id": claim_id}

    completed = int(tracker.get("completed", 0))
    total = int(tracker.get("total", 0))

    # Check if reduction is done
    reduction_exists = r.exists(f"claim:reduction:done:{claim_id}")

    return {
        "claim_id": claim_id,
        "status": "completed" if reduction_exists else "processing",
        "stage1_progress": f"{completed}/{total}",
        "stage2_done": bool(reduction_exists)
    }
