# pdf-to-md

Convert a research PDF into structured Markdown using **Nougat-OCR** for text, equations, and tables, plus **Docling** for figures, captions, and footnotes. The two tools run in isolated environments and their outputs are merged into a single, durable `combined.md` with copied/relinked figure assets.

- **Nougat** is the canonical source for prose, headings, equations, and tables.
- **Docling** supplies figures, captions, and footnote/layout evidence.
- Runs are cached and resumable per-PDF (content-hash keyed), so re-running with `--resume` won't repeat OCR unless inputs change.

## Contents

- [Use as an AI skill](#use-as-an-ai-skill)
- [Use from the terminal](#use-from-the-terminal)
- [Installation](#installation)
- [CLI options](#cli-options)
- [Output structure](#output-structure)
- [Profiles](#profiles)
- [LaTeX output](#latex-output)
- [Troubleshooting](#troubleshooting)
- [Repository layout](#repository-layout)

## Use as an AI skill

Install [`SKILL.md`](SKILL.md) as a skill in your preferred AI tool (Codex, Cursor, etc.) so an agent can drive this repo's PDF-to-Markdown workflow on your behalf. See [INSTALL.md](INSTALL.md) for Codex/Cursor-specific copy steps.

### Example prompts

Once installed as a skill, you can ask your AI assistant things like:

- "Convert `~/Downloads/paper.pdf` to Markdown using the fast profile."
- "Re-run the conversion for `paper.pdf` with inline figure placement instead of appended."
- "Check that Nougat and Docling are installed before converting `thesis.pdf`."
- "Regenerate just the merge output for the existing job, don't rerun OCR."
- "Convert `paper.pdf` to Markdown and also produce a compiled LaTeX/PDF version."
- "Review `merge_report.json` for the last conversion and tell me which figures were unplaced."

The agent will follow the workflow documented in `SKILL.md`: check for existing Nougat/Docling installs, provision the runtime only if needed, pick a profile, run extraction with `--resume`, and review `combined.md`, `figures/`, and `merge_report.json`.

## Use from the terminal

```bash
python scripts/extract_pdf_to_md.py input.pdf --output-dir outputs
```

The merged result is written to `outputs/<pdf-stem>-<content-hash>/combined.md`.

A more complete example, explicitly choosing a profile and tool paths:

```bash
python3 scripts/extract_pdf_to_md.py /path/to/paper.pdf \
  --profile fast \
  --output-dir outputs \
  --nougat-cmd /path/to/runtime/nougat/bin/nougat \
  --docling-cmd /path/to/runtime/docling/bin/docling \
  --resume
```

## Installation

Requires **Python 3.12**. Nougat and Docling are installed into two isolated environments to avoid dependency conflicts:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
python3 scripts/bootstrap_runtime.py --runtime-dir .runtime --python 3.12
```

Then verify everything is wired up correctly:

```bash
python3 scripts/check_deps.py \
  --nougat-cmd .runtime/nougat/bin/nougat \
  --docling-cmd .runtime/docling/bin/docling \
  --output-dir outputs
```

See [INSTALL.md](INSTALL.md) for full cross-platform instructions (macOS, Linux, Windows, WSL2), Codex/Cursor skill installation, uv vs. venv+pip fallback, updates, and removal.

## CLI options

Key options for `scripts/extract_pdf_to_md.py`:

| Option | Description |
| --- | --- |
| `--profile fast\|quality` | `fast` uses Nougat's small model with conservative batching; `quality` uses Nougat's base model for higher-fidelity extraction. |
| `--output-dir DIR` | Root directory for job output. |
| `--nougat-cmd PATH` / `--docling-cmd PATH` | Explicit executable paths (recommended when tools aren't on `PATH`). |
| `--resume` | Reuse valid cached stage outputs (content-hash keyed); re-normalizes cached Docling links. |
| `--force` | Recompute both external stages from scratch. |
| `--merge-only` | Rebuild the merge/assets from an existing hashed job without invoking Nougat or Docling. |
| `--figure-placement appended\|inline` | `appended` (default) places all figures under `## Figures`; `inline` anchors figures near their source text, with low-confidence figures placed under `## Unplaced Figures`. |
| `--image-mode embedded` | Use embedded Docling images instead of referenced image export. |
| `--keep-base64` | Retain data URIs in `docling.md` (final outputs still decode figures). |
| `--parallel-stages` | Run Nougat and Docling concurrently (only on hosts with enough resources). |
| `--normalize-math` | Normalize bracketed math delimiters. |
| `--to-latex` | Generate LaTeX output via Pandoc (requires `--pandoc-cmd`). |
| `--pandoc-cmd PATH` | Path to the Pandoc executable, required with `--to-latex`. |
| `--nougat-arg VALUE` / `--docling-arg VALUE` | Repeatable passthrough args to Nougat/Docling, e.g. `--nougat-arg=--no-skipping`, `--docling-arg=--device --docling-arg=cpu`. |

Run `python scripts/extract_pdf_to_md.py --help` for the full list.

## Output structure

Each PDF produces a directory named `<safe-stem>-<content-hash>`:

```
outputs/<pdf-stem>-<content-hash>/
  nougat.mmd            # canonical prose/equations/tables from Nougat
  docling.md             # normalized Docling Markdown (assets in docling_assets/)
  docling_assets/
  docling_decoded.md      # merge input with images copied/decoded into figures/
  figures/
  combined.md             # final merged Markdown artifact
  merge_report.json       # figure placement, footnotes, validation (schema 3)
  manifest.json           # PDF hash, profile, args, tool/runtime versions
  timings.json            # stage/merge/decode timings
  combined_for_tex.md      # optional, with --to-latex
  combined.tex             # optional, with --to-latex
```

All local image references in final Markdown are durable relative paths (e.g. `figures/figure-001.png`) — no absolute paths or `.staging/` references.

## Profiles

- **fast** — Nougat small model, conservative batch settings. Good for routine conversions.
- **quality** — Nougat base model, larger batches. Use when fidelity matters more than speed.

Both profiles are CPU-compatible; CUDA/Apple MPS acceleration is optional (see [INSTALL.md](INSTALL.md)).

## LaTeX output

With Pandoc installed, add `--to-latex --pandoc-cmd $(command -v pandoc)` to generate `combined.tex`. Compile from the job directory so relative figure paths resolve:

```bash
cd outputs/<pdf-stem>-<content-hash>
latexmk -pdf -halt-on-error -file-line-error combined.tex
```

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Nougat or Docling missing | Run `check_deps.py --json` and pass explicit executable paths. |
| Docling image-export failure | Retry with `--image-mode embedded`. |
| Nougat `[MISSING_PAGE]` output | Add `--nougat-arg=--no-skipping`. |
| macOS OpenMP crash | Set `KMP_SHM_DISABLE=1` and `OMP_NUM_THREADS=1`. |
| Missing/unresolved figure | Check `merge_report.json`, then rerun with `--resume` or `--merge-only`. |
| LaTeX Unicode/missing-graphics errors | See preflight checks described in [INSTALL.md](INSTALL.md#latex-output). |

See [INSTALL.md](INSTALL.md#troubleshooting) for the complete table.

## Repository layout

```
scripts/    extract_pdf_to_md.py, check_deps.py, bootstrap_runtime.py, benchmarks, etc.
requirements/  pinned deps for the nougat/ and docling/ isolated environments
references/  workflow.md, io-spec.md, tooling.md (deep-dive docs referenced by SKILL.md)
tests/      test_pipeline.py, test_output_contract.py
benchmarks/  benchmark scripts and fixtures
```

## Documentation

- [SKILL.md](SKILL.md) — AI-skill definition and quick workflow.
- [INSTALL.md](INSTALL.md) — full installation, portability, and troubleshooting guide.
- `references/workflow.md`, `references/io-spec.md`, `references/tooling.md` — deeper technical references.
