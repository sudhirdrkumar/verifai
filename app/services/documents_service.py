import hashlib
import json
import mimetypes
import re
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pypdf import PdfReader, PdfWriter
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.core.config import settings

from app.schemas.document import (
    DocumentBulkDeleteResponse,
    DocumentDownloadUrlResponse,
    DocumentListResponse,
    DocumentParseStatusUpdateRequest,
    DocumentResponse,
)
from app.services.storage_service import (
    StorageConfigError,
    StorageOperationError,
    delete_object,
    generate_download_url,
    upload_bytes,
    _s3_client,
)


class ClaimNotFoundError(Exception):
    pass


class DocumentNotFoundError(Exception):
    pass


class DocumentMergeError(Exception):
    pass


_SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
_MERGE_IMAGE_MAX_WIDTH = 2200
_MERGE_IMAGE_MAX_HEIGHT = 2200
_MERGE_IMAGE_JPEG_QUALITY = 68
_MERGE_IMAGE_PDF_DPI = 150
_MERGE_LOSSY_MAX_WIDTH = 1800
_MERGE_LOSSY_MAX_HEIGHT = 1800
_MERGE_LOSSY_JPEG_QUALITY = 45
_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_LEGACY_DOC_EXTENSIONS = (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    ".bmp",
    ".webp",
    ".doc",
    ".docx",
)
_LEGACY_URL_HOST_HINTS = ("amazonaws.com", "teamrightworks.in", "s3.")


def _normalize_metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _to_document_response(row: dict[str, Any]) -> DocumentResponse:
    row["metadata"] = _normalize_metadata(row.get("metadata"))
    return DocumentResponse.model_validate(row)


def _sanitize_filename(name: str) -> str:
    base = (name or "document").strip()
    base = base.replace("\\", "_").replace("/", "_")
    base = re.sub(r"\s+", "_", base)
    base = re.sub(r"[^A-Za-z0-9._-]", "", base)
    return base[:180] or "document"


def _claim_exists(db: Session, claim_id: UUID) -> bool:
    exists = db.execute(
        text("SELECT 1 FROM claims WHERE id = :claim_id"),
        {"claim_id": str(claim_id)},
    ).first()
    return exists is not None


def _normalize_http_url(value: Any) -> str:
    raw = str(value or "").strip().strip("'\"")
    if not raw:
        return ""
    raw = raw.replace("\\/", "/").rstrip("),.;]")
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("www."):
        raw = "https://" + raw
    if not re.match(r"^https?://", raw, flags=re.IGNORECASE):
        return ""
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return raw


def _looks_like_document_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()

    if any(path.endswith(ext) for ext in _LEGACY_DOC_EXTENSIONS):
        return True
    if any(token in host for token in _LEGACY_URL_HOST_HINTS):
        if any(token in path for token in ("document", "doc", "claim", "upload", "proclaim", "bill", "report")):
            return True
        if bool(parsed.query):
            return True
    return False


def _extract_legacy_document_links(payload: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add_link(candidate: Any, source_path: str) -> None:
        normalized = _normalize_http_url(candidate)
        if not normalized or not _looks_like_document_url(normalized):
            return
        key = normalized.lower()
        if key in seen:
            return
        seen.add(key)
        out.append((normalized, source_path))

    def walk(obj: Any, source_path: str) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_text = str(key or "")
                key_lower = key_text.lower()
                next_path = f"{source_path}.{key_lower}" if source_path else key_lower
                if isinstance(value, str):
                    if any(token in key_lower for token in ("url", "link", "file", "document", "attachment", "s3", "bucket", "path")):
                        add_link(value, next_path)
                    for match in _HTTP_URL_RE.findall(value):
                        add_link(match, next_path + ":text")
                else:
                    walk(value, next_path)
            return

        if isinstance(obj, list):
            for idx, item in enumerate(obj):
                walk(item, f"{source_path}[{idx}]")
            return

        if isinstance(obj, str):
            add_link(obj, source_path)
            for match in _HTTP_URL_RE.findall(obj):
                add_link(match, source_path + ":text")

    walk(payload, "legacy_payload")
    try:
        dumped = json.dumps(payload, ensure_ascii=False)
        for match in _HTTP_URL_RE.findall(dumped):
            add_link(match, "legacy_payload:regex")
    except Exception:
        pass
    return out


def _file_name_from_url(url: str, fallback_index: int) -> str:
    parsed = urlparse(url)
    base_name = unquote(Path(parsed.path or "").name)
    if not base_name:
        base_name = f"legacy_document_{fallback_index}.pdf"
    if "." not in base_name:
        base_name += ".pdf"
    return _sanitize_filename(base_name)


def _first_direct_download_url(storage_key: str, metadata: dict[str, Any]) -> str:
    provider = str(metadata.get("storage_provider") or "").strip().lower()

    # Plain HTTP/HTTPS key can be opened directly.
    storage_url = _normalize_http_url(storage_key)
    if storage_url:
        return storage_url

    # External-link style migrated docs should open directly.
    for candidate in (
        metadata.get("external_document_url"),
        metadata.get("external_url"),
        metadata.get("legacy_s3_url"),
    ):
        normalized = _normalize_http_url(candidate)
        if normalized:
            return normalized

    # For internal S3-backed docs, keep presigned URL behavior (no direct URL override).
    if provider not in {"external_link", "legacy_external"}:
        return ""

    bucket = str(metadata.get("legacy_s3_bucket") or metadata.get("bucket") or "").strip()
    key = str(storage_key or "").strip()
    if bucket and key and not _normalize_http_url(key):
        region = str(metadata.get("legacy_s3_region") or metadata.get("region") or "ap-south-1").strip() or "ap-south-1"
        endpoint = str(metadata.get("legacy_s3_endpoint") or "").strip()
        escaped_key = quote(key.lstrip("/"), safe="/-_.~")
        if endpoint:
            return f"{endpoint.rstrip('/')}/{bucket}/{escaped_key}"
        return f"https://{bucket}.s3.{region}.amazonaws.com/{escaped_key}"
    return ""

def _materialize_legacy_payload_documents(db: Session, claim_id: UUID) -> int:
    legacy_row = db.execute(
        text("SELECT legacy_payload FROM claim_legacy_data WHERE claim_id = :claim_id LIMIT 1"),
        {"claim_id": str(claim_id)},
    ).mappings().first()
    if legacy_row is None:
        return 0

    payload = _normalize_metadata(legacy_row.get("legacy_payload"))
    if not payload:
        return 0

    candidate_links = _extract_legacy_document_links(payload)
    if not candidate_links:
        return 0

    existing_rows = db.execute(
        text("SELECT storage_key, metadata FROM claim_documents WHERE claim_id = :claim_id"),
        {"claim_id": str(claim_id)},
    ).mappings().all()
    existing_keys = {str(r.get("storage_key") or "").strip().lower() for r in existing_rows if str(r.get("storage_key") or "").strip()}
    existing_urls: set[str] = set()
    for row in existing_rows:
        row_key = str(row.get("storage_key") or "").strip()
        row_metadata = _normalize_metadata(row.get("metadata"))
        for candidate in (
            row_metadata.get("external_document_url"),
            row_metadata.get("external_url"),
            row_metadata.get("legacy_s3_url"),
            row_metadata.get("s3_url"),
            row_key,
        ):
            normalized = _normalize_http_url(candidate)
            if normalized:
                existing_urls.add(normalized.lower())

    inserted = 0
    for idx, (url, source_path) in enumerate(candidate_links, start=1):
        normalized = _normalize_http_url(url)
        if not normalized:
            continue
        if normalized.lower() in existing_urls:
            continue

        storage_key = f"legacy-external/{uuid5(NAMESPACE_URL, normalized).hex}"
        if storage_key.lower() in existing_keys:
            continue

        file_name = _file_name_from_url(normalized, idx)
        guessed_mime, _ = mimetypes.guess_type(file_name)
        metadata = {
            "storage_provider": "external_link",
            "external_document_url": normalized,
            "legacy_source": "claim_legacy_data",
            "legacy_source_path": source_path,
            "imported_via": "auto_materialize",
        }

        row = db.execute(
            text(
                """
                INSERT INTO claim_documents (
                    claim_id,
                    storage_key,
                    file_name,
                    mime_type,
                    file_size_bytes,
                    checksum_sha256,
                    parse_status,
                    retention_class,
                    uploaded_by,
                    metadata
                )
                VALUES (
                    :claim_id,
                    :storage_key,
                    :file_name,
                    :mime_type,
                    NULL,
                    NULL,
                    'succeeded',
                    'standard',
                    'legacy_sync',
                    CAST(:metadata AS jsonb)
                )
                ON CONFLICT (claim_id, storage_key) DO NOTHING
                RETURNING id
                """
            ),
            {
                "claim_id": str(claim_id),
                "storage_key": storage_key,
                "file_name": file_name,
                "mime_type": guessed_mime or "application/pdf",
                "metadata": json.dumps(metadata),
            },
        ).mappings().first()
        if row is not None:
            inserted += 1
            existing_keys.add(storage_key.lower())
            existing_urls.add(normalized.lower())

    if inserted > 0:
        db.commit()
    return inserted


def _public_s3_object_url(bucket: str, key: str) -> str:
    escaped_key = quote(str(key or "").lstrip("/"), safe="/-_.~")
    endpoint = str(settings.s3_endpoint_url or "").strip()
    if endpoint:
        return f"{endpoint.rstrip('/')}/{bucket}/{escaped_key}"
    return f"https://{bucket}.s3.{settings.s3_region}.amazonaws.com/{escaped_key}"


def _materialize_s3_prefix_documents(db: Session, claim_id: UUID) -> int:
    if not settings.s3_bucket:
        return 0

    claim_row = db.execute(
        text("SELECT external_claim_id FROM claims WHERE id = :claim_id LIMIT 1"),
        {"claim_id": str(claim_id)},
    ).mappings().first()
    if claim_row is None:
        return 0

    external_claim_id = str(claim_row.get("external_claim_id") or "").strip()
    if not external_claim_id:
        return 0

    prefixes = [f"claims/{external_claim_id}/", f"claims//{external_claim_id}/"]

    existing_rows = db.execute(
        text("SELECT storage_key FROM claim_documents WHERE claim_id = :claim_id"),
        {"claim_id": str(claim_id)},
    ).mappings().all()
    existing_keys = {str(r.get("storage_key") or "").strip().lower() for r in existing_rows if str(r.get("storage_key") or "").strip()}

    client = _s3_client()
    inserted = 0

    for prefix in prefixes:
        continuation_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": settings.s3_bucket,
                "Prefix": prefix,
                "MaxKeys": 1000,
            }
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token

            response = client.list_objects_v2(**kwargs)
            objects = response.get("Contents") or []
            for obj in objects:
                key = str(obj.get("Key") or "").strip()
                if not key or key.endswith("/"):
                    continue
                if key.lower() in existing_keys:
                    continue

                file_name = _sanitize_filename(Path(key).name or (external_claim_id + ".pdf"))
                guessed_mime, _ = mimetypes.guess_type(file_name)
                metadata = {
                    "storage_provider": "s3",
                    "bucket": settings.s3_bucket,
                    "region": settings.s3_region,
                    "s3_url": _public_s3_object_url(settings.s3_bucket, key),
                    "imported_via": "s3_prefix_backfill",
                    "legacy_source": "s3_bucket_prefix",
                }

                row = db.execute(
                    text(
                        """
                        INSERT INTO claim_documents (
                            claim_id,
                            storage_key,
                            file_name,
                            mime_type,
                            file_size_bytes,
                            checksum_sha256,
                            parse_status,
                            retention_class,
                            uploaded_by,
                            uploaded_at,
                            metadata
                        )
                        VALUES (
                            :claim_id,
                            :storage_key,
                            :file_name,
                            :mime_type,
                            :file_size_bytes,
                            NULL,
                            'succeeded',
                            'standard',
                            'legacy_sync',
                            NOW(),
                            CAST(:metadata AS jsonb)
                        )
                        ON CONFLICT (claim_id, storage_key) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "claim_id": str(claim_id),
                        "storage_key": key,
                        "file_name": file_name,
                        "mime_type": guessed_mime or "application/pdf",
                        "file_size_bytes": int(obj.get("Size") or 0),
                        "metadata": json.dumps(metadata),
                    },
                ).mappings().first()
                if row is not None:
                    inserted += 1
                    existing_keys.add(key.lower())

            if not bool(response.get("IsTruncated")):
                break
            continuation_token = str(response.get("NextContinuationToken") or "").strip() or None

    if inserted > 0:
        db.commit()
    return inserted


def ensure_legacy_documents_materialized(db: Session, claim_id: UUID) -> int:
    inserted = 0
    try:
        inserted += _materialize_legacy_payload_documents(db, claim_id)
    except Exception:
        db.rollback()
    try:
        inserted += _materialize_s3_prefix_documents(db, claim_id)
    except Exception:
        db.rollback()
    return inserted

def _emit_workflow_event(
    db: Session,
    claim_id: UUID,
    event_type: str,
    actor_id: str | None,
    payload: dict[str, Any],
) -> None:
    db.execute(
        text(
            """
            INSERT INTO workflow_events (claim_id, actor_type, actor_id, event_type, event_payload)
            VALUES (:claim_id, 'user', :actor_id, :event_type, CAST(:event_payload AS jsonb))
            """
        ),
        {
            "claim_id": str(claim_id),
            "actor_id": actor_id,
            "event_type": event_type,
            "event_payload": json.dumps(payload),
        },
    )


def create_document(
    db: Session,
    claim_id: UUID,
    file_name: str,
    mime_type: str,
    file_bytes: bytes,
    uploaded_by: str | None,
    retention_class: str,
) -> DocumentResponse:
    if not _claim_exists(db, claim_id):
        raise ClaimNotFoundError

    safe_file_name = _sanitize_filename(file_name)
    checksum = hashlib.sha256(file_bytes).hexdigest()
    object_key = f"claims/{claim_id}/documents/{uuid4().hex}_{safe_file_name}"

    upload_result = upload_bytes(object_key=object_key, payload=file_bytes, content_type=mime_type)
    metadata = {
        "storage_provider": "s3",
        "bucket": upload_result["bucket"],
        "region": upload_result["region"],
        "s3_url": upload_result["url"],
        "etag": upload_result.get("etag"),
    }

    row = db.execute(
        text(
            """
            INSERT INTO claim_documents (
                claim_id,
                storage_key,
                file_name,
                mime_type,
                file_size_bytes,
                checksum_sha256,
                parse_status,
                retention_class,
                uploaded_by,
                metadata
            )
            VALUES (
                :claim_id,
                :storage_key,
                :file_name,
                :mime_type,
                :file_size_bytes,
                :checksum_sha256,
                'pending',
                :retention_class,
                :uploaded_by,
                CAST(:metadata AS jsonb)
            )
            RETURNING
                id,
                claim_id,
                storage_key,
                file_name,
                mime_type,
                file_size_bytes,
                checksum_sha256,
                parse_status,
                page_count,
                retention_class,
                uploaded_by,
                uploaded_at,
                parsed_at,
                metadata
            """
        ),
        {
            "claim_id": str(claim_id),
            "storage_key": object_key,
            "file_name": safe_file_name,
            "mime_type": mime_type,
            "file_size_bytes": len(file_bytes),
            "checksum_sha256": checksum,
            "retention_class": retention_class,
            "uploaded_by": uploaded_by,
            "metadata": json.dumps(metadata),
        },
    ).mappings().one()

    document = _to_document_response(dict(row))
    _emit_workflow_event(
        db=db,
        claim_id=claim_id,
        event_type="document_uploaded",
        actor_id=uploaded_by,
        payload={"document_id": str(document.id), "storage_key": document.storage_key},
    )
    db.commit()
    return document


def _detect_merge_file_kind(file_name: str, mime_type: str) -> str:
    ext = Path(file_name or "").suffix.lower()
    mime = str(mime_type or "").lower().strip()

    if ext == ".pdf" or mime == "application/pdf":
        return "pdf"

    if ext in _SUPPORTED_IMAGE_EXTENSIONS:
        return "image"

    if mime.startswith("image/"):
        return "image"

    return "unsupported"


def _image_to_pdf_bytes(file_name: str, file_bytes: bytes) -> bytes:
    try:
        from PIL import Image, ImageSequence
    except ImportError as exc:
        raise DocumentMergeError("Pillow is required for image to PDF conversion. Run: pip install Pillow") from exc

    try:
        with Image.open(BytesIO(file_bytes)) as img:
            frames_rgb = []
            for frame in ImageSequence.Iterator(img):
                rgb = frame.convert("RGB")
                width, height = rgb.size
                scale = min(
                    1.0,
                    _MERGE_IMAGE_MAX_WIDTH / max(1, width),
                    _MERGE_IMAGE_MAX_HEIGHT / max(1, height),
                )
                if scale < 1.0:
                    rgb = rgb.resize(
                        (
                            max(1, int(width * scale)),
                            max(1, int(height * scale)),
                        ),
                        Image.Resampling.LANCZOS,
                    )
                frames_rgb.append(rgb)

            if not frames_rgb:
                raise DocumentMergeError(f"Could not read image content from: {file_name}")

            output = BytesIO()
            first = frames_rgb[0]
            rest = frames_rgb[1:]
            first.save(
                output,
                format="PDF",
                save_all=bool(rest),
                append_images=rest,
                optimize=True,
                quality=_MERGE_IMAGE_JPEG_QUALITY,
                resolution=float(_MERGE_IMAGE_PDF_DPI),
            )

            for frame in frames_rgb:
                frame.close()

            return output.getvalue()
    except DocumentMergeError:
        raise
    except Exception as exc:
        raise DocumentMergeError(f"Failed to convert image to PDF: {file_name}") from exc



def _apply_lossy_pdf_recompress(pdf_bytes: bytes) -> tuple[bytes, int]:
    try:
        from PIL import Image
    except ImportError:
        return pdf_bytes, 0

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        writer = PdfWriter(clone_from=reader)
        replaced_images = 0

        for page in writer.pages:
            images = list(page.images)
            for image in images:
                try:
                    pil_img = image.image
                    if pil_img.mode not in ("RGB", "L"):
                        pil_img = pil_img.convert("RGB")

                    width, height = pil_img.size
                    scale = min(
                        1.0,
                        _MERGE_LOSSY_MAX_WIDTH / max(1, width),
                        _MERGE_LOSSY_MAX_HEIGHT / max(1, height),
                    )
                    if scale < 1.0:
                        pil_img = pil_img.resize(
                            (
                                max(1, int(width * scale)),
                                max(1, int(height * scale)),
                            ),
                            Image.Resampling.LANCZOS,
                        )

                    image.replace(
                        pil_img,
                        quality=_MERGE_LOSSY_JPEG_QUALITY,
                        optimize=True,
                        progressive=True,
                    )
                    replaced_images += 1
                except Exception:
                    continue

            try:
                page.compress_content_streams()
            except Exception:
                pass

        output = BytesIO()
        try:
            writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
        except TypeError:
            writer.compress_identical_objects()
        except Exception:
            pass
        writer.write(output)
        lossy_pdf = output.getvalue()

        if not lossy_pdf:
            return pdf_bytes, 0

        # Keep lossy output only when it is smaller than original merged bytes.
        if len(lossy_pdf) < len(pdf_bytes):
            return lossy_pdf, replaced_images

        return pdf_bytes, replaced_images
    except Exception:
        return pdf_bytes, 0


def merge_files_to_single_pdf(file_items: list[dict[str, Any]]) -> tuple[bytes, list[str], list[str], int]:
    writer = PdfWriter()
    accepted: list[str] = []
    skipped: list[str] = []
    total_input_bytes = 0

    for item in file_items:
        file_name = str(item.get("file_name") or "document")
        mime_type = str(item.get("mime_type") or "")
        file_bytes = item.get("file_bytes") or b""

        if not isinstance(file_bytes, (bytes, bytearray)) or not file_bytes:
            skipped.append(file_name)
            continue

        kind = _detect_merge_file_kind(file_name, mime_type)
        if kind == "unsupported":
            skipped.append(file_name)
            continue

        try:
            if kind == "pdf":
                reader = PdfReader(BytesIO(bytes(file_bytes)))
            else:
                image_pdf_bytes = _image_to_pdf_bytes(file_name, bytes(file_bytes))
                reader = PdfReader(BytesIO(image_pdf_bytes))

            if not reader.pages:
                skipped.append(file_name)
                continue

            for page in reader.pages:
                try:
                    page.compress_content_streams()
                except Exception:
                    pass
                writer.add_page(page)

            total_input_bytes += len(file_bytes)
            accepted.append(file_name)
        except DocumentMergeError:
            raise
        except Exception as exc:
            raise DocumentMergeError(f"Failed to merge file: {file_name}") from exc

    if not accepted or len(writer.pages) == 0:
        raise DocumentMergeError("No supported files to merge. Allowed: PDF, JPG, JPEG, PNG, TIF, TIFF, BMP, WEBP")

    output = BytesIO()
    try:
        writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
    except TypeError:
        writer.compress_identical_objects()
    except Exception:
        pass
    writer.write(output)
    merged_pdf = output.getvalue()

    if not merged_pdf:
        raise DocumentMergeError("Merged PDF is empty")

    return merged_pdf, accepted, skipped, total_input_bytes


def create_merged_document(
    db: Session,
    claim_id: UUID,
    file_items: list[dict[str, Any]],
    uploaded_by: str | None,
    retention_class: str,
    compression_mode: str = "standard",
) -> tuple[DocumentResponse, list[str], list[str], int, int, int, float]:
    if not _claim_exists(db, claim_id):
        raise ClaimNotFoundError

    if not file_items:
        raise DocumentMergeError("No files provided for merge upload")

    merged_pdf_bytes, accepted_files, skipped_files, source_total_size_bytes = merge_files_to_single_pdf(file_items)

    requested_mode = str(compression_mode or "standard").strip().lower()
    applied_profile = "aggressive_image_optimize"
    if requested_mode == "lossy":
        lossy_pdf_bytes, replaced_images = _apply_lossy_pdf_recompress(merged_pdf_bytes)
        if len(lossy_pdf_bytes) < len(merged_pdf_bytes):
            merged_pdf_bytes = lossy_pdf_bytes
            applied_profile = "lossy_pdf_recompress"
        else:
            applied_profile = "lossy_requested_fallback"

    output_size_bytes = len(merged_pdf_bytes)
    saved_size_bytes = max(0, source_total_size_bytes - output_size_bytes)
    compression_ratio = round((output_size_bytes / source_total_size_bytes), 4) if source_total_size_bytes > 0 else 1.0

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    merged_name = f"claim_{str(claim_id)[:8]}_merged_{ts}.pdf"

    created = create_document(
        db=db,
        claim_id=claim_id,
        file_name=merged_name,
        mime_type="application/pdf",
        file_bytes=merged_pdf_bytes,
        uploaded_by=uploaded_by,
        retention_class=retention_class,
    )

    merge_meta = {
        "is_merged_pdf": True,
        "merge_source_file_count": len(file_items),
        "merge_accepted_file_count": len(accepted_files),
        "merge_skipped_file_count": len(skipped_files),
        "merge_accepted_files": accepted_files,
        "merge_skipped_files": skipped_files,
        "merged_source_total_size_bytes": source_total_size_bytes,
        "merged_output_size_bytes": output_size_bytes,
        "merged_saved_size_bytes": saved_size_bytes,
        "merge_compression_ratio": compression_ratio,
        "merge_profile": applied_profile,
        "merge_image_max_width": _MERGE_IMAGE_MAX_WIDTH,
        "merge_image_max_height": _MERGE_IMAGE_MAX_HEIGHT,
        "merge_image_jpeg_quality": _MERGE_IMAGE_JPEG_QUALITY,
        "merge_image_pdf_dpi": _MERGE_IMAGE_PDF_DPI,
        "merge_mode_requested": requested_mode,
    }

    db.execute(
        text(
            """
            UPDATE claim_documents
            SET metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:merge_meta AS jsonb)
            WHERE id = :document_id
            """
        ),
        {
            "document_id": str(created.id),
            "merge_meta": json.dumps(merge_meta),
        },
    )

    row = db.execute(
        text(
            """
            SELECT
                id,
                claim_id,
                storage_key,
                file_name,
                mime_type,
                file_size_bytes,
                checksum_sha256,
                parse_status,
                page_count,
                retention_class,
                uploaded_by,
                uploaded_at,
                parsed_at,
                metadata
            FROM claim_documents
            WHERE id = :document_id
            """
        ),
        {"document_id": str(created.id)},
    ).mappings().one()
    document = _to_document_response(dict(row))

    _emit_workflow_event(
        db=db,
        claim_id=claim_id,
        event_type="document_merge_uploaded",
        actor_id=uploaded_by,
        payload={
            "document_id": str(document.id),
            "accepted_files": accepted_files,
            "skipped_files": skipped_files,
            "source_file_count": len(file_items),
            "accepted_file_count": len(accepted_files),
            "skipped_file_count": len(skipped_files),
            "merged_source_total_size_bytes": source_total_size_bytes,
            "merged_output_size_bytes": output_size_bytes,
            "merged_saved_size_bytes": saved_size_bytes,
            "merge_compression_ratio": compression_ratio,
        "merge_profile": applied_profile,
        "merge_image_max_width": _MERGE_IMAGE_MAX_WIDTH,
        "merge_image_max_height": _MERGE_IMAGE_MAX_HEIGHT,
        "merge_image_jpeg_quality": _MERGE_IMAGE_JPEG_QUALITY,
        "merge_image_pdf_dpi": _MERGE_IMAGE_PDF_DPI,
        "merge_mode_requested": requested_mode,
        },
    )
    db.commit()

    return (
        document,
        accepted_files,
        skipped_files,
        source_total_size_bytes,
        output_size_bytes,
        saved_size_bytes,
        compression_ratio,
    )


def list_documents(db: Session, claim_id: UUID, limit: int, offset: int) -> DocumentListResponse:
    if not _claim_exists(db, claim_id):
        raise ClaimNotFoundError

    try:
        ensure_legacy_documents_materialized(db, claim_id)
    except Exception:
        # Never block document listing because of legacy payload parsing/materialization errors.
        db.rollback()
    params = {"claim_id": str(claim_id), "limit": limit, "offset": offset}
    total = db.execute(
        text("SELECT COUNT(*) FROM claim_documents WHERE claim_id = :claim_id"),
        params,
    ).scalar_one()

    rows = db.execute(
        text(
            """
            SELECT
                id,
                claim_id,
                storage_key,
                file_name,
                mime_type,
                file_size_bytes,
                checksum_sha256,
                parse_status,
                page_count,
                retention_class,
                uploaded_by,
                uploaded_at,
                parsed_at,
                metadata
            FROM claim_documents
            WHERE claim_id = :claim_id
            ORDER BY uploaded_at DESC
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).mappings().all()

    items = [_to_document_response(dict(r)) for r in rows]
    return DocumentListResponse(total=total, items=items)


def update_document_parse_status(
    db: Session,
    document_id: UUID,
    payload: DocumentParseStatusUpdateRequest,
) -> DocumentResponse:
    row = db.execute(
        text(
            """
            UPDATE claim_documents
            SET parse_status = :parse_status,
                parsed_at = CASE WHEN :parse_status = 'succeeded' THEN NOW() ELSE parsed_at END
            WHERE id = :document_id
            RETURNING
                id,
                claim_id,
                storage_key,
                file_name,
                mime_type,
                file_size_bytes,
                checksum_sha256,
                parse_status,
                page_count,
                retention_class,
                uploaded_by,
                uploaded_at,
                parsed_at,
                metadata
            """
        ),
        {"document_id": str(document_id), "parse_status": payload.parse_status.value},
    ).mappings().first()

    if row is None:
        db.rollback()
        raise DocumentNotFoundError

    document = _to_document_response(dict(row))
    _emit_workflow_event(
        db=db,
        claim_id=document.claim_id,
        event_type="document_parse_status_updated",
        actor_id=payload.actor_id,
        payload={
            "document_id": str(document.id),
            "parse_status": document.parse_status.value,
            "note": payload.note,
        },
    )
    db.commit()
    return document


def get_document_download_url(
    db: Session,
    document_id: UUID,
    expires_in: int,
) -> DocumentDownloadUrlResponse:
    row = db.execute(
        text("SELECT id, storage_key, metadata FROM claim_documents WHERE id = :document_id"),
        {"document_id": str(document_id)},
    ).mappings().first()

    if row is None:
        raise DocumentNotFoundError

    storage_key = str(row.get("storage_key") or "")
    metadata = _normalize_metadata(row.get("metadata"))
    direct_url = _first_direct_download_url(storage_key, metadata)
    if direct_url:
        return DocumentDownloadUrlResponse(
            document_id=row["id"],
            storage_key=storage_key,
            download_url=direct_url,
            expires_in=expires_in,
        )

    url = generate_download_url(object_key=storage_key, expires_in=expires_in)
    return DocumentDownloadUrlResponse(
        document_id=row["id"],
        storage_key=storage_key,
        download_url=url,
        expires_in=expires_in,
    )


def delete_documents(
    db: Session,
    claim_id: UUID,
    document_ids: list[UUID],
    actor_id: str | None,
) -> DocumentBulkDeleteResponse:
    if not _claim_exists(db, claim_id):
        raise ClaimNotFoundError

    unique_doc_ids: list[UUID] = []
    seen: set[str] = set()
    for doc_id in document_ids:
        key = str(doc_id)
        if key in seen:
            continue
        seen.add(key)
        unique_doc_ids.append(doc_id)

    if not unique_doc_ids:
        return DocumentBulkDeleteResponse(
            claim_id=claim_id,
            requested=0,
            deleted=0,
            failed=0,
            not_found=0,
            deleted_document_ids=[],
            failed_document_ids=[],
            not_found_document_ids=[],
        )

    select_query = text(
        """
        SELECT id, storage_key, metadata
        FROM claim_documents
        WHERE claim_id = :claim_id
          AND id IN :document_ids
        """
    ).bindparams(bindparam("document_ids", expanding=True))

    rows = db.execute(
        select_query,
        {
            "claim_id": str(claim_id),
            "document_ids": [str(doc_id) for doc_id in unique_doc_ids],
        },
    ).mappings().all()

    found_map = {str(r["id"]): r for r in rows}
    not_found_ids = [doc_id for doc_id in unique_doc_ids if str(doc_id) not in found_map]

    deletable_ids: list[UUID] = []
    failed_ids: list[UUID] = []

    for row in rows:
        doc_uuid = row["id"]
        metadata = _normalize_metadata(row.get("metadata"))
        direct_url = _first_direct_download_url(str(row.get("storage_key") or ""), metadata)
        if direct_url and (
            str(metadata.get("storage_provider") or "").strip().lower() in {"external_link", "legacy_external"}
            or str(row.get("storage_key") or "").strip().lower().startswith("legacy-external/")
            or _normalize_http_url(row.get("storage_key"))
        ):
            deletable_ids.append(doc_uuid)
            continue
        try:
            delete_object(row["storage_key"])
            deletable_ids.append(doc_uuid)
        except (StorageConfigError, StorageOperationError):
            failed_ids.append(doc_uuid)

    deleted_ids: list[UUID] = []
    if deletable_ids:
        delete_query = text(
            """
            DELETE FROM claim_documents
            WHERE claim_id = :claim_id
              AND id IN :document_ids
            RETURNING id
            """
        ).bindparams(bindparam("document_ids", expanding=True))

        deleted_rows = db.execute(
            delete_query,
            {
                "claim_id": str(claim_id),
                "document_ids": [str(doc_id) for doc_id in deletable_ids],
            },
        ).mappings().all()
        deleted_ids = [r["id"] for r in deleted_rows]

        _emit_workflow_event(
            db=db,
            claim_id=claim_id,
            event_type="document_deleted",
            actor_id=actor_id,
            payload={
                "deleted_document_ids": [str(doc_id) for doc_id in deleted_ids],
                "requested": len(unique_doc_ids),
                "deleted": len(deleted_ids),
                "failed": len(failed_ids),
                "not_found": len(not_found_ids),
            },
        )

    db.commit()

    return DocumentBulkDeleteResponse(
        claim_id=claim_id,
        requested=len(unique_doc_ids),
        deleted=len(deleted_ids),
        failed=len(failed_ids),
        not_found=len(not_found_ids),
        deleted_document_ids=deleted_ids,
        failed_document_ids=failed_ids,
        not_found_document_ids=not_found_ids,
    )

















