"""
Folder Sync Service - Automatically monitors a folder for documents and uploads to S3.

Features:
- Monitors a configurable folder path for PDF and image files
- Extracts claim numbers from filenames
- Merges multiple files per claim
- Uploads to S3
- Tracks uploads in database
"""
import os
import re
import logging
from pathlib import Path
from typing import Optional, Tuple
from uuid import uuid4

from app.db.session import SessionLocal
from app.services.storage_service import upload_bytes
from app.services.documents_service import merge_files_to_single_pdf
from app.schemas.auth import UserRole

logger = logging.getLogger(__name__)

# Filename pattern: claim_number_completed or similar variants
CLAIM_PATTERN = re.compile(r'(\d{8,10})(?:_completed)?', re.IGNORECASE)
SUPPORTED_EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp'}


def extract_claim_number(filename: str) -> Optional[str]:
    """Extract claim number from filename."""
    match = CLAIM_PATTERN.search(filename)
    if match:
        return match.group(1)
    return None


def get_mime_type(filename: str) -> str:
    """Get MIME type based on file extension."""
    ext = Path(filename).suffix.lower()
    mime_types = {
        '.pdf': 'application/pdf',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.tiff': 'image/tiff',
        '.tif': 'image/tiff',
        '.bmp': 'image/bmp',
    }
    return mime_types.get(ext, 'application/octet-stream')


def sync_folder(folder_path: str, actor_id: str = "system") -> dict:
    """
    Sync documents from a folder to S3.

    Args:
        folder_path: Path to the folder containing documents
        actor_id: User ID performing the sync

    Returns:
        Dictionary with sync results
    """
    if not folder_path or not os.path.isdir(folder_path):
        logger.error(f"Invalid folder path: {folder_path}")
        return {"success": False, "error": f"Invalid folder path: {folder_path}", "processed": 0}

    results = {
        "success": True,
        "processed": 0,
        "uploaded": 0,
        "failed": 0,
        "errors": [],
        "claim_files": {},
    }

    try:
        # Group files by claim number
        claim_files = {}

        for filename in os.listdir(folder_path):
            filepath = os.path.join(folder_path, filename)

            # Skip directories and unsupported files
            if not os.path.isfile(filepath):
                continue

            if Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue

            # Extract claim number
            claim_number = extract_claim_number(filename)
            if not claim_number:
                logger.warning(f"Could not extract claim number from: {filename}")
                results["errors"].append(f"Could not parse claim number from: {filename}")
                continue

            if claim_number not in claim_files:
                claim_files[claim_number] = []

            claim_files[claim_number].append({
                "filepath": filepath,
                "filename": filename,
                "mime_type": get_mime_type(filename),
            })

            results["processed"] += 1

        # Process each claim's files
        db = SessionLocal()
        try:
            for claim_number, files in claim_files.items():
                try:
                    uploaded = _process_claim_files(claim_number, files, actor_id, db)
                    if uploaded:
                        results["uploaded"] += 1
                except Exception as e:
                    logger.exception(f"Error processing claim {claim_number}: {e}")
                    results["failed"] += 1
                    results["errors"].append(f"Claim {claim_number}: {str(e)}")
        finally:
            db.close()

        results["claim_files"] = claim_files
        logger.info(f"Folder sync completed: {results}")
        return results

    except Exception as e:
        logger.exception(f"Folder sync error: {e}")
        return {
            "success": False,
            "error": str(e),
            "processed": 0,
            "uploaded": 0,
            "failed": 0,
        }


def _process_claim_files(claim_number: str, files: list, actor_id: str, db) -> bool:
    """
    Process files for a single claim.

    - Merges multiple files if present
    - Uploads to S3
    - Records in database
    """
    if not files:
        return False

    try:
        # Read file contents
        file_items = []
        for file_info in files:
            with open(file_info["filepath"], "rb") as f:
                content = f.read()

            file_items.append({
                "filename": file_info["filename"],
                "content": content,
                "mime_type": file_info["mime_type"],
                "size": len(content),
            })

        # Merge files if multiple
        if len(file_items) > 1:
            merged_content, success_files, failed_files, page_count = merge_files_to_single_pdf(file_items)
            if not merged_content:
                logger.error(f"Failed to merge files for claim {claim_number}")
                return False

            upload_filename = f"claim_{claim_number}_merged_uploaded.pdf"
            mime_type = "application/pdf"
            content_to_upload = merged_content
        else:
            # Single file - use as-is
            file_item = file_items[0]
            upload_filename = f"claim_{claim_number}_{file_item['filename']}"
            mime_type = file_item["mime_type"]
            content_to_upload = file_item["content"]

        # Upload to S3
        s3_result = upload_bytes(
            object_key=upload_filename,
            payload=content_to_upload,
            content_type=mime_type,
        )

        if not s3_result or not s3_result.get("success"):
            logger.error(f"S3 upload failed for claim {claim_number}: {s3_result}")
            return False

        # Record in database (claim_documents table)
        from sqlalchemy import text

        document_id = str(uuid4())
        db.execute(
            text("""
                INSERT INTO claim_documents (
                    id, claim_id, file_name, storage_key, mime_type,
                    file_size, uploaded_by, created_at, updated_at
                ) VALUES (
                    :id, :claim_id, :file_name, :storage_key, :mime_type,
                    :file_size, :uploaded_by, NOW(), NOW()
                )
                ON CONFLICT(storage_key) DO NOTHING
            """),
            {
                "id": document_id,
                "claim_id": claim_number,
                "file_name": upload_filename,
                "storage_key": s3_result.get("object_key", upload_filename),
                "mime_type": mime_type,
                "file_size": len(content_to_upload),
                "uploaded_by": actor_id,
            }
        )
        db.commit()

        logger.info(f"Successfully processed claim {claim_number}: {upload_filename}")
        return True

    except Exception as e:
        logger.exception(f"Error processing claim {claim_number}: {e}")
        if db:
            db.rollback()
        return False
