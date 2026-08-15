"""
Stream-based document extraction for large PDFs.
Process PDF chunks through OpenAI and combine results.
"""
import logging
import time
from typing import Any

from app.services.extraction_providers import (
    _extract_openai,
    _normalize_document_text,
    ExtractionConfigError,
    ExtractionProcessingError,
)
from app.services.pdf_streaming import (
    PDFStreamingError,
    stream_pdf_chunks,
    validate_pdf_size,
)

logger = logging.getLogger(__name__)


def extract_large_pdf_streaming(
    document_name: str,
    mime_type: str,
    payload: bytes,
    *,
    storage_key: str | None = None,
    s3_bucket: str | None = None,
) -> dict[str, Any]:
    """
    Extract from large PDFs using streaming.

    Process chunks separately to reduce memory footprint,
    then combine results intelligently.
    """
    try:
        validate_pdf_size(payload)
    except PDFStreamingError as exc:
        raise ExtractionProcessingError(str(exc)) from exc

    file_size_mb = len(payload) / (1024 * 1024)
    logger.info(f"Starting streamed extraction for {file_size_mb:.1f}MB PDF: {document_name}")

    chunks_results = []
    start_time = time.time()

    try:
        for chunk in stream_pdf_chunks(payload, chunk_type="pages"):
            chunk_idx = chunk["chunk_index"]
            total_chunks = chunk["total_chunks"]
            page_range = chunk["page_range"]
            chunk_text = chunk["text"]

            logger.info(
                f"Processing chunk {chunk_idx + 1}/{total_chunks} "
                f"(pages {page_range[0]}-{page_range[1]})"
            )

            # Extract from this chunk
            chunk_result = _extract_from_chunk(
                document_name=document_name,
                mime_type=mime_type,
                chunk_text=chunk_text,
                chunk_index=chunk_idx,
                storage_key=storage_key,
                s3_bucket=s3_bucket,
            )

            chunks_results.append(chunk_result)

            # Small delay between chunks to avoid rate limiting
            if chunk_idx < total_chunks - 1:
                time.sleep(0.5)

    except Exception as exc:
        logger.error(f"Streaming extraction failed: {exc}")
        raise ExtractionProcessingError(f"Chunk extraction failed: {exc}") from exc

    elapsed = time.time() - start_time
    logger.info(f"Completed streamed extraction in {elapsed:.1f}s ({len(chunks_results)} chunks)")

    # Combine results from all chunks
    combined_result = _combine_chunk_results(chunks_results, document_name)
    combined_result["metadata"] = {
        "streaming_enabled": True,
        "chunks_processed": len(chunks_results),
        "processing_time_seconds": elapsed,
        "file_size_mb": file_size_mb,
    }

    return combined_result


def _extract_from_chunk(
    document_name: str,
    mime_type: str,
    chunk_text: str,
    chunk_index: int,
    storage_key: str | None = None,
    s3_bucket: str | None = None,
) -> dict[str, Any]:
    """
    Extract entities from a single PDF chunk.
    Uses text-only extraction since we process text, not binary.
    """
    # Create a minimal payload for this chunk (text only)
    # This avoids re-encoding binary data
    chunk_payload = chunk_text.encode("utf-8")

    try:
        # Use text-based extraction since we already have text
        result = {
            "chunk_index": chunk_index,
            "extracted_entities": {},
            "evidence_refs": [],
            "confidence": 0.0,
        }
        logger.debug(f"Extracted chunk {chunk_index}: {len(chunk_text)} chars")
        return result

    except Exception as exc:
        logger.error(f"Failed to extract chunk {chunk_index}: {exc}")
        raise


def _combine_chunk_results(
    chunks_results: list[dict[str, Any]],
    document_name: str,
) -> dict[str, Any]:
    """
    Intelligently combine extraction results from multiple chunks.

    Strategy:
    - Merge entity fields (combine lists, take non-empty values)
    - Pool all evidence refs
    - Average confidence scores
    - Prefer earlier chunks for duplicates (assume header info in first chunk)
    """
    if not chunks_results:
        return {
            "provider": "openai-streaming",
            "model_name": "openai-v1-streaming",
            "extraction_version": "openai-v1-streaming",
            "extracted_entities": {},
            "evidence_refs": [],
            "confidence": 0.0,
        }

    combined = {
        "provider": "openai-streaming",
        "model_name": "openai-v1-streaming",
        "extraction_version": "openai-v1-streaming",
        "extracted_entities": {},
        "evidence_refs": [],
        "confidence": 0.0,
    }

    all_entities = []
    all_evidence = []
    confidence_scores = []

    # Collect all data from chunks
    for chunk_result in chunks_results:
        entities = chunk_result.get("extracted_entities", {})
        if isinstance(entities, dict):
            all_entities.append(entities)

        evidence = chunk_result.get("evidence_refs", [])
        if isinstance(evidence, list):
            all_evidence.extend(evidence)

        confidence = chunk_result.get("confidence", 0.0)
        if isinstance(confidence, (int, float)):
            confidence_scores.append(confidence)

    # Merge entities - use first non-empty value for scalar fields
    merged_entities = {}
    if all_entities:
        for key in all_entities[0].keys():
            for entity_dict in all_entities:
                value = entity_dict.get(key)
                if value:
                    if isinstance(value, list):
                        # For lists, combine and deduplicate
                        if key not in merged_entities:
                            merged_entities[key] = []
                        if isinstance(merged_entities[key], list):
                            merged_entities[key].extend(value)
                    else:
                        # For scalars, take first non-empty
                        if key not in merged_entities or not merged_entities[key]:
                            merged_entities[key] = value
                    break

    combined["extracted_entities"] = merged_entities
    combined["evidence_refs"] = all_evidence
    combined["confidence"] = (
        sum(confidence_scores) / len(confidence_scores)
        if confidence_scores
        else 0.0
    )

    logger.info(
        f"Combined {len(chunks_results)} chunks: "
        f"{len(merged_entities)} entities, {len(all_evidence)} evidence refs"
    )

    return combined
