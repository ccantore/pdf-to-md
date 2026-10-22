# I/O contract

## Input

The pipeline accepts one local PDF path. The file is hashed before external
processing so identical content gets the same job directory even when the
source path changes.

## Output tree

    outputs/
      <safe-stem>-<sha256-prefix>/
        nougat.mmd
        docling.md
        docling_code.json
        docling_assets/
        docling_decoded.md
        figures/
          figure-001.png
        <safe-stem>.md
        recovery/base.md
        page_map.json
        merge_report.json
        ocr_audit.json
        ocr_audit.md
        ocr_audit_review.json
        manifest.json
        timings.json
        <safe-stem>_for_tex.md
        <safe-stem>.tex

The LaTeX files are optional and appear only when --to-latex is used.

## Semantics

- `nougat.mmd` is canonical for prose, headings, equations, and tables. Its
  code remains canonical unless the conservative hybrid match succeeds.
- `docling_code.json` is a compact schema-1 cache of ordered code text,
  surrounding anchors, and page provenance derived from Docling JSON.
- `docling.md` is normalized Docling Markdown retained for inspection and
  cache repair. Local references resolve below the sibling `docling_assets/`
  directory; it is not the final prose body.
- `docling_assets/` contains copied Docling assets needed by the normalized
  source. References outside the source/staging root are rejected.
- `docling_decoded.md` rewrites local and embedded image references to the
  canonical `figures/figure-NNN.ext` form.
- `figures/` contains decoded or copied image assets. Each figure is emitted
  once and has a durable relative link.
- `<safe-stem>.md` is the final merged Markdown. It must contain no absolute local
  paths, `.staging/` paths, unresolved local assets, or formula placeholders.
- `merge_report.json` uses merge schema 6 and records per-code-block status,
  page/line ranges, replacement counts and fallback reasons, placed/unplaced
  figure counts, footnote reconstruction, structural asset/path validation, an
  OCR-audit summary, and warnings. Treat structural validation failures as
  conversion failures; OCR findings are nonfatal review prompts.
- `ocr_audit.json` uses audit schema 2. It records `clear` or
  `review_required`, one coverage value (`full`, `partial`, or `unavailable`)
  per PDF page, stable finding IDs/fingerprints, optional PDF character boxes,
  content origin, finding type, confidence, extracted/reference snippets, and
  contextual evidence. `ocr_audit.md` is the human-readable report.
- `ocr_audit_review.json` is a persistent schema-1 resolution ledger. A
  `source_matches` or `false_positive` classification suppresses only the
  same finding fingerprint; a changed PDF or relevant extracted content makes
  the finding active again. Use `scripts/audit_ocr.py resolve` to record a
  classification.
- `manifest.json` records the PDF basename and hash, profile, external-stage
  arguments, tool executable names and versions, runtime, and completed stages.
  Absolute input and tool paths stay in memory for the running process and are
  not written. External OCR cache keys are independent of merge/normalization
  settings.
- `timings.json` records external-stage, merge, image-decoding, and
  `ocr_audit_seconds` timings. Audit time does not participate in external
  cache keys.

The default `--figure-placement appended` mode preserves Nougat's body and
adds normalized figure blocks once under `## Figures`. With explicit
`--figure-placement inline`, the merger masks structured Docling content and
uses one monotonic, context-based alignment to choose Nougat paragraph
boundaries. A figure with an ambiguous or low-confidence anchor is emitted
once under `## Unplaced Figures` and a warning is recorded. Docling contributes
figure/caption and footnote evidence; prose, formulas and tables are transplanted
only through an explicitly verified recovery ledger. It may replace a code fence only when both prose anchors and
Nougat's leading printed-number sequence produce a unique match. Consecutive
Docling page blocks are joined into one balanced fence; `ˆ` and `−` are
normalized, unambiguous underscores are repaired from Nougat identifiers, and
intentional `∆` is preserved. Footnote markers and definitions are reconstructed
in both modes, including continuations split by page or figure blocks.

## Cache rules

`--resume` reuses an external stage only when its manifest cache key matches
and every declared output is present, non-empty, and valid. A missing
`docling_code.json` invalidates only Docling. The runner then re-normalizes
cached Docling Markdown/assets and rebuilds the merge artifacts.
`--force` recomputes the external stages. Missing or malformed manifests never
authorize reuse.

`--merge-only` targets the existing content-hashed job for the supplied PDF and
performs normalization, figure decoding, footnote reconstruction, merging,
structural validation, and the OCR audit without invoking Nougat or Docling. It
is the supported repair path after a completed OCR run. If `docling_code.json`
is unavailable, Nougat code is retained with a report warning. A missing or invalid source asset is
reported and may require a Docling rerun; a missing durable asset invalidates
only the affected Docling cache where possible.

External-stage cache keys include the PDF content hash, profile, tool path or
version, and stage arguments. Merge choices such as figure placement, math
normalization, metadata, and LaTeX generation are deliberately excluded so
they can be changed without repeating OCR.

## Completeness contract (merge 6 / audit 2)

`merge_report.json` separates `execution.ok`, structural `validation.ok`,
`completeness.status`, `fidelity` (audit status, coverage and scope), and overall
`status` (`clear` or `review_required`). Overall clear requires complete content,
a clear audit and no numbering prompts. Audit skip/failure never establishes
fidelity. Missing-page markers always mean incomplete, even if their page
number is invalid or page extraction is unavailable.

`page_map.json` schema 1 records the PDF hash, final Markdown hash, completeness,
numbering diagnostics and one record per PDF page. Each record contains:

- `page`: one-based PDF page number (not the printed page label).
- `status`: complete, incomplete or unknown.
- `final_spans`: matching half-open character offsets in the final UTF-8-decoded
  Markdown string, not byte offsets or assumed page boundaries.
- `mapping_confidence`: unique_anchor, ambiguous_overlap, explicit_missing_marker or unavailable.
- `matched_text_fraction`, `missing_markers`, `fidelity_coverage`, and `recovery`.

Complete requires every normalized PDF text token to match a distinct final
occurrence. Overlapping page windows cannot share token ownership; unresolved
claims remain unknown. Unmatched blocks of at least five tokens are also sought
in unclaimed final spans, so appended captions and relocated footnotes can count
toward completeness. Competing relocation claims are resolved jointly; ambiguous
or shorter blocks remain unknown. A bounded deletion means incomplete only when
no exact occurrence exists elsewhere and the remaining unclaimed tokens cannot
account for it. An explicit missing marker always means incomplete. Text-layer `full`
coverage is not visual verification; the report states this limitation.
Extraction failure produces an unknown page map with an error; explicit markers
still make it incomplete. A warning is inserted after any YAML front matter
when content is incomplete or unknown. Its offsets and hash describe the
actual delivered artifact.

Audit schema 2 compares the final body and records its hash, completeness,
alignment coverage, and text-layer fidelity coverage separately. Review ledger
schema 1 is retained; fingerprints bind the PDF, origin, reference, extracted
content and surrounding context, so affected changes reactivate findings.
Legacy reports are recomputed from cached OCR, without changing OCR cache keys.
A legacy resolution cannot establish completeness.

`--require-complete` returns 2 after output/report generation for incomplete or
unknown content; ordinary successful execution remains exit 0. Structural and
execution failures keep their existing error behavior. This flag checks content
completeness only, not whether every audit prompt has been resolved.

See [recovery](recovery.md) for the optional verified replacement ledger.

Missing markers include bare and numbered Nougat `EMPTY`, `FAIL` and `POST`
forms. Bare markers have a null page in `page_map.json`; the merge report
counts them as `unattributed_missing_markers` separately from `missing_pages`.
They keep the document incomplete and prevent full coverage on pages whose
attribution is uncertain. `uncompared_characters` lists meaningful normalized
source characters outside the comparison tokenizer (such as integral symbols
or non-Latin text). Their presence prevents complete/full claims even when all
recognized tokens match; original-page inspection remains necessary.
