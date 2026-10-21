# Workflow

## First run

1. Follow INSTALL.md and provision separate Nougat and Docling environments.
2. Run scripts/check_deps.py with explicit executable paths if the environments
   are not activated.
3. Choose --profile fast or --profile quality.
4. Run scripts/extract_pdf_to_md.py with --resume.
5. Review `<safe-stem>.md`, figures/, merge_report.json, manifest.json, timings.json,
   and ocr_audit.md/ocr_audit.json.
6. Resolve every active OCR finding against only the named PDF page or bounding
   box. Repair nougat.mmd (or docling.md for an inserted caption/footnote) only
   when the PDF disagrees. Otherwise record `source_matches` or `false_positive`
   with `scripts/audit_ocr.py`.

The default `--figure-placement appended` mode keeps Nougat's complete body
and adds each normalized Docling figure once under `## Figures`. Nougat is
also the body source when `--figure-placement inline` is selected; inline
placement only inserts figure blocks at monotonic Nougat paragraph anchors.
Docling prose, tables, and formulas are never transplanted into the body.
Uniquely aligned numbered code is the exception: Docling content is reflowed
using Nougat's leading printed-number sequence, joined across consecutive
pages, and emitted as one plain balanced fence. Ambiguous evidence stays Nougat.

## Repeat runs

--resume reuses a stage only when its PDF hash, profile, arguments, tool path,
and tool version match the manifest. If a stage is missing or stale, only that
stage is rerun. A missing `docling_code.json` reruns only Docling. --force
invalidates both external stages.

Changing figure placement, math normalization, metadata, or LaTeX options does
not require OCR reruns; rerun the merge command with --resume.

When a job already has valid external stages, use the same PDF, profile, and
output directory with `--merge-only` to rebuild Markdown, repair cached assets,
and rerun the OCR audit without invoking either OCR tool. It retains Nougat
code with a warning if `docling_code.json` is unavailable. `--resume` performs
this merge-time normalization and audit as part of a normal run. Both paths
rewrite local Docling image references to durable relative links and remove
stale `.staging/` paths.

For example:

    python3 scripts/extract_pdf_to_md.py /path/to/paper.pdf \
      --profile fast --output-dir outputs \
      --merge-only --figure-placement appended

## Performance guidance

- Use fast for routine conversions and quality for documents where extraction
  fidelity matters more than runtime.
- Referenced Docling images avoid storing large base64 strings in Markdown.
- Keep stages serial unless the machine has enough memory for both models.
- Use --parallel-stages only after measuring the host; GPU memory contention can
  make concurrent execution slower or fail.
- Use timings.json to compare model, merge, image-decoding, and
  `ocr_audit_seconds` costs.
- Use merge_report.json to compare placed and unplaced figures, fallback
  warnings, reconstructed footnotes, structural validation failures, and
  OCR-audit status. Use ocr_audit.json to inspect per-page coverage and active
  substitutions. A structurally clean report contains no unresolved local
  assets, absolute paths, `.staging/` references, or formula placeholders; a
  clear audit is limited to its reported coverage.

## Recovery

- If Docling cannot export referenced images, the runner retries with embedded
  images. You can force the fallback with --image-mode embedded.
- For missing-page markers, use the bounded [page recovery workflow](recovery.md).
  Do not rerun the entire document automatically or remove unresolved markers.
- If a run is interrupted, rerun with --resume. Partial stage directories are
  discarded before a retry and are never treated as valid cache entries.
- If inline matching is ambiguous, the affected figure is emitted once under
  `## Unplaced Figures` and the report explains the fallback; inspect that
  section before accepting the result.
- If the OCR audit reports a finding, inspect only its page/bounding box and
  resolve it after visual comparison. Batch confirmed source-artifact repairs,
  then rerun with `--merge-only` and the original merge/LaTeX options. The
  audit never edits content automatically. `partial` and `unavailable` pages
  are coverage limitations, not proof of fidelity.
- If the audit runtime cannot execute, the conversion remains nonfatal but
  ocr_audit.json is `review_required` with an execution error; restore the
  Nougat interpreter's pypdfium2 import before accepting the audit.
- For LaTeX, use --to-latex. Pandoc runs from the hashed job directory with
  the job, `figures/`, and `docling_assets/` as resource paths. Preflight
  rejects unresolved assets, staging paths, formula placeholders, and raw
  Unicode unsupported by the documented pdfLaTeX route. Balanced code fences
  become `verbatim`; intentional `∆` receives an explicit pdfLaTeX declaration.
  Compile from that directory with:

      latexmk -pdf -halt-on-error -file-line-error <safe-stem>.tex

Review `page_map.json` before accepting completeness. `validation.ok` only checks
structure; `--require-complete` returns 2 when content is incomplete or unknown.
See [the output contract](io-spec.md#completeness-contract-merge-6--audit-2).
