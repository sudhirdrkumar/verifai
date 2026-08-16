import json
import re


DOCUMENT_EXCLUSION_PATTERNS = (
    (re.compile(r"(?:^|[_\-\s])frm[_\-\s]*id(?:[_\-\s.]|$)", re.IGNORECASE), "Form ID document"),
    (re.compile(r"(?:^|[_\-\s])claim[_\-\s]*form(?:[_\-\s.]|$)", re.IGNORECASE), "Claim form template"),
    (
        re.compile(
            r"(?:^|[_\-\s])(kyc|e[_\-\s]*kyc|c[_\-\s]*kyc|aadhaar|aadhar|pan[_\-\s]*card|"
            r"passport|voter[_\-\s]*id|driving[_\-\s]*licen[cs]e)(?:[_\-\s.]|$)",
            re.IGNORECASE,
        ),
        "KYC/identity document",
    ),
    (re.compile(r"(?:^|[_\-\s])(cheque|check)(?:[_\-\s.]|$)", re.IGNORECASE), "Cheque document"),
)


def excluded_document_reason(file_name: str) -> str | None:
    normalized_name = str(file_name or "").strip()
    for pattern, reason in DOCUMENT_EXCLUSION_PATTERNS:
        if pattern.search(normalized_name):
            return reason
    return None


def group_documents_by_size(documents: list[dict], max_bytes: int, max_files: int) -> list[list[dict]]:
    remaining = list(documents)
    groups: list[list[dict]] = []
    while remaining:
        first = remaining.pop(0)
        if (
            first.get("mime_type") != "application/pdf"
            or int(first.get("file_size") or 0) > max_bytes
            or max_files < 2
        ):
            groups.append([first])
            continue

        group = [first]
        total_bytes = int(first.get("file_size") or 0)
        deferred = []
        for candidate in remaining:
            candidate_bytes = int(candidate.get("file_size") or 0)
            if (
                len(group) < max_files
                and candidate.get("mime_type") == "application/pdf"
                and total_bytes + candidate_bytes <= max_bytes
            ):
                group.append(candidate)
                total_bytes += candidate_bytes
            else:
                deferred.append(candidate)
        groups.append(group)
        remaining = deferred
    return groups


def parse_batched_ocr_response(response_text: str, expected_ids: set[str]) -> dict[str, str]:
    text = str(response_text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
        text = text.strip()

    payload = json.loads(text)
    documents = payload.get("documents") if isinstance(payload, dict) else None
    if not isinstance(documents, list):
        raise ValueError("Batch OCR response must contain a documents list")

    results: dict[str, str] = {}
    for item in documents:
        if not isinstance(item, dict):
            continue
        document_id = str(item.get("document_id") or "").strip()
        extracted_text = str(item.get("text") or "").strip()
        if document_id in expected_ids and extracted_text:
            results[document_id] = extracted_text
    return results
