from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import audit_ocr as audit  # noqa: E402


class OcrAuditTests(unittest.TestCase):
    @staticmethod
    def fake_page(text: str, number: int = 1) -> audit.PdfPage:
        normalized = audit.normalize_with_mapping(text)
        chars = [audit.CharPosition(float(index), 0.0, float(index + 1), 10.0) for index in range(len(text))]
        tokens = audit.semantic_tokens(normalized, chars)
        return audit.PdfPage(number, text, chars, (100.0, 100.0), tokens, [])

    def test_cli_default_merged_path_uses_safe_pdf_stem(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "RBC notes (2024).pdf"
            pdf.write_bytes(b"synthetic pdf")
            report = {"status": "clear", "execution": {"ok": True}}
            with patch.object(audit, "run_audit", return_value=report) as run:
                self.assertEqual(audit.main([str(pdf), str(root)]), 0)
        self.assertEqual(run.call_args.args[3].name, "RBC_notes_2024.md")

    def test_normalization_unifies_ligatures_line_breaks_math_and_scripts(self) -> None:
        self.assertEqual(
            audit.normalize_text("ﬁnal co-\noperate σ_z⁻² α ≤ β − 1"),
            audit.normalize_text(r"final cooperate \\sigma_z^{-2} \\alpha <= \\beta - 1"),
        )
        self.assertEqual(audit.normalize_text("x₁²"), "x 1 2")
        self.assertEqual(audit.normalize_text("∞"), audit.normalize_text(r"\infty"))

    def test_page_21_math_symbol_substitution_is_reported(self) -> None:
        reference = "Context before the equation is σ_z⁻² and the context after agrees."
        extracted = r"Context before the equation is \sigma_\mu^{-2} and the context after agrees."
        reference_tokens = audit.semantic_tokens(audit.normalize_with_mapping(reference))
        extracted_tokens = audit.semantic_tokens(audit.normalize_with_mapping(extracted))
        findings = audit.compare_token_sequences(
            reference_tokens,
            extracted_tokens,
            reference,
            extracted,
            21,
            "nougat_body",
            audit.formula_spans(reference),
            audit.formula_spans(extracted),
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["page"], 21)
        self.assertEqual(findings[0]["type"], "mathematical_symbol")
        self.assertEqual(findings[0]["reference"], "z")
        self.assertEqual(findings[0]["extracted"], r"\mu")

    def test_plain_equation_identifier_substitution_is_mathematical(self) -> None:
        reference = "The equation x = 1 remains stable in the surrounding text today."
        extracted = "The equation y = 1 remains stable in the surrounding text today."
        findings = audit.compare_token_sequences(
            audit.semantic_tokens(audit.normalize_with_mapping(reference)),
            audit.semantic_tokens(audit.normalize_with_mapping(extracted)),
            reference,
            extracted,
            4,
            "nougat_body",
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "mathematical_symbol")

    def test_corrected_or_identical_unusual_expression_is_not_reported(self) -> None:
        pairs = (
            ("Context z σ_z⁻² after", r"Context z \\sigma_z^{-2} after"),
            ("An unusual authorial expression qzx 17 remains.", "An unusual authorial expression qzx 17 remains."),
        )
        for reference, extracted in pairs:
            with self.subTest(reference=reference):
                findings = audit.compare_token_sequences(
                    audit.semantic_tokens(audit.normalize_with_mapping(reference)),
                    audit.semantic_tokens(audit.normalize_with_mapping(extracted)),
                    reference,
                    extracted,
                    1,
                    "nougat_body",
                    audit.formula_spans(reference),
                    audit.formula_spans(extracted),
                )
                self.assertEqual(findings, [])

    def test_prose_and_digit_substitutions_have_distinct_types(self) -> None:
        for reference, extracted, expected in (
            ("The stable phrase remains around the model output and the conclusion agrees.", "The stable phrase remains around the model result and the conclusion agrees.", "prose"),
            ("The stable estimate is 1.25 in the table.", "The stable estimate is 1.35 in the table.", "digit"),
        ):
            with self.subTest(expected=expected):
                findings = audit.compare_token_sequences(
                    audit.semantic_tokens(audit.normalize_with_mapping(reference)),
                    audit.semantic_tokens(audit.normalize_with_mapping(extracted)),
                    reference,
                    extracted,
                    1,
                    "nougat_body",
                )
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0]["type"], expected)

    def test_repeated_headers_are_excluded_from_page_alignment_candidates(self) -> None:
        pages = [
            self.fake_page("Graduate Macro Notes\n\nPage one has distinct content here."),
            self.fake_page("Graduate Macro Notes\n\nPage two has distinct content here.", 2),
        ]
        ranges = audit.repeated_header_ranges(pages)
        self.assertEqual(len(ranges[1]), 1)
        self.assertEqual(len(ranges[2]), 1)
        self.assertEqual(pages[1].text[ranges[2][0][0]:ranges[2][0][1]].strip(), "Graduate Macro Notes")

    def test_positioned_scripts_are_reordered_to_the_normalized_source_order(self) -> None:
        tokens = [
            audit.Token("k", 0, 1, bbox=(0.0, 0.0, 5.0, 10.0)),
            audit.Token("alpha", 1, 2, bbox=(5.0, 8.0, 8.0, 14.0)),
            audit.Token("t", 2, 3, bbox=(5.0, 2.0, 8.0, 8.0)),
        ]
        reordered = audit.reorder_positioned_scripts(tokens)
        self.assertEqual([token.value for token in reordered], ["k", "t", "alpha"])

    def test_ambiguous_page_alignment_is_marked_partial(self) -> None:
        page = self.fake_page(
            "repeat one two three repeat one two three repeat one two three repeat one two three"
        )
        nougat = audit.semantic_tokens(audit.normalize_with_mapping(page.text))
        alignments, coverage = audit.align_pages([page], nougat)
        self.assertEqual(alignments, [])
        self.assertEqual(coverage, {1: "partial"})

    def test_review_resolution_suppresses_only_the_same_fingerprint(self) -> None:
        pdf_text = "Context before the equation is σ_z⁻² and the context after agrees."
        nougat_text = r"Context before the equation is \sigma_\mu^{-2} and the context after agrees."
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "paper.pdf"
            nougat = root / "nougat.mmd"
            combined = root / "combined.md"
            pdf.write_bytes(b"synthetic pdf")
            nougat.write_text(nougat_text, encoding="utf-8")
            combined.write_text(nougat_text, encoding="utf-8")
            with patch.object(audit, "extract_pdf_pages", return_value=[self.fake_page(pdf_text, 1)]):
                first = audit.run_audit(pdf, nougat, None, combined, root)
            self.assertEqual(first["status"], "review_required")
            finding_id = first["findings"][0]["id"]
            self.assertEqual(audit.resolve_finding(root, finding_id, "source_matches"), 0)

            with patch.object(audit, "extract_pdf_pages", return_value=[self.fake_page(pdf_text, 1)]):
                same = audit.run_audit(pdf, nougat, None, combined, root)
            self.assertEqual(same["status"], "review_required")
            self.assertEqual(same["completeness"], "unknown")
            self.assertFalse(same["findings"][0]["active"])

            changed = r"Context before the equation is \sigma_\nu^{-2} and the context after agrees."
            # A final-only edit must invalidate the affected resolution.
            combined.write_text(changed, encoding="utf-8")
            with patch.object(audit, "extract_pdf_pages", return_value=[self.fake_page(pdf_text, 1)]):
                changed_report = audit.run_audit(pdf, nougat, None, combined, root)
            self.assertEqual(changed_report["status"], "review_required")
            self.assertTrue(any(item.get("active", True) for item in changed_report["findings"]))

    def test_repeated_same_page_substitutions_remain_distinct(self) -> None:
        pdf_text = (
            "First stable context has estimate 1.25 and several matching words after it today. "
            "Second different context has estimate 1.25 and several matching words after it tomorrow."
        )
        nougat_text = (
            "First stable context has estimate 1.35 and several matching words after it today. "
            "Second different context has estimate 1.35 and several matching words after it tomorrow."
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "paper.pdf"
            nougat = root / "nougat.mmd"
            combined = root / "combined.md"
            pdf.write_bytes(b"synthetic pdf")
            nougat.write_text(nougat_text, encoding="utf-8")
            combined.write_text(nougat_text, encoding="utf-8")
            with patch.object(audit, "extract_pdf_pages", return_value=[self.fake_page(pdf_text)]):
                first = audit.run_audit(pdf, nougat, None, combined, root)

            self.assertEqual(first["summary"]["detected_findings"], 2)
            self.assertEqual(len({finding["id"] for finding in first["findings"]}), 2)
            self.assertEqual(audit.resolve_finding(root, first["findings"][0]["id"], "false_positive"), 0)

            with patch.object(audit, "extract_pdf_pages", return_value=[self.fake_page(pdf_text)]):
                second = audit.run_audit(pdf, nougat, None, combined, root)
            self.assertEqual(second["status"], "review_required")
            self.assertEqual(second["summary"]["active_findings"], 1)

    def test_clear_audit_persists_machine_report_human_report_and_empty_ledger(self) -> None:
        text = "A stable exact phrase remains in this document without substitutions."
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "paper.pdf"
            nougat = root / "nougat.mmd"
            combined = root / "combined.md"
            pdf.write_bytes(b"synthetic pdf")
            nougat.write_text(text, encoding="utf-8")
            combined.write_text(text, encoding="utf-8")
            with patch.object(audit, "extract_pdf_pages", return_value=[self.fake_page(text, 1)]):
                report = audit.run_audit(pdf, nougat, None, combined, root)
            self.assertEqual(report["status"], "clear")
            self.assertTrue((root / "ocr_audit.json").is_file())
            self.assertTrue((root / "ocr_audit.md").is_file())
            self.assertEqual(
                json.loads((root / "ocr_audit_review.json").read_text()),
                {"resolutions": {}, "schema": "1"},
            )

    def test_formula_not_decoded_placeholder_is_not_docling_evidence(self) -> None:
        docling = "<!-- formula-not-decoded -->\n\nFigure 1: A useful caption\n"
        combined = "The body.\n\n## Figures\n\n![A useful caption](figures/figure-001.png)\n"
        evidence = audit.extract_docling_evidence(docling, combined)
        self.assertEqual([item.origin for item in evidence], ["docling_caption"])
        self.assertNotIn("formula", " ".join(item.text for item in evidence).lower())

    def test_unavailable_pdf_text_layer_uses_docling_for_prose_only(self) -> None:
        reference = "The stable prose anchor remains in this document and the conclusion agrees today now."
        extracted = "The stable prose anchor remains in this document and the conclusion changes today now."
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "scan.pdf"
            nougat = root / "nougat.mmd"
            docling = root / "docling.md"
            combined = root / "combined.md"
            pdf.write_bytes(b"synthetic scan")
            nougat.write_text(extracted, encoding="utf-8")
            docling.write_text(reference, encoding="utf-8")
            combined.write_text(extracted, encoding="utf-8")
            page = audit.PdfPage(1, "", [], (100.0, 100.0), [], [])
            with patch.object(audit, "extract_pdf_pages", return_value=[page]):
                report = audit.run_audit(pdf, nougat, docling, combined, root)
        self.assertEqual(report["coverage"]["1"], "unavailable")
        self.assertEqual(report["findings"][0]["origin"], "docling_prose_fallback")
        self.assertEqual(report["findings"][0]["content_origin"], "final_body")
        self.assertEqual(report["findings"][0]["type"], "prose")
        self.assertLessEqual(report["findings"][0]["confidence"], 0.74)
        self.assertEqual(report["findings"][0]["confidence_basis"], "lower-confidence-docling-fallback")
        self.assertIn("Mathematical coverage is limited", " ".join(report["limitations"]))

    def test_docling_fallback_skips_pages_with_pdf_text_coverage(self) -> None:
        page_one_nougat = "The stable prose anchor remains in this document and the conclusion agrees today now."
        page_one_docling = "The stable prose anchor remains in this document and the conclusion changes today now."
        page_two = "Another reliable passage remains available for fallback comparison on page two today."
        docling = f"<!-- page 1 -->\n{page_one_docling}\n<!-- page 2 -->\n{page_two}\n"
        nougat = f"{page_one_nougat}\n{page_two}\n"
        pages = [
            self.fake_page(page_one_nougat, 1),
            audit.PdfPage(2, "", [], (100.0, 100.0), [], []),
        ]

        self.assertEqual(audit.audit_docling_fallback(docling, nougat, pages, [2]), [])


if __name__ == "__main__":
    unittest.main()
