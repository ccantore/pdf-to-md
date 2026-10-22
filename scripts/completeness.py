"""Conservative final-artifact reconciliation and verified page repairs.

No OCR is launched here. Character spans are half-open Python string offsets.
A matching text layer is evidence about text, never a visual fidelity claim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

MISSING = re.compile(r"\[MISSING_PAGE_(EMPTY|FAIL|POST)(?:\s*:\s*(\d+))?\]")
WARNING = "> **Conversion requires review:** content completeness is {status}. See `page_map.json` and `merge_report.json`.\n\n"


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def missing_markers(text):
    return [{"page": int(m[2]) if m[2] else None, "kind": m[1].lower(), "span": [m.start(), m.end()]} for m in MISSING.finditer(text)]


def apply_recoveries(text, pdf_hash, ledger):
    """Apply exact, unique replacements to the unmodified merged body.

    The ledger binds both the PDF and whole pre-recovery body; accepting an old
    patch against a new merge would silently transplant potentially stale text.
    """
    if not ledger:
        return text, []
    if ledger.get('schema') != '1' or ledger.get('pdf_sha256') != pdf_hash or ledger.get('base_md_sha256') != digest(text):
        raise ValueError('Recovery ledger is stale or has an unsupported schema')
    accepted = []
    edits = []
    seen_pages = set()
    for item in ledger.get('replacements', []):
        page = item.get('page')
        old, new = item.get('original'), item.get('replacement')
        verification = item.get('verification', {})
        if (not isinstance(page, int) or isinstance(page, bool) or page < 1 or page in seen_pages
                or not isinstance(old, str) or not old or text.count(old) != 1
                or not isinstance(new, str) or not new.strip() or missing_markers(new)
                or item.get('method') not in {'docling', 'targeted_extraction', 'transcription'}
                or verification.get('source_page') != page
                or verification.get('visually_verified') is not True
                or not verification.get('evidence')):
            raise ValueError('Recovery needs a unique boundary and original-page verification')
        markers = missing_markers(old)
        if any(m['page'] != page for m in markers):
            raise ValueError('Recovery boundary includes a different missing page')
        start = text.index(old)
        end = start + len(old)
        if item.get('span') != [start, end]:
            raise ValueError('Recovery span no longer matches its exact boundary')
        if any(start < b and end > a for a, b, _ in edits):
            raise ValueError('Recovery boundaries overlap')
        edits.append((start, end, new))
        seen_pages.add(page)
        accepted.append({"page": page, "method": item['method'], "original_span": [start, end],
                         "replacement_sha256": digest(new), "verification": verification})
    for start, end, new in sorted(edits, reverse=True):
        text = text[:start] + new + text[end:]
    return text, accepted


def numbering_diagnostics(text):
    """Heuristic prompts, not absence claims. External references stay advisory."""
    definitions = {'equation': set(), 'table': set()}
    for m in re.finditer(r'\\tag\{([A-Za-z]*\d+)\}|^\s*\(([A-Za-z]*\d+)\)\s*$', text, re.M):
        definitions['equation'].add(m[1] or m[2])
    for m in re.finditer(r'^\s*(?:#{1,6}\s*)?(?:\*\*)?Table\s+([A-Za-z]*\d+)\b', text, re.M | re.I):
        definitions['table'].add(m[1])
    prompts = []
    for kind, labels in definitions.items():
        groups = {}
        for label in labels:
            match = re.fullmatch(r'([A-Za-z]*)(\d+)', label)
            groups.setdefault(match[1].upper(), set()).add(int(match[2]))
        for prefix, nums in groups.items():
            ordered = sorted(nums)
            for left, right in zip(ordered, ordered[1:]):
                if right > left + 1:
                    prompts.append({'type': 'numbering_gap', 'kind': kind,
                                    'between': [f'{prefix}{left}', f'{prefix}{right}']})
    for match in re.finditer(r'\b(Table|Eq(?:uation)?\.?)\s*\(?([A-Za-z]*\d+)\)?', text, re.I):
        kind = 'table' if match[1].lower() == 'table' else 'equation'
        if match[2].upper() not in {x.upper() for x in definitions[kind]}:
            prompts.append({'type': 'unresolved_reference', 'kind': kind, 'label': match[2],
                            'span': [match.start(), match.end()], 'may_be_external': True})
    labels = set(re.findall(r'\\label\{([^}]+)\}', text))
    for match in re.finditer(r'\\(?:eqref|ref|autoref)\{([^}]+)\}', text):
        if match[1] not in labels:
            prompts.append({'type': 'unresolved_reference', 'kind': 'label', 'label': match[1],
                            'span': [match.start(), match.end()], 'may_be_external': True})
    return prompts


def _unmatched_runs(length, mapping):
    start = None
    for index in range(length + 1):
        if index < length and index not in mapping:
            if start is None:
                start = index
        elif start is not None:
            yield start, index
            start = None


def _remove_shared_claims(mappings):
    """Reject all competing owners; page iteration order must not pick a winner."""
    owners = defaultdict(list)
    for page_index, mapping in enumerate(mappings):
        for source, final in mapping.items():
            owners[final].append((page_index, source))
    ambiguous = set()
    for claims in owners.values():
        if len(claims) > 1:
            for page_index, source in claims:
                del mappings[page_index][source]
                ambiguous.add(page_index)
    return ambiguous


def reconcile(pages, text, pdf_hash=None, recoveries=()):
    import audit_ocr as audit
    tokens = audit.semantic_tokens(audit.normalize_with_mapping(text))
    values = [t.value for t in tokens]
    positions = defaultdict(list)
    for index, value in enumerate(values):
        positions[value].append(index)

    def occurrences(needle):
        return [start for start in positions[needle[0]]
                if values[start:start + len(needle)] == needle]

    alignments, _ = audit.align_pages(pages, tokens)
    by_page = {a.page_number: a for a in alignments}
    markers = missing_markers(text)
    mappings, deletions = [], []
    # Alignment windows intentionally overlap. They generate proposals, not
    # ownership: allocate final token occurrences jointly across all pages.
    for page in pages:
        mapping, bounded = {}, []
        alignment = by_page.get(page.number)
        if alignment and not any(m['page'] in {None, page.number} for m in markers):
            offset = alignment.nougat_start
            matcher = SequenceMatcher(None, [t.value for t in page.tokens],
                                      values[offset:alignment.nougat_end], autojunk=False)
            for block in matcher.get_matching_blocks():
                mapping.update((block.a + i, offset + block.b + i) for i in range(block.size))
            bounded = [(a, b) for tag, a, b, _, _ in matcher.get_opcodes()
                       if tag == 'delete' and b-a >= 5 and a > 0 and b < len(page.tokens)]
        mappings.append(mapping)
        deletions.append(bounded)
    ambiguous = _remove_shared_claims(mappings)
    used = {final for mapping in mappings for final in mapping.values()}

    # Captions and footnotes may be appended or otherwise relocated. Match
    # sufficiently long exact blocks anywhere in the remaining artifact, then
    # resolve competing relocation proposals together as well.
    relocated = [dict(mapping) for mapping in mappings]
    for index, page in enumerate(pages):
        if page.number not in by_page or any(m['page'] in {None, page.number} for m in markers):
            continue
        for start, end in _unmatched_runs(len(page.tokens), mappings[index]):
            if end - start < 5:
                continue
            needle = [t.value for t in page.tokens[start:end]]
            candidates = [pos for pos in occurrences(needle)
                          if not any(i in used for i in range(pos, pos + len(needle)))]
            if len(candidates) == 1:
                relocated[index].update((start+i, candidates[0]+i) for i in range(len(needle)))
    ambiguous.update(_remove_shared_claims(relocated))
    mappings = relocated
    used = {final for mapping in mappings for final in mapping.values()}
    available = Counter(value for index, value in enumerate(values) if index not in used)
    records = []
    for index, page in enumerate(pages):
        marks = [m for m in markers if m['page'] in {None, page.number}]
        mapping = mappings[index]
        # A tokenizer match fraction cannot account for characters it discards.
        # Fail closed on meaningful normalized symbols outside its vocabulary.
        normalized = audit.normalize_with_mapping(page.text)
        represented = {i for match in audit.SEMANTIC_TOKEN_RE.finditer(normalized.text)
                       for i in range(match.start(), match.end())}
        unsupported = sorted({char for i, char in enumerate(normalized.text)
                              if i not in represented and unicodedata.category(char)[0] in {'L', 'N', 'M', 'S'}
                              and not any(a <= normalized.spans[i][0] < b for a, b in page.ignored_ranges)})
        record = {'page': page.number, 'status': 'unknown', 'mapping_confidence': 'unavailable',
                  'final_spans': [], 'missing_markers': marks, 'matched_text_fraction': 0.0,
                  'fidelity_coverage': 'unavailable', 'uncompared_characters': unsupported,
                  'recovery': [r for r in recoveries if r['page'] == page.number]}
        if page.number in by_page and not marks:
            record['matched_text_fraction'] = round(len(mapping) / max(1, len(page.tokens)), 4)
            record['mapping_confidence'] = 'ambiguous_overlap' if index in ambiguous else 'unique_anchor'
            # Coalesce only consecutive final occurrences, including relocated
            # blocks; no span may claim another page's tokens.
            for final in sorted(mapping.values()):
                if record['final_spans'] and previous + 1 == final:
                    record['final_spans'][-1][1] = tokens[final].end
                else:
                    record['final_spans'].append([tokens[final].start, tokens[final].end])
                previous = final
            record['fidelity_coverage'] = 'partial'
            if len(mapping) == len(page.tokens) and page.tokens and not unsupported:
                record['status'] = 'complete'
                record['mapping_confidence'] = 'unique_anchor'
                if not audit.code_like_page(page):
                    record['fidelity_coverage'] = 'full'
            # A monotonic deletion alone is not evidence of absence. Exact
            # occurrences elsewhere, or enough unclaimed tokens in another
            # order, require uncertainty rather than an omission claim.
            for start, end in deletions[index]:
                remaining = [page.tokens[i].value for i in range(start, end) if i not in mapping]
                needle = [t.value for t in page.tokens[start:end]]
                if remaining and not occurrences(needle) and Counter(remaining) - available:
                    record['status'] = 'incomplete'
        if marks:
            attributed = any(m['page'] == page.number for m in marks)
            record['status'] = 'incomplete' if attributed else 'unknown'
            record['mapping_confidence'] = 'explicit_missing_marker' if attributed else 'unattributed_missing_marker'
        records.append(record)
    status = 'incomplete' if markers or any(r['status'] == 'incomplete' for r in records) else (
        'complete' if records and all(r['status'] == 'complete' for r in records) else 'unknown')
    return {'schema': '1', 'pdf_sha256': pdf_hash, 'final_md_sha256': digest(text),
            'status': status, 'pages': records, 'missing_markers': markers,
            'coverage_scope': 'PDF text layer only; visual layout, images and unencoded mathematics remain unverified.',
            'numbering_diagnostics': numbering_diagnostics(text)}


def fallback(text, pdf_hash, error):
    result = reconcile([], text, pdf_hash)
    result['error'] = error
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', type=Path)
    parser.add_argument('markdown', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    import audit_ocr as audit
    text = args.markdown.read_text(encoding='utf-8')
    pdf_hash = hashlib.sha256(args.pdf.read_bytes()).hexdigest()
    try:
        result = reconcile(audit.extract_pdf_pages(args.pdf), text, pdf_hash)
    except Exception as exc:
        result = fallback(text, pdf_hash, str(exc))
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
