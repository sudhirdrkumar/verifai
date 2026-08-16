import json
import unittest

from backend.ocr_batching import (
    excluded_document_reason,
    group_documents_by_size,
    parse_batched_ocr_response,
)


class DocumentExclusionTests(unittest.TestCase):
    def test_configured_document_types_are_excluded(self):
        names = (
            "PUN01885708_FRMID.PDF",
            "claim_form_template.pdf",
            "patient_KYC.pdf",
            "cancelled_Cheque.jpeg",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIsNotNone(excluded_document_reason(name))

    def test_clinical_names_are_not_excluded(self):
        for name in ("discharge_summary.pdf", "lab_checkup_results.pdf", "claim_bill.pdf"):
            with self.subTest(name=name):
                self.assertIsNone(excluded_document_reason(name))


class BatchGroupingTests(unittest.TestCase):
    def test_pdfs_are_grouped_under_size_limit(self):
        mb = 1024 * 1024
        documents = [
            {"document_id": "1", "mime_type": "application/pdf", "file_size": 3 * mb},
            {"document_id": "2", "mime_type": "application/pdf", "file_size": 3 * mb},
            {"document_id": "3", "mime_type": "application/pdf", "file_size": 3 * mb},
        ]
        groups = group_documents_by_size(documents, max_bytes=7 * mb, max_files=4)
        self.assertEqual([["1", "2"], ["3"]], [[d["document_id"] for d in group] for group in groups])

    def test_oversized_pdf_and_image_remain_single(self):
        mb = 1024 * 1024
        documents = [
            {"document_id": "large", "mime_type": "application/pdf", "file_size": 8 * mb},
            {"document_id": "image", "mime_type": "image/jpeg", "file_size": 1 * mb},
        ]
        groups = group_documents_by_size(documents, max_bytes=7 * mb, max_files=4)
        self.assertEqual([["large"], ["image"]], [[d["document_id"] for d in group] for group in groups])


class BatchResponseTests(unittest.TestCase):
    def test_response_is_mapped_to_expected_document_ids(self):
        payload = {
            "documents": [
                {"document_id": "doc-1", "text": "first text"},
                {"document_id": "doc-2", "text": "second text"},
                {"document_id": "unexpected", "text": "ignored"},
            ]
        }
        result = parse_batched_ocr_response(json.dumps(payload), {"doc-1", "doc-2"})
        self.assertEqual({"doc-1": "first text", "doc-2": "second text"}, result)


if __name__ == "__main__":
    unittest.main()
