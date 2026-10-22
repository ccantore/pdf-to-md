# Verified page recovery

Recovery is an agent-guided operation, not an automatic fallback from Nougat to
Docling. Never transplant unverified formulas or table cells. Keep confidential
source files, page images and ledgers in the local job area.

1. Read the page map and inspect the indicated original PDF page. Check cached
   Docling text/layout for the same content. A blank PDF text layer does not mean
   the page is visually blank.
2. If cached evidence is insufficient, extract only the affected source page to
   a temporary PDF, retaining its original page number in your notes. Run at
   most one targeted Nougat/Docling retry using the existing dedicated runtime.
   Inspect the installed command's help for supported page-selection options;
   do not assume them. Never run this retry inside `--merge-only`.
3. Compare the candidate with the source visually. Verify equations, subscripts,
   table headings and cells. Transcribe manually if feasible. Record the method
   and what was checked. Uncertain content stays incomplete; report it honestly.
4. Construct the ledger below against the merged body **before recovery and
   before the generated completeness warning**, including any YAML metadata.
   Each accepted merge writes that exact base to `recovery/base.md`. Hash its UTF-8
   contents. `original` must
   occur exactly once and `span` must identify its half-open character offsets.
5. Rerun the same merge command with `--recovery-ledger /local/recovery.json`.
   Inspect the final Markdown, page map and audit. The original OCR cache stays
   intact; repeating the merge applies the same repairs exactly once.

```json
{
  "schema": "1",
  "pdf_sha256": "<source PDF SHA-256>",
  "base_md_sha256": "<pre-recovery merged body SHA-256>",
  "replacements": [
    {
      "page": 2,
      "span": [120, 142],
      "original": "<exact unique source span>",
      "replacement": "<verified replacement Markdown>",
      "method": "transcription",
      "verification": {
        "source_page": 2,
        "visually_verified": true,
        "evidence": "Checked the original page, including every equation and table cell."
      }
    }
  ]
}
```

The example offsets are illustrative; calculate them from the actual base.
Methods are `docling`, `targeted_extraction`, or `transcription`. Use at most one
replacement per page. Replacements must be nonempty, nonoverlapping and free of
missing-page markers. A replacement may not consume another page's missing
marker. Stale hashes, ambiguous boundaries and missing verification cause an
error rather than partial application. The page map retains verification
provenance and replacement hashes; recovery does not automatically assert
complete coverage of the rest of the page.

Numbering diagnostics are advisory. Check appendix numbering and external
references before inferring absence. A reviewer must verify the original page
before claiming an equation, method or table is absent. A suppressed audit
finding is not evidence of an extraction error.

Recovery candidates and page maps are staged before publication. A rejected
ledger preserves the previous delivered Markdown, page map and audit/merge
reports. The source PDF must be readable so each recovery page can be checked
against its page count; unavailable page membership fails closed. A bare
`[MISSING_PAGE_POST]` has unknown page attribution and cannot be consumed by a
page-specific ledger replacement.
