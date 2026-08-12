"""
Stream large PDFs for extraction - process in chunks instead of loading entire file to memory.
"""
import io
import logging
from typing import Iterator

from pypdf import PdfReader

logger = logging.getLogger(__name__)

# Configuration
MAX_PDF_SIZE_MB = 100
CHUNK_SIZE_MB = 10
MAX_PAGES_PER_CHUNK = 10


class PDFStreamingError(Exception):
    pass


def validate_pdf_size(payload: bytes) -> None:
    """Check if PDF is too large to process."""
    size_mb = len(payload) / (1024 * 1024)
    if size_mb > MAX_PDF_SIZE_MB:
        raise PDFStreamingError(
            f"PDF too large: {size_mb:.1f}MB (max {MAX_PDF_SIZE_MB}MB)"
        )


def stream_pdf_chunks(
    payload: bytes,
    chunk_type: str = "pages",  # 'pages' or 'text'
) -> Iterator[dict]:
    """
    Stream PDF content in manageable chunks.

    Yields chunks of type:
    {
        'chunk_index': int,
        'total_chunks': int,
        'text': str,
        'page_range': (start, end),  # For page-based chunks
        'size_bytes': int,
    }
    """
    validate_pdf_size(payload)

    try:
        reader = PdfReader(io.BytesIO(payload))
    except Exception as exc:
        raise PDFStreamingError(f"PDF parsing failed: {exc}") from exc

    total_pages = len(reader.pages)
    if total_pages == 0:
        raise PDFStreamingError("PDF has no pages")

    if chunk_type == "pages":
        yield from _chunk_by_pages(reader, total_pages)
    else:
        yield from _chunk_by_text_size(reader, total_pages)


def _chunk_by_pages(reader: PdfReader, total_pages: int) -> Iterator[dict]:
    """Break PDF into chunks by page count."""
    chunks_count = (total_pages + MAX_PAGES_PER_CHUNK - 1) // MAX_PAGES_PER_CHUNK

    for chunk_idx in range(chunks_count):
        start_page = chunk_idx * MAX_PAGES_PER_CHUNK
        end_page = min(start_page + MAX_PAGES_PER_CHUNK, total_pages)

        text_parts = []
        for page_num in range(start_page, end_page):
            try:
                page_text = (reader.pages[page_num].extract_text() or "").strip()
                if page_text:
                    text_parts.append(page_text)
            except Exception as exc:
                logger.warning(f"Failed to extract page {page_num}: {exc}")
                continue

        chunk_text = "\n\n".join(text_parts)
        chunk_size = len(chunk_text.encode("utf-8"))

        yield {
            "chunk_index": chunk_idx,
            "total_chunks": chunks_count,
            "text": chunk_text,
            "page_range": (start_page, end_page),
            "pages": list(range(start_page, end_page)),
            "size_bytes": chunk_size,
        }


def _chunk_by_text_size(reader: PdfReader, total_pages: int) -> Iterator[dict]:
    """Break PDF into chunks by text size."""
    chunk_threshold = CHUNK_SIZE_MB * 1024 * 1024
    chunk_idx = 0
    current_text = ""
    start_page = 0

    for page_num in range(total_pages):
        try:
            page_text = (reader.pages[page_num].extract_text() or "").strip()
            if not page_text:
                continue
        except Exception as exc:
            logger.warning(f"Failed to extract page {page_num}: {exc}")
            continue

        potential_text = current_text + "\n\n" + page_text if current_text else page_text
        potential_size = len(potential_text.encode("utf-8"))

        # If adding this page exceeds chunk size, yield current chunk
        if potential_size > chunk_threshold and current_text:
            chunk_size = len(current_text.encode("utf-8"))
            yield {
                "chunk_index": chunk_idx,
                "total_chunks": -1,  # Unknown total
                "text": current_text,
                "page_range": (start_page, page_num),
                "pages": list(range(start_page, page_num)),
                "size_bytes": chunk_size,
            }
            chunk_idx += 1
            current_text = page_text
            start_page = page_num
        else:
            current_text = potential_text

    # Yield final chunk
    if current_text:
        chunk_size = len(current_text.encode("utf-8"))
        yield {
            "chunk_index": chunk_idx,
            "total_chunks": chunk_idx + 1,
            "text": current_text,
            "page_range": (start_page, total_pages),
            "pages": list(range(start_page, total_pages)),
            "size_bytes": chunk_size,
        }


def estimate_memory_usage(payload_bytes: int) -> dict[str, str]:
    """Estimate memory usage for processing a PDF."""
    file_size_mb = payload_bytes / (1024 * 1024)
    # Accounting for: file + parsed data + base64 encoding (1.33x)
    peak_usage_mb = file_size_mb * (1 + 0.5 + 1.33)  # Rough estimate

    return {
        "file_size": f"{file_size_mb:.1f}MB",
        "estimated_peak_memory": f"{peak_usage_mb:.1f}MB",
        "recommended_chunk_mode": "pages" if file_size_mb > 50 else "text",
    }
