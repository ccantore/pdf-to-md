from __future__ import annotations
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import audit_ocr as audit
import completeness as comp


def page(text, number=1):
    return audit.PdfPage(number, text, [], None, audit.semantic_tokens(audit.normalize_with_mapping(text)), [])


class CompletenessTests(unittest.TestCase):
    def test_missing_markers_override_matching_anchors(self):
        text = 'A stable and uniquely identifiable paragraph with meaningful information.'
        for kind in ['EMPTY', 'FAIL']:
            report = comp.reconcile([page(text, 25)], text + f'\n[MISSING_PAGE_{kind}:25]')
            self.assertEqual(report['status'], 'incomplete')
            self.assertEqual(report['pages'][0]['fidelity_coverage'], 'unavailable')
            self.assertEqual(report['pages'][0]['final_spans'], [])

    def test_exact_text_and_offsets(self):
        source = 'A stable uniquely identifiable paragraph with meaningful information.'
        text = 'Preface unrelated to content.\n' + source
        report = comp.reconcile([page(source)], text)
        self.assertEqual(report['status'], 'complete')
        start, end = report['pages'][0]['final_spans'][0]
        self.assertEqual(text[start:end], source.rstrip('.'))
        self.assertEqual(report['final_md_sha256'], comp.digest(text))

    def test_anchor_alone_does_not_prove_completeness(self):
        anchor = 'A stable uniquely identifiable paragraph with meaningful information.'
        source = anchor + ' A whole second section introduces previously unmentioned economic assumptions.'
        report = comp.reconcile([page(source)], anchor)
        self.assertNotEqual(report['status'], 'complete')
        self.assertEqual(report['pages'][0]['fidelity_coverage'], 'partial')

    def test_bounded_unmarked_omission(self):
        left = 'These households consume goods and supply productive labour every period. '
        right = 'The government collects taxes and issues bonds under a separate budget constraint.'
        source = left + 'Several additional equations describe household optimal choices over time. ' + right
        report = comp.reconcile([page(source)], left + right)
        self.assertEqual(report['status'], 'incomplete')

    def test_blank_scanned_and_ambiguous_pages_remain_unknown(self):
        for source, text in [('', ''), ('Table 1 2 3', 'Table 1 2 3'),
                             ('A uniquely informative stable sequence appears several times.',
                              'A uniquely informative stable sequence appears several times. ' * 2)]:
            self.assertEqual(comp.reconcile([page(source)], text)['status'], 'unknown')

    def test_table_only_page_requires_all_text(self):
        source = 'Table S3 Household welfare consumption employment capital policy baseline alternative'
        report = comp.reconcile([page(source)], source)
        self.assertEqual(report['status'], 'complete')
        self.assertIn('text layer only', report['coverage_scope'])

    def test_fallback_still_detects_markers(self):
        report = comp.fallback('[MISSING_PAGE_FAIL:12]', 'hash', 'unavailable')
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['missing_markers'][0]['page'], 12)

    def ledger(self, text):
        old = '[MISSING_PAGE_EMPTY:2]'
        start = text.index(old)
        return {'schema': '1', 'pdf_sha256': 'hash', 'base_md_sha256': comp.digest(text),
                'replacements': [{'page': 2, 'original': old, 'span': [start, start+len(old)],
                                  'replacement': 'Verified recovered content.', 'method': 'transcription',
                                  'verification': {'source_page': 2, 'visually_verified': True,
                                                   'evidence': 'Inspected original page 2.'}}]}

    def test_verified_recovery_is_reproducible_and_hash_bound(self):
        text = 'Before\n[MISSING_PAGE_EMPTY:2]\nAfter'
        ledger = self.ledger(text)
        output, provenance = comp.apply_recoveries(text, 'hash', ledger)
        self.assertIn('Verified recovered content.', output)
        self.assertEqual(provenance[0]['page'], 2)
        self.assertEqual(comp.apply_recoveries(text, 'hash', ledger)[0], output)
        for changed_text, changed_hash in [(text+'x', 'hash'), (text, 'other')]:
            with self.assertRaises(ValueError):
                comp.apply_recoveries(changed_text, changed_hash, ledger)

    def test_recovery_rejects_unverified_ambiguous_and_wrong_page(self):
        text = '[MISSING_PAGE_EMPTY:2]'
        for change in ['unverified', 'span', 'page', 'empty', 'duplicate']:
            ledger = self.ledger(text)
            item = ledger['replacements'][0]
            if change == 'unverified': item['verification']['visually_verified'] = False
            if change == 'span': item['span'] = [0, 1]
            if change == 'page': item['page'] = 3
            if change == 'empty': item['replacement'] = ''
            if change == 'duplicate': ledger['replacements'].append(dict(item))
            with self.assertRaises(ValueError): comp.apply_recoveries(text, 'hash', ledger)

    def test_numbering_is_advisory_and_supports_appendix(self):
        text = r'\tag{S1}' + '\n' + r'\tag{S3}' + '\nSee Equation (S2) and Table S4 in another paper.'
        diagnostics = comp.numbering_diagnostics(text)
        self.assertTrue(any(d['type'] == 'numbering_gap' for d in diagnostics))
        self.assertTrue(any(d.get('may_be_external') for d in diagnostics))

    def test_final_artifact_audit_and_resolution_cannot_clear_missing_pages(self):
        source = 'The context before this estimate is 1.25 and several matching words follow this result.'
        final = source.replace('1.25', '1.35') + '\n[MISSING_PAGE_FAIL:2]'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf, raw, merged = root/'paper.pdf', root/'nougat.mmd', root/'paper.md'
            pdf.write_bytes(b'synthetic')
            raw.write_text(source)
            merged.write_text(final)
            with patch.object(audit, 'extract_pdf_pages', return_value=[page(source), page('missing', 2)]):
                report = audit.run_audit(pdf, raw, None, merged, root)
            self.assertEqual(report['completeness'], 'incomplete')
            self.assertEqual(report['final_md_sha256'], comp.digest(final))
            self.assertTrue(report['findings'])
            self.assertEqual(report['findings'][0]['origin'], 'final_body')
            audit.resolve_finding(root, report['findings'][0]['id'], 'false_positive')
            resolved = json.loads((root/'ocr_audit.json').read_text())
            self.assertEqual(resolved['status'], 'review_required')


if __name__ == '__main__': unittest.main()
