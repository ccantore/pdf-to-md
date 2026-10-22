from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_deps  # noqa: E402
import benchmark_pipeline  # noqa: E402
import extract_pdf_to_md as pipeline  # noqa: E402
from parse_docling_figures import decode_docling_markdown, normalize_docling_markdown  # noqa: E402
from prepare_md_for_latex import (  # noqa: E402
    fix_fences,
    pdftex_header_includes,
    validate_latex_input,
)


PNG_BYTES = b"fake-png-bytes"
VALID_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
RBC_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "rbc_like"


def write_fake_cli(path: Path, tool: str) -> Path:
    encoded_png = base64.b64encode(PNG_BYTES).decode("ascii")
    source = f"""#!/usr/bin/env python3
from pathlib import Path
import base64
import json
import os
import sys

if "--version" in sys.argv:
    print("fake-{tool} 1.0")
    raise SystemExit(0)
if "--help" in sys.argv:
    print("fake {tool} help")
    raise SystemExit(0)

counter_dir = Path(os.environ["FAKE_COUNTER_DIR"])
counter_dir.mkdir(parents=True, exist_ok=True)
counter = counter_dir / "{tool}"
counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))
if os.environ.get("FAKE_FAIL_{tool.upper()}"):
    print("fake {tool} failure", file=sys.stderr)
    raise SystemExit(3)
pdf = next(Path(argument) for argument in sys.argv[1:] if argument.lower().endswith(".pdf"))

if "{tool}" == "nougat":
    output_dir = Path(sys.argv[sys.argv.index("--out") + 1])
    output_dir.mkdir(parents=True, exist_ok=True)
    nougat_text = os.environ.get("FAKE_NOUGAT_MARKDOWN", "# Test paper\\n\\nText with \\\\(x+y\\\\).\\n")
    (output_dir / f"{{pdf.stem}}.mmd").write_text(nougat_text, encoding="utf-8")
else:
    output_dir = Path(sys.argv[sys.argv.index("--output") + 1])
    output_dir.mkdir(parents=True, exist_ok=True)
    mode = sys.argv[sys.argv.index("--image-export-mode") + 1]
    if mode == "referenced" and os.environ.get("FAKE_FAIL_REFERENCED"):
        print("referenced mode unsupported", file=sys.stderr)
        raise SystemExit(1)
    if mode == "referenced":
        png_bytes = base64.b64decode("{VALID_PNG_B64}") if os.environ.get("FAKE_VALID_PNG") else {PNG_BYTES!r}
        (output_dir / "image-1.png").write_bytes(png_bytes)
        if os.environ.get("FAKE_ABSOLUTE_STAGING"):
            image = f"![sample](<{{output_dir / 'image-1.png'}}>)"
        else:
            image = "![sample](image-1.png)"
    else:
        image = "![sample](data:image/png;base64,{encoded_png})"
    (output_dir / f"{{pdf.stem}}.md").write_text("Figure 1: Sample\\n\\n" + image + "\\n")
    raw_json = {{"texts": [], "body": {{"children": []}}}}
    json_source = os.environ.get("FAKE_DOCLING_JSON")
    if json_source:
        raw_json = json.loads(Path(json_source).read_text(encoding="utf-8"))
    (output_dir / f"{{pdf.stem}}.json").write_text(json.dumps(raw_json), encoding="utf-8")
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def write_text_pdf(path, pages):
    """Small real PDF fixture: exercise PDFium, not the unavailable-PDF fallback."""
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    kids = []
    for text in pages:
        page_id = len(objects) + 1
        kids.append(f'{page_id} 0 R')
        objects.append((f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
            f'/Resources << /Font << /F1 3 0 R >> >> /Contents {page_id+1} 0 R >>').encode())
        commands = ['BT /F1 10 Tf 12 TL 40 740 Td']
        for line in textwrap.wrap(text, 85):
            escaped = line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
            commands.append(f'({escaped}) Tj T*')
        commands.append('ET')
        stream = '\n'.join(commands).encode('ascii')
        objects.append(f'<< /Length {len(stream)} >>\nstream\n'.encode() + stream + b'\nendstream')
    objects[1] = f'<< /Type /Pages /Count {len(pages)} /Kids [{" ".join(kids)}] >>'.encode()
    result = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f'{index} 0 obj\n'.encode() + obj + b'\nendobj\n')
    xref = len(result)
    result.extend(f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        result.extend(f'{offset:010d} 00000 n \n'.encode())
    result.extend(f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    path.write_bytes(result)


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.counter_dir = self.root / "counters"
        self.nougat = write_fake_cli(self.root / "fake_nougat.py", "nougat")
        self.docling = write_fake_cli(self.root / "fake_docling.py", "docling")
        self.env = {"FAKE_COUNTER_DIR": str(self.counter_dir)}

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_pipeline(
        self,
        pdf: Path,
        *extra: str,
        profile: str = "fast",
        output_dir: Path | None = None,
    ) -> int:
        output_dir = output_dir or (self.root / "outputs")
        argv = [
            "extract_pdf_to_md.py",
            str(pdf),
            "--profile",
            profile,
            "--output-dir",
            str(output_dir),
            "--nougat-cmd",
            str(self.nougat),
            "--docling-cmd",
            str(self.docling),
            *extra,
        ]
        with patch.dict(os.environ, self.env, clear=False), patch.object(sys, "argv", argv):
            return pipeline.main()

    @staticmethod
    def _tree_fingerprint(root: Path) -> tuple[tuple[str, str], ...]:
        entries: list[tuple[str, str]] = []
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append((path.relative_to(root).as_posix(), digest))
        return tuple(entries)

    @staticmethod
    def _artifact_fingerprint(job_dir: Path, pdf: Path) -> dict[str, str]:
        names = (
            "docling.md",
            "docling_decoded.md",
            f"{pipeline.safe_stem(pdf)}.md",
            "merge_report.json",
        )
        return {
            name: hashlib.sha256((job_dir / name).read_bytes()).hexdigest()
            for name in names
        }

    def test_one_external_run_then_resume_and_force(self) -> None:
        pdf = self.root / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.7\nfirst")

        self.assertEqual(self.run_pipeline(pdf), 0)
        self.assertEqual((self.counter_dir / "nougat").read_text(), "1")
        self.assertEqual((self.counter_dir / "docling").read_text(), "1")

        job_dirs = list((self.root / "outputs").iterdir())
        self.assertEqual(len(job_dirs), 1)
        job_dir = job_dirs[0]
        self.assertTrue((job_dir / "nougat.mmd").is_file())
        self.assertTrue((job_dir / "docling.md").is_file())
        self.assertIn("docling_assets/image-1.png", (job_dir / "docling.md").read_text())
        self.assertTrue((job_dir / "docling_assets" / "image-1.png").is_file())
        self.assertFalse((job_dir / ".staging").exists())
        self.assertTrue((job_dir / "figures" / "figure-001.png").is_file())
        self.assertTrue((job_dir / "docling_code.json").is_file())
        self.assertIn("## Figures", (job_dir / "paper.md").read_text())
        self.assertFalse((job_dir / "combined.md").exists())

        self.assertEqual(self.run_pipeline(pdf, "--resume"), 0)
        self.assertEqual((self.counter_dir / "nougat").read_text(), "1")
        self.assertEqual((self.counter_dir / "docling").read_text(), "1")

        self.assertEqual(self.run_pipeline(pdf, "--resume", "--image-mode", "embedded"), 0)
        self.assertEqual((self.counter_dir / "nougat").read_text(), "1")
        self.assertEqual((self.counter_dir / "docling").read_text(), "2")

        self.assertEqual(self.run_pipeline(pdf, "--force"), 0)
        self.assertEqual((self.counter_dir / "nougat").read_text(), "2")
        self.assertEqual((self.counter_dir / "docling").read_text(), "3")

        manifest = json.loads((job_dir / "manifest.json").read_text())
        self.assertEqual(manifest["schema"], "2")
        self.assertEqual(manifest["stages"]["nougat"]["status"], "complete")
        self.assertEqual(manifest["stages"]["docling"]["image_mode"], "referenced")
        timings = json.loads((job_dir / "timings.json").read_text())
        self.assertIn("nougat", timings["stages"])
        self.assertIn("docling", timings["stages"])

    def test_absolute_referenced_asset_under_spaced_staging_path_is_rewritten(self) -> None:
        pdf = self.root / "paper (absolute-assets).pdf"
        pdf.write_bytes(b"%PDF-1.7\nabsolute-assets")
        spaced_output = self.root / "outputs with spaces (RBC)"
        self.env["FAKE_ABSOLUTE_STAGING"] = "1"

        self.assertEqual(self.run_pipeline(pdf, output_dir=spaced_output), 0)
        job_dir = next(spaced_output.iterdir())
        docling = (job_dir / "docling.md").read_text(encoding="utf-8")
        self.assertIn("docling_assets/image-1.png", docling)
        self.assertNotIn(".staging", docling)
        self.assertNotIn(str(self.root), docling)
        self.assertTrue((job_dir / "docling_assets" / "image-1.png").is_file())
        self.assertFalse((job_dir / ".staging").exists())

        self.assertEqual(self.run_pipeline(pdf, "--resume", output_dir=spaced_output), 0)
        self.assertEqual((self.counter_dir / "nougat").read_text(), "1")
        self.assertEqual((self.counter_dir / "docling").read_text(), "1")
        self.assertEqual(self.run_pipeline(pdf, "--merge-only", output_dir=spaced_output), 0)
        self.assertEqual((self.counter_dir / "nougat").read_text(), "1")
        self.assertEqual((self.counter_dir / "docling").read_text(), "1")
        self.assertNotIn(
            ".staging",
            (job_dir / "paper_absolute-assets.md").read_text(encoding="utf-8"),
        )

    def test_resume_and_merge_only_are_idempotent_and_collapse_legacy_prefixes(self) -> None:
        pdf = self.root / "idempotent (absolute).pdf"
        pdf.write_bytes(b"%PDF-1.7\nidempotent")
        output_dir = self.root / "outputs with spaces (idempotent)"
        self.env["FAKE_ABSOLUTE_STAGING"] = "1"

        self.assertEqual(self.run_pipeline(pdf, output_dir=output_dir), 0)
        job_dir = next(output_dir.iterdir())
        self.assertEqual((self.counter_dir / "nougat").read_text(), "1")
        self.assertEqual((self.counter_dir / "docling").read_text(), "1")
        baseline_artifacts = self._artifact_fingerprint(job_dir, pdf)
        baseline_assets = self._tree_fingerprint(job_dir / "docling_assets")
        baseline_figures = self._tree_fingerprint(job_dir / "figures")

        for _ in range(3):
            self.assertEqual(self.run_pipeline(pdf, "--resume", output_dir=output_dir), 0)
            self.assertEqual(self._artifact_fingerprint(job_dir, pdf), baseline_artifacts)
            self.assertEqual(self._tree_fingerprint(job_dir / "docling_assets"), baseline_assets)
            self.assertEqual(self._tree_fingerprint(job_dir / "figures"), baseline_figures)
        for _ in range(3):
            self.assertEqual(self.run_pipeline(pdf, "--merge-only", output_dir=output_dir), 0)
            self.assertEqual(self._artifact_fingerprint(job_dir, pdf), baseline_artifacts)
            self.assertEqual(self._tree_fingerprint(job_dir / "docling_assets"), baseline_assets)
            self.assertEqual(self._tree_fingerprint(job_dir / "figures"), baseline_figures)

        docling = (job_dir / "docling.md").read_text(encoding="utf-8")
        for target in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", docling):
            if "docling_assets" in target:
                self.assertEqual(target.count("docling_assets/"), 1)
        self.assertFalse((job_dir / "docling_assets" / "docling_assets").exists())

        # Simulate a legacy cache whose canonical Markdown accumulated the
        # prefix during repeated normalization.  The durable basename remains
        # available, so repair must collapse the path without rerunning OCR.
        legacy_target = "docling_assets/docling_assets/docling_assets/image-1.png"
        (job_dir / "docling.md").write_text(
            f"Figure 1: Sample\n\n![sample]({legacy_target})\n", encoding="utf-8"
        )
        self.assertEqual(self.run_pipeline(pdf, "--resume", output_dir=output_dir), 0)
        self.assertEqual((self.counter_dir / "nougat").read_text(), "1")
        self.assertEqual((self.counter_dir / "docling").read_text(), "1")
        repaired = (job_dir / "docling.md").read_text(encoding="utf-8")
        self.assertIn("docling_assets/image-1.png", repaired)
        self.assertNotIn("docling_assets/docling_assets", repaired)
        self.assertFalse((job_dir / "docling_assets" / "docling_assets").exists())

        repaired_baseline = self._artifact_fingerprint(job_dir, pdf)
        repaired_assets = self._tree_fingerprint(job_dir / "docling_assets")
        repaired_figures = self._tree_fingerprint(job_dir / "figures")
        for _ in range(3):
            self.assertEqual(self.run_pipeline(pdf, "--merge-only", output_dir=output_dir), 0)
            self.assertEqual(self._artifact_fingerprint(job_dir, pdf), repaired_baseline)
            self.assertEqual(self._tree_fingerprint(job_dir / "docling_assets"), repaired_assets)
            self.assertEqual(self._tree_fingerprint(job_dir / "figures"), repaired_figures)
        self.assertEqual((self.counter_dir / "nougat").read_text(), "1")
        self.assertEqual((self.counter_dir / "docling").read_text(), "1")

    def test_rbc_inline_merge_is_nougat_canonical_and_reports_figure_counts(self) -> None:
        nougat = (RBC_FIXTURE_ROOT / "nougat.mmd").read_text(encoding="utf-8")
        figures = (RBC_FIXTURE_ROOT / "docling.md").read_text(encoding="utf-8").replace(
            "{{FIGURE_1}}", "![Production function](figures/figure-001.png)"
        )
        merged, report = pipeline.merge_outputs_with_report(nougat, figures, "inline", False)
        self.assertIn("# RBC Quantitative Benchmark", merged)
        self.assertEqual(merged.count("The representative household chooses consumption"), 1)
        self.assertEqual(merged.count("c_t^{-\\sigma}"), 1)
        self.assertEqual(merged.count("| Parameter | Value |"), 1)
        self.assertEqual(merged.count("Production function"), 1)
        self.assertEqual(report["figures"], {"total": 1, "placed": 1, "unplaced": 0})

        monotonic_nougat = "Intro alpha comes first. Middle beta follows. Conclusion gamma comes last.\n"
        monotonic_figures = (
            "Intro alpha comes first.\n\nFigure 1: First\n\n![First](figures/figure-001.png)\n\n"
            "Middle beta follows.\n\nFigure 2: Second\n\n![Second](figures/figure-002.png)\n\n"
            "Conclusion gamma comes last.\n"
        )
        monotonic, monotonic_report = pipeline.merge_outputs_with_report(
            monotonic_nougat, monotonic_figures, "inline", False
        )
        self.assertLess(monotonic.find("figure-001.png"), monotonic.find("figure-002.png"))
        self.assertEqual(monotonic_report["figures"]["placed"], 2)

        ambiguous_nougat = "The repeated anchor appears before the repeated anchor and after the repeated anchor.\n"
        ambiguous_figures = (
            "The repeated anchor appears before the repeated anchor and after the repeated anchor.\n\n"
            "Figure 1: Ambiguous\n\n![Ambiguous](figures/figure-001.png)\n"
        )
        ambiguous, ambiguous_report = pipeline.merge_outputs_with_report(
            ambiguous_nougat, ambiguous_figures, "inline", False
        )
        self.assertIn("Unplaced Figures", ambiguous)
        self.assertEqual(ambiguous_report["figures"]["unplaced"], 1)
        self.assertTrue(ambiguous_report["warnings"])

    def test_rbc_hybrid_code_reflows_three_pages_into_one_numbered_fence(self) -> None:
        nougat = (RBC_FIXTURE_ROOT / "nougat_code.mmd").read_text(encoding="utf-8")
        sidecar = json.loads((RBC_FIXTURE_ROOT / "docling_code.json").read_text(encoding="utf-8"))

        merged, report = pipeline.merge_outputs_with_report(
            nougat,
            "",
            "appended",
            False,
            sidecar["groups"],
        )

        self.assertEqual(merged.count("```"), 2)
        code = merged.split("```", 2)[1].strip().splitlines()
        self.assertEqual(len(code), 89)
        self.assertEqual([int(line.split(maxsplit=1)[0]) for line in code], list(range(1, 90)))
        self.assertEqual(code[1], "2")
        self.assertIn("load baseline_rbc_params;", code[6])
        self.assertIn("set_param_value", code[7])
        self.assertIn("K(-1)^(alpha)*N^(-alpha)", code[29])
        self.assertIn("logY -logN", code[59])
        self.assertIn("stoch_simul", code[88])
        self.assertIn("hp_filter=1600", code[88])
        self.assertIn("∆", merged)
        self.assertNotIn("ˆ", merged)
        self.assertNotIn("−", merged)
        self.assertGreater(merged.index("This prose must remain outside"), merged.rindex("```"))

        code_report = report["code_blocks"]
        self.assertEqual(report["schema"], "6")
        self.assertEqual(code_report["replaced"], 1)
        self.assertEqual(code_report["fallback"], 0)
        self.assertEqual(code_report["blocks"][0]["pages"], [10, 11, 12])
        self.assertEqual(code_report["blocks"][0]["line_range"], [1, 89])
        self.assertEqual(code_report["blocks"][0]["replacement_count"], 1)
        self.assertIsNone(code_report["blocks"][0]["fallback_reason"])

    def test_hybrid_code_falls_back_for_missing_or_ambiguous_evidence(self) -> None:
        nougat = (RBC_FIXTURE_ROOT / "nougat_code.mmd").read_text(encoding="utf-8")
        sidecar = json.loads((RBC_FIXTURE_ROOT / "docling_code.json").read_text(encoding="utf-8"))

        retained, missing_report, warnings = pipeline.hybridize_code_blocks(nougat, None)
        self.assertEqual(retained, nougat)
        self.assertEqual(missing_report["fallback"], 1)
        self.assertIn("unavailable", warnings[0])

        ambiguous = nougat.split("```", 1)[0] + nougat
        retained, report, _ = pipeline.hybridize_code_blocks(ambiguous, sidecar["groups"])
        self.assertEqual(retained, ambiguous)
        self.assertEqual(report["fallback"], 1)
        self.assertIn("anchor", report["blocks"][0]["fallback_reason"])

        flattened = json.loads(json.dumps(sidecar["groups"]))
        flattened[0]["blocks"] = [
            {"text": "var Y I K N A C", "provenance": [{"page": 10}]}
        ]
        retained, report, _ = pipeline.hybridize_code_blocks(nougat, flattened)
        self.assertEqual(retained, nougat)
        self.assertEqual(report["fallback"], 1)
        self.assertIn("flattened", report["blocks"][0]["fallback_reason"])

        skipped_page = json.loads(json.dumps(sidecar["groups"]))
        skipped_page[0]["blocks"][1]["provenance"] = [{"page": 13}]
        retained, report, _ = pipeline.hybridize_code_blocks(nougat, skipped_page)
        self.assertEqual(retained, nougat)
        self.assertEqual(report["fallback"], 1)
        self.assertIn("consecutive pages", report["blocks"][0]["fallback_reason"])

    @unittest.skipUnless(shutil.which("pandoc"), "pandoc is not installed")
    def test_pandoc_caption_has_no_source_numbering_or_duplicate_text(self) -> None:
        figures = "Figure 1: Production function\n\n![OCR alt](figures/figure-001.png)\n"
        merged, report = pipeline.merge_outputs_with_report(
            "The production model follows.\n", figures, "appended", False
        )
        self.assertEqual(report["figures"]["total"], 1)
        self.assertIn("![Production function](figures/figure-001.png)", merged)
        self.assertNotIn("![Figure 1: Production function]", merged)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "figures").mkdir()
            (root / "figures" / "figure-001.png").write_bytes(PNG_BYTES)
            (root / "input.md").write_text(merged, encoding="utf-8")
            tex = root / "output.tex"
            subprocess.run(
                [
                    shutil.which("pandoc") or "pandoc",
                    "input.md",
                    "-s",
                    "--from",
                    "markdown",
                    "--to",
                    "latex",
                    f"--resource-path=.{os.pathsep}figures",
                    "-o",
                    tex.name,
                ],
                check=True,
                cwd=root,
            )
            latex = tex.read_text(encoding="utf-8")
            self.assertEqual(latex.count(r"\caption{Production function}"), 1)
            self.assertNotIn(r"\caption{Figure 1: Production function}", latex)
            self.assertNotIn("Figure 1: Production function", latex)

    @unittest.skipUnless(
        shutil.which("pandoc") and shutil.which("latexmk"),
        "pandoc and latexmk are not installed",
    )
    def test_real_latexmk_compiles_figure_without_missing_graphics(self) -> None:
        pdf = self.root / "real-latex.pdf"
        pdf.write_bytes(b"%PDF-1.7\nreal-latex")
        self.env["FAKE_VALID_PNG"] = "1"
        self.env["FAKE_NOUGAT_MARKDOWN"] = (
            "# Test paper\n\nJosé presents a naïve estimate.\n\n"
            "```\n"
            "1 var ∆;\n"
            "2 % This long code comment remains inside the balanced listing despite reading like prose.\n"
            "```\n"
        )
        pandoc = shutil.which("pandoc") or "pandoc"
        latexmk = shutil.which("latexmk") or "latexmk"

        self.assertEqual(
            self.run_pipeline(pdf, "--to-latex", "--pandoc-cmd", pandoc),
            0,
        )
        job_dir = next((self.root / "outputs").iterdir())
        generated_tex = (job_dir / "real-latex.tex").read_text(encoding="utf-8")
        self.assertEqual(generated_tex.count(r"\begin{verbatim}"), 1)
        self.assertIn("DeclareUnicodeCharacter{2206}", generated_tex)
        self.assertIn("This long code comment remains inside", generated_tex)
        result = subprocess.run(
            [latexmk, "-pdf", "-halt-on-error", "-file-line-error", "real-latex.tex"],
            cwd=job_dir,
            check=False,
            capture_output=True,
            text=True,
        )
        log = (job_dir / "real-latex.log").read_text(encoding="utf-8", errors="replace")
        diagnostics = f"\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}\nLOG:\n{log}"
        self.assertEqual(result.returncode, 0, diagnostics)
        self.assertNotIn("! LaTeX Error", log)
        self.assertNotIn("not found", log.lower())
        self.assertNotIn("missing graphics", log.lower())
        compiled_pdf = job_dir / "real-latex.pdf"
        self.assertTrue(compiled_pdf.is_file())
        try:
            import fitz  # type: ignore[import-not-found]
        except ImportError:
            self.assertIn("figure-001.png", log)
        else:
            document = fitz.open(compiled_pdf)
            image_count = sum(len(page.get_images(full=True)) for page in document)
            document.close()
            self.assertGreaterEqual(image_count, 1)

    def test_outside_staging_image_is_rejected(self) -> None:
        stage = self.root / ".staging" / "docling"
        stage.mkdir(parents=True)
        outside = self.root / "outside.png"
        outside.write_bytes(PNG_BYTES)
        source = stage / "paper.md"
        source.write_text(f"![outside](<{outside}>)\n", encoding="utf-8")

        with self.assertRaises(ValueError):
            normalize_docling_markdown(source, self.root / "normalized.md", self.root / "assets", stage)

    def test_traversal_encoded_traversal_and_symlink_escape_are_rejected(self) -> None:
        stage = self.root / ".staging" / "docling"
        stage.mkdir(parents=True)
        assets = self.root / "assets"
        outside = self.root / "outside.png"
        outside.write_bytes(PNG_BYTES)
        source = stage / "paper.md"
        output = self.root / "normalized.md"

        for target in ("../outside.png", "..%2Foutside.png"):
            with self.subTest(target=target):
                source.write_text(f"![escape]({target})\n", encoding="utf-8")
                with self.assertRaises(ValueError):
                    normalize_docling_markdown(source, output, assets, stage)

        link = stage / "linked.png"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this platform")
        source.write_text("![symlink](linked.png)\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            normalize_docling_markdown(source, output, assets, stage)

    def test_final_artifact_validator_rejects_existing_relative_escape(self) -> None:
        job = self.root / "job"
        job.mkdir()
        outside = self.root / "validator-outside.png"
        outside.write_bytes(PNG_BYTES)
        errors = pipeline.markdown_reference_errors(
            f"![outside](../{outside.name})\n", job
        )
        self.assertTrue(errors)

    def test_latex_preflight_rejects_staging_and_unsupported_unicode(self) -> None:
        self.assertTrue(validate_latex_input("![staged](.staging/docling/image.png)\n"))
        self.assertTrue(validate_latex_input("Unsupported raw Unicode: ✓\n"))
        self.assertTrue(validate_latex_input("Raw Greek alpha: α\n"))
        self.assertTrue(validate_latex_input("Ideographic comma: 、\n"))
        self.assertTrue(validate_latex_input("Arabic comma: ،\n"))
        self.assertFalse(validate_latex_input("José presents a naïve estimate.\n"))
        self.assertFalse(validate_latex_input("José, naïve; estimate: robust!\n"))
        self.assertFalse(validate_latex_input("Nougat math $x+y$ and figures/figure-001.png.\n"))
        self.assertFalse(validate_latex_input("A code symbol ∆ is declared explicitly.\n"))
        self.assertEqual(len(pdftex_header_includes("```\n1 var ∆;\n```\n")), 1)

    def test_footnote_orphan_continuation_survives_intervening_body_and_figure(self) -> None:
        nougat = (
            "The baseline estimate is robust.\n\n"
            "The intervening canonical paragraph stays in the Nougat narrative.\n\n"
            "Another canonical paragraph follows the figure.\n"
        )
        figures = (
            "The baseline estimate is robust 1\n\n"
            "1. This note starts before the page break and ends incomplete,\n\n"
            "The intervening canonical paragraph stays in the Docling layout.\n\n"
            "Figure 1: A policy function\n\n"
            "![A policy function](figures/figure-001.png)\n\n"
            "Another canonical paragraph follows the figure.\n\n"
            "lowercase continuation completes the note with evidence at https://example.com/rbc.\n"
        )

        for placement in ("appended", "inline"):
            with self.subTest(placement=placement):
                merged = pipeline.merge_outputs(nougat, figures, placement, False)
                self.assertEqual(merged.count("[^1]"), 2)
                definition = next(line for line in merged.splitlines() if line.startswith("[^1]:"))
                self.assertIn("This note starts before the page break", definition)
                self.assertIn("lowercase continuation completes the note", definition)
                self.assertIn("https://example.com/rbc", definition)
                self.assertNotIn("intervening canonical paragraph stays in the Docling layout", definition)
                self.assertNotIn("A policy function", definition)
                self.assertIn("The intervening canonical paragraph stays in the Nougat narrative.", merged)
                self.assertIn("Another canonical paragraph follows the figure.", merged)
                body = merged.split("[^1]:", 1)[0]
                self.assertNotIn("lowercase continuation completes the note", body)

    def test_ordinary_numbered_body_item_is_not_reconstructed_as_footnote(self) -> None:
        nougat = (
            "The analysis proceeds in case 1.\n\n"
            "1. This first numbered scenario includes several descriptive words and ends normally.\n"
        )
        docling = (
            "The analysis proceeds in case 1.\n\n"
            "1. This first numbered scenario includes several descriptive words and ends normally.\n"
        )

        for placement in ("appended", "inline"):
            with self.subTest(placement=placement):
                merged, report = pipeline.merge_outputs_with_report(
                    nougat, docling, placement, False
                )
                self.assertNotIn("[^1]", merged)
                self.assertNotIn("Footnotes", merged)
                self.assertEqual(
                    merged.count(
                        "1. This first numbered scenario includes several descriptive words and ends normally."
                    ),
                    1,
                )
                self.assertEqual(report["footnotes"], {"placed": 0, "unplaced": 0})

    def test_duplicate_two_sided_inline_context_falls_back_to_unplaced(self) -> None:
        nougat = (
            "Before the first section.\n\n"
            "alpha beta gamma.\n\n"
            "delta epsilon zeta.\n\n"
            "Between the duplicated sections.\n\n"
            "alpha beta gamma.\n\n"
            "delta epsilon zeta.\n\n"
            "After the second section.\n"
        )
        docling = (
            "alpha beta gamma.\n\n"
            "Figure 1: Ambiguous figure\n\n"
            "![Ambiguous figure](figures/figure-001.png)\n\n"
            "delta epsilon zeta.\n"
        )

        merged, report = pipeline.merge_outputs_with_report(nougat, docling, "inline", False)
        self.assertEqual(report["figures"], {"total": 1, "placed": 0, "unplaced": 1})
        self.assertTrue(report["warnings"])
        self.assertIn("## Unplaced Figures", merged)
        self.assertEqual(merged.count("figures/figure-001.png"), 1)

    def test_keep_base64_is_limited_to_docling_inspection_artifact(self) -> None:
        pdf = self.root / "keep-base64.pdf"
        pdf.write_bytes(b"%PDF-1.7\nkeep-base64")

        self.assertEqual(self.run_pipeline(pdf, "--keep-base64"), 0)
        job_dir = next((self.root / "outputs").iterdir())
        docling = (job_dir / "docling.md").read_text(encoding="utf-8")
        decoded = (job_dir / "docling_decoded.md").read_text(encoding="utf-8")
        combined = (job_dir / "keep-base64.md").read_text(encoding="utf-8")
        self.assertIn("data:image/png;base64,", docling)
        self.assertIn("figures/figure-001.png", decoded)
        self.assertIn("figures/figure-001.png", combined)
        self.assertNotIn("data:image/png;base64,", decoded)
        self.assertNotIn("data:image/png;base64,", combined)
        self.assertTrue((job_dir / "figures" / "figure-001.png").is_file())

    def test_existing_nougat_footnote_is_not_duplicated_by_docling_evidence(self) -> None:
        nougat = (
            "The result is robust[^1].\n\n"
            "[^1]: Existing Nougat definition is complete and should remain exactly once.\n"
        )
        docling = (
            "The result is robust 1\n\n"
            "1. Docling evidence for this citation contains enough words and ends normally.\n"
        )

        for placement in ("appended", "inline"):
            with self.subTest(placement=placement):
                merged, report = pipeline.merge_outputs_with_report(
                    nougat, docling, placement, False
                )
                self.assertEqual(merged.count("[^1]"), 2)
                self.assertEqual(
                    merged.count(
                        "[^1]: Existing Nougat definition is complete and should remain exactly once."
                    ),
                    1,
                )
                self.assertNotIn("[^1]: Docling evidence", merged)
                self.assertEqual(report["footnotes"]["placed"], 0)
                self.assertEqual(report["footnotes"]["unplaced"], 0)

    def test_duplicate_footnote_context_uses_later_ocr_digit_occurrence(self) -> None:
        nougat = (
            "The model has a duplicated explanatory paragraph.\n\n"
            "The model has a duplicated explanatory paragraph 1\n"
        )
        docling = (
            "The model has a duplicated explanatory paragraph 1\n\n"
            "1. This note is associated with the later duplicate paragraph and is long enough.\n"
        )
        merged, report = pipeline.merge_outputs_with_report(nougat, docling, "appended", False)
        occurrences = [
            line for line in merged.splitlines() if "duplicated explanatory paragraph" in line
        ]
        self.assertEqual(len(occurrences), 2)
        self.assertNotIn("[^1]", occurrences[0])
        self.assertIn("[^1]", occurrences[1])
        self.assertEqual(report["footnotes"], {"placed": 1, "unplaced": 0})

    def test_repeated_footnote_context_without_ocr_digit_stays_unplaced(self) -> None:
        nougat = (
            "The model repeats a duplicated explanatory paragraph.\n\n"
            "The model repeats a duplicated explanatory paragraph.\n"
        )
        docling = (
            "The model repeats a duplicated explanatory paragraph 1\n\n"
            "1. This note has no reliable matching OCR digit and should remain unplaced.\n"
        )
        merged, report = pipeline.merge_outputs_with_report(nougat, docling, "appended", False)
        body = merged.split("## Footnotes (Unplaced)", 1)[0]
        self.assertNotIn("[^1]", body)
        self.assertIn("## Footnotes (Unplaced)", merged)
        self.assertEqual(report["footnotes"], {"placed": 0, "unplaced": 1})

    def test_missing_referenced_asset_invalidates_only_docling(self) -> None:
        pdf = self.root / "missing-asset.pdf"
        pdf.write_bytes(b"%PDF-1.7\nasset")
        self.assertEqual(self.run_pipeline(pdf), 0)
        job_dir = next((self.root / "outputs").iterdir())
        (job_dir / "docling_assets" / "image-1.png").unlink()

        self.assertEqual(self.run_pipeline(pdf, "--resume"), 0)
        self.assertEqual((self.counter_dir / "nougat").read_text(), "1")
        self.assertEqual((self.counter_dir / "docling").read_text(), "2")

    def test_failed_force_marks_outputs_unavailable_until_resume(self) -> None:
        pdf = self.root / "stale.pdf"
        pdf.write_bytes(b"%PDF-1.7\nstale")
        self.assertEqual(self.run_pipeline(pdf), 0)
        job_dir = next((self.root / "outputs").iterdir())

        self.env["FAKE_FAIL_NOUGAT"] = "1"
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_pipeline(pdf, "--force")
        manifest = json.loads((job_dir / "manifest.json").read_text())
        self.assertEqual(manifest["stages"]["nougat"]["status"], "running")
        self.assertFalse((job_dir / "nougat.mmd").exists())
        self.assertFalse((job_dir / "stale.md").exists())

        self.env.pop("FAKE_FAIL_NOUGAT")
        self.assertEqual(self.run_pipeline(pdf, "--resume"), 0)
        self.assertEqual((self.counter_dir / "nougat").read_text(), "3")
        self.assertEqual((self.counter_dir / "docling").read_text(), "2")

    def test_same_stem_pdfs_use_distinct_jobs(self) -> None:
        first = self.root / "one" / "paper.pdf"
        second = self.root / "two" / "paper.pdf"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_bytes(b"%PDF-1.7\nfirst")
        second.write_bytes(b"%PDF-1.7\nsecond")

        self.assertEqual(self.run_pipeline(first), 0)
        self.assertEqual(self.run_pipeline(second), 0)
        job_dirs = sorted(path.name for path in (self.root / "outputs").iterdir())
        self.assertEqual(len(job_dirs), 2)
        self.assertNotEqual(job_dirs[0], job_dirs[1])

    def test_embedded_images_are_decoded_without_base64_in_output(self) -> None:
        source = self.root / "docling.md"
        encoded = base64.b64encode(PNG_BYTES).decode("ascii")
        source.write_text(f"![sample](data:image/png;base64,{encoded})\n")
        output = self.root / "decoded.md"
        figures = self.root / "figures"

        self.assertEqual(decode_docling_markdown(source, output, figures), 1)
        self.assertEqual(output.read_text(), "![sample](figures/figure-001.png)\n")
        self.assertEqual((figures / "figure-001.png").read_bytes(), PNG_BYTES)

    def test_referenced_images_are_decoded_from_asset_files(self) -> None:
        assets = self.root / "assets"
        assets.mkdir()
        (assets / "image.png").write_bytes(PNG_BYTES)
        source = assets / "paper.md"
        source.write_text("![sample](image.png)\n")
        output = self.root / "decoded.md"
        figures = self.root / "figures"

        self.assertEqual(decode_docling_markdown(source, output, figures), 1)
        self.assertEqual(output.read_text(), "![sample](figures/figure-001.png)\n")
        self.assertEqual((figures / "figure-001.png").read_bytes(), PNG_BYTES)

    def test_referenced_image_failure_falls_back_to_embedded(self) -> None:
        pdf = self.root / "fallback.pdf"
        pdf.write_bytes(b"%PDF-1.7\nfallback")
        self.env["FAKE_FAIL_REFERENCED"] = "1"

        self.assertEqual(self.run_pipeline(pdf), 0)
        job_dir = next((self.root / "outputs").iterdir())
        manifest = json.loads((job_dir / "manifest.json").read_text())
        self.assertEqual(manifest["stages"]["docling"]["image_mode"], "embedded")
        self.assertTrue((job_dir / "figures" / "figure-001.png").is_file())

    def test_inline_merge_reuses_formula_placeholder(self) -> None:
        merged = pipeline.merge_outputs(
            "Intro text $x+y$ end",
            "Intro text <!-- formula-not-decoded --> end\n\n![sample](figures/figure-001.png)",
            "inline",
            False,
        )
        self.assertIn("$x+y$", merged)
        self.assertIn("figures/figure-001.png", merged)

    def test_equations_tables_footnotes_and_metadata(self) -> None:
        tokens = pipeline.extract_tokens(
            "Text $x+y$\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n\\begin{tabular}{cc}a&b\\end{tabular}"
        )
        self.assertIn("formula", {token.type for token in tokens})
        self.assertIn("table", {token.type for token in tokens})
        self.assertIn("latex_table", {token.type for token in tokens})

        figures = (
            "A cited paper result 1\n\n"
            "1. This is a detailed footnote with a citation at https://example.com.\n\n"
            "Figure 1: A sample\n\n![sample](figures/figure-001.png)\n"
        )
        merged = pipeline.merge_outputs("A cited paper result is robust.", figures, "appended", False)
        self.assertIn("[^1]", merged)
        self.assertIn("## Figures", merged)
        self.assertIn("https://example.com", merged)

        metadata = pipeline.build_yaml_metadata("A title", "An author", "2026-08-19")
        self.assertIn('title: "A title"', metadata)
        self.assertIn('author: "An author"', metadata)

    def test_latex_preparation_closes_prose_fences(self) -> None:
        prepared = fix_fences("```\nA prose sentence.\n")
        self.assertEqual(prepared.count("```"), 2)
        self.assertTrue(prepared.endswith("```\n"))

        balanced = (
            "```\n"
            "1 var y;\n"
            "2 % This unusually long comment reads like prose but is still verbatim code.\n"
            "```\n\nFollowing prose.\n"
        )
        self.assertEqual(fix_fences(balanced), balanced)

    def test_missing_code_sidecar_merge_only_falls_back_and_resume_reruns_docling(self) -> None:
        pdf = self.root / "legacy-sidecar.pdf"
        pdf.write_bytes(b"%PDF-1.7\nlegacy-sidecar")
        self.env["FAKE_NOUGAT_MARKDOWN"] = "Before code anchors stay stable.\n\n```\n1 code\n```\n"
        self.assertEqual(self.run_pipeline(pdf), 0)
        job_dir = next((self.root / "outputs").iterdir())
        (job_dir / "docling_code.json").unlink()

        self.assertEqual(self.run_pipeline(pdf, "--merge-only"), 0)
        report = json.loads((job_dir / "merge_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["code_blocks"]["source"], "unavailable")
        self.assertEqual(report["code_blocks"]["fallback"], 1)
        self.assertEqual((self.counter_dir / "nougat").read_text(), "1")
        self.assertEqual((self.counter_dir / "docling").read_text(), "1")

        self.assertEqual(self.run_pipeline(pdf, "--resume"), 0)
        self.assertEqual((self.counter_dir / "nougat").read_text(), "1")
        self.assertEqual((self.counter_dir / "docling").read_text(), "2")
        self.assertTrue((job_dir / "docling_code.json").is_file())

    def test_success_removes_only_legacy_pipeline_owned_combined_files(self) -> None:
        pdf = self.root / "legacy-cleanup.pdf"
        pdf.write_bytes(b"%PDF-1.7\nlegacy-cleanup")
        self.assertEqual(self.run_pipeline(pdf), 0)
        job_dir = next((self.root / "outputs").iterdir())
        for name in ("combined.md", "combined_for_tex.md", "combined.tex", "combined.pdf", "combined.aux"):
            (job_dir / name).write_text("legacy", encoding="utf-8")

        self.assertEqual(self.run_pipeline(pdf, "--merge-only"), 0)
        for name in ("combined.md", "combined_for_tex.md", "combined.tex"):
            self.assertFalse((job_dir / name).exists(), name)
        for name in ("combined.pdf", "combined.aux"):
            self.assertTrue((job_dir / name).exists(), name)

    def test_fast_and_quality_profiles_smoke_with_a_real_pdf_input(self) -> None:
        pdf = self.root / "smoke.pdf"
        pdf.write_bytes(
            b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
            b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
        )
        self.assertEqual(self.run_pipeline(pdf, profile="fast"), 0)
        self.assertEqual(self.run_pipeline(pdf, profile="quality"), 0)
        job_dir = next((self.root / "outputs").iterdir())
        manifest = json.loads((job_dir / "manifest.json").read_text())
        self.assertEqual(manifest["profile"], "quality")

    def test_parallel_stages_are_opt_in(self) -> None:
        pdf = self.root / "parallel.pdf"
        pdf.write_bytes(b"%PDF-1.7\nparallel")
        self.assertEqual(self.run_pipeline(pdf, "--parallel-stages"), 0)
        self.assertEqual((self.counter_dir / "nougat").read_text(), "1")
        self.assertEqual((self.counter_dir / "docling").read_text(), "1")

    def test_merge_only_can_rebuild_v2_outputs_without_external_commands(self) -> None:
        pdf = self.root / "merge-only.pdf"
        pdf.write_bytes(b"%PDF-1.7\nmerge-only")
        self.assertEqual(self.run_pipeline(pdf), 0)
        argv = [
            "extract_pdf_to_md.py",
            str(pdf),
            "--profile",
            "fast",
            "--output-dir",
            str(self.root / "outputs"),
            "--merge-only",
            "--nougat-cmd",
            str(self.nougat),
        ]
        with patch.object(sys, "argv", argv):
            self.assertEqual(pipeline.main(), 0)
        self.assertEqual((self.counter_dir / "nougat").read_text(), "1")
        self.assertEqual((self.counter_dir / "docling").read_text(), "1")
        job_dir = next((self.root / "outputs").iterdir())
        merge_report = json.loads((job_dir / "merge_report.json").read_text())
        self.assertEqual(merge_report["schema"], "6")
        self.assertIn("ocr_audit", merge_report)
        self.assertTrue((job_dir / "ocr_audit.json").is_file())
        timings = json.loads((job_dir / "timings.json").read_text())
        self.assertIn("ocr_audit_seconds", timings)

        # An explicit skip also removes a stale report, while preserving the
        # expensive external-stage cache and the resolution ledger.
        self.assertEqual(self.run_pipeline(pdf, "--resume", "--skip-ocr-audit"), 0)
        self.assertFalse((job_dir / "ocr_audit.json").exists())
        self.assertFalse((job_dir / "ocr_audit.md").exists())
        skipped_report = json.loads((job_dir / "merge_report.json").read_text())
        self.assertEqual(skipped_report["ocr_audit"]["status"], "skipped")
        self.assertTrue((job_dir / "ocr_audit_review.json").is_file())

    def test_missing_markers_block_strict_success_when_audit_skipped(self):
        pdf = self.root / "missing.pdf"
        pdf.write_bytes(b"%PDF-1.7 synthetic")
        with patch.object(pipeline, "find_audit_interpreter", return_value=None):
            self.assertEqual(self.run_pipeline(pdf, "--skip-ocr-audit"), 0)
            job = next((self.root / "outputs").iterdir())
            (job / "nougat.mmd").write_text("Before\n[MISSING_PAGE_EMPTY:16]\n[MISSING_PAGE_FAIL:25]\n[MISSING_PAGE_POST]\n[MISSING_PAGE_POST:10]\nAfter")
            self.assertEqual(self.run_pipeline(pdf, "--merge-only", "--skip-ocr-audit", "--require-complete"), 2)
            report = json.loads((job / "merge_report.json").read_text())
            mapping = json.loads((job / "page_map.json").read_text())
            text = (job / "missing.md").read_text()
            self.assertTrue(report["validation"]["ok"])
            self.assertEqual(report["status"], "review_required")
            self.assertEqual(report["completeness"]["status"], "incomplete")
            self.assertEqual(report["completeness"]["missing_pages"], [10, 16, 25])
            self.assertIn("**Conversion requires review:**", text)
            for marker in mapping["missing_markers"]:
                a, b = marker["span"]
                self.assertTrue(text[a:b].startswith("[MISSING_PAGE_"))
            self.assertEqual(mapping["final_md_sha256"], hashlib.sha256(text.encode()).hexdigest())
            self.assertEqual(self.run_pipeline(pdf, "--merge-only", "--skip-ocr-audit", "--require-complete"), 2)
            self.assertEqual((job / "missing.md").read_text(), text)
            self.assertEqual((self.counter_dir / "nougat").read_text(), "1")
            self.assertEqual((self.counter_dir / "docling").read_text(), "1")
            base = (job / "recovery" / "base.md").read_text()
            replacements = []
            for number, kind in [(16, "EMPTY"), (25, "FAIL")]:
                original = f"[MISSING_PAGE_{kind}:{number}]"
                start = base.index(original)
                replacements.append({"page": number, "original": original,
                    "span": [start, start + len(original)], "replacement": f"Verified synthetic page {number}.",
                    "method": "transcription", "verification": {"source_page": number,
                    "visually_verified": True, "evidence": "Synthetic verification fixture."}})
            ledger = self.root / "recovery.json"
            ledger.write_text(json.dumps({"schema": "1", "pdf_sha256": pipeline.sha256_file(pdf),
                "base_md_sha256": hashlib.sha256(base.encode()).hexdigest(), "replacements": replacements}))
            with self.assertRaisesRegex(ValueError, "membership cannot be verified"):
                self.run_pipeline(pdf, "--merge-only", "--skip-ocr-audit",
                    "--require-complete", "--recovery-ledger", str(ledger))
            self.assertEqual((job / "missing.md").read_text(), text)
            self.assertEqual(json.loads((job / "merge_report.json").read_text()), report)

    def test_invalid_recovery_preserves_delivered_artifacts(self):
        if pipeline.find_audit_interpreter(None, {}) is None:
            self.skipTest("PDFium runtime unavailable")
        pdf = self.root / 'recovery.pdf'
        source = 'A stable uniquely identifiable paragraph with meaningful information.'
        write_text_pdf(pdf, [source])
        self.env['FAKE_NOUGAT_MARKDOWN'] = source
        self.assertEqual(self.run_pipeline(pdf, '--require-complete'), 0)
        job = next((self.root / 'outputs').iterdir())
        names = ['recovery.md', 'page_map.json', 'merge_report.json', 'ocr_audit.json', 'recovery/base.md']
        before = {name: (job/name).read_bytes() for name in names}
        base = (job/'recovery/base.md').read_text()
        start = base.index(source)
        ledger = {'schema': '1', 'pdf_sha256': pipeline.sha256_file(pdf),
                  'base_md_sha256': pipeline.digest(base), 'replacements': [{
                      'page': 999, 'original': source, 'replacement': 'Changed content.',
                      'span': [start, start+len(source)], 'method': 'transcription',
                      'verification': {'source_page': 999, 'visually_verified': True,
                                       'evidence': 'Synthetic verification fixture.'}}]}
        path = self.root/'ledger.json'
        for invalid in ['page', 'stale', 'boundary', 'asset']:
            with self.subTest(invalid=invalid):
                candidate = json.loads(json.dumps(ledger))
                if invalid == 'stale':
                    candidate['base_md_sha256'] = 'stale'
                if invalid == 'boundary':
                    candidate['replacements'][0]['span'] = [0, 1]
                if invalid == 'asset':
                    candidate['replacements'][0]['page'] = 1
                    candidate['replacements'][0]['verification']['source_page'] = 1
                    candidate['replacements'][0]['replacement'] = '![broken](figures/nonexistent.png)'
                path.write_text(json.dumps(candidate))
                with self.assertRaises(ValueError):
                    self.run_pipeline(pdf, '--merge-only', '--recovery-ledger', str(path))
                self.assertEqual({name: (job/name).read_bytes() for name in names}, before)
        ledger['replacements'][0]['page'] = 1
        ledger['replacements'][0]['verification']['source_page'] = 1
        ledger['replacements'][0]['replacement'] = source + ' Verified extra material.'
        path.write_text(json.dumps(ledger))
        self.assertEqual(self.run_pipeline(pdf, '--merge-only', '--recovery-ledger', str(path)), 0)
        accepted = {name: (job/name).read_bytes() for name in names[:3]}
        self.assertIn(b'Verified extra material.', accepted['recovery.md'])
        self.assertEqual(self.run_pipeline(pdf, '--merge-only', '--recovery-ledger', str(path)), 0)
        self.assertEqual({name: (job/name).read_bytes() for name in names[:3]}, accepted)

    def test_strict_mode_accounts_for_repeated_occurrences_in_real_pdf(self):
        interpreter = pipeline.find_audit_interpreter(None, {})
        if interpreter is None:
            self.skipTest("PDFium runtime unavailable")
        repeated = 'These assumptions determine household savings every period.'
        first = 'The opening section introduces aggregate production capital accumulation and firms. ' + repeated
        end = 'The following section investigates monetary policy shocks under sticky prices.'
        second = repeated + ' ' + end
        pdf = self.root / 'two-pages.pdf'
        write_text_pdf(pdf, [first, second])
        self.env['FAKE_NOUGAT_MARKDOWN'] = first + '\n\n2\n\n' + end
        self.assertEqual(self.run_pipeline(pdf, '--require-complete'), 2)
        job = next((self.root / 'outputs').iterdir())
        report = json.loads((job / 'merge_report.json').read_text())
        self.assertEqual(report['completeness']['status'], 'unknown')
        self.assertEqual(report['status'], 'review_required')
        (job / 'nougat.mmd').write_text(first + '\n\n2\n\n' + second)
        self.assertEqual(self.run_pipeline(pdf, '--merge-only', '--require-complete'), 0)
        report = json.loads((job / 'merge_report.json').read_text())
        self.assertEqual(report['completeness']['status'], 'complete')
        self.assertEqual(report['status'], 'clear')

    def test_audit_interpreter_discovers_windows_venv_python(self) -> None:
        nougat = self.root / "nougat.exe"
        interpreter = self.root / "python.exe"
        nougat.write_bytes(b"launcher")
        interpreter.write_bytes(b"python")
        nougat.chmod(nougat.stat().st_mode | stat.S_IXUSR)
        interpreter.chmod(interpreter.stat().st_mode | stat.S_IXUSR)

        with (
            patch.object(pipeline, "launcher_interpreter", return_value=None),
            patch.object(
                pipeline,
                "audit_interpreter_has_pdfium",
                side_effect=lambda candidate: Path(candidate) == interpreter,
            ),
            patch.object(pipeline.shutil, "which", return_value=None),
        ):
            self.assertEqual(pipeline.find_audit_interpreter(str(nougat), {}), str(interpreter))

    def test_audit_interpreter_resolves_basename_without_using_cwd(self) -> None:
        bindir = self.root / "venv" / "bin"
        bindir.mkdir(parents=True)
        nougat = bindir / "nougat"
        interpreter = bindir / "python"
        nougat.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        interpreter.write_bytes(b"python")
        nougat.chmod(nougat.stat().st_mode | stat.S_IXUSR)
        interpreter.chmod(interpreter.stat().st_mode | stat.S_IXUSR)
        manifest = {"tools": {"nougat": {"command": "nougat"}}}
        cwd_python = self.root / "python"
        cwd_python.write_bytes(b"python")
        cwd_python.chmod(cwd_python.stat().st_mode | stat.S_IXUSR)

        with (
            patch.object(pipeline, "launcher_interpreter", return_value=None),
            patch.object(
                pipeline,
                "audit_interpreter_has_pdfium",
                side_effect=lambda candidate: Path(candidate) == interpreter,
            ),
            patch.object(pipeline, "find_command", side_effect=lambda cmd: str(nougat) if cmd == "nougat" else None),
        ):
            self.assertEqual(pipeline.find_audit_interpreter(None, manifest), str(interpreter))

        with (
            patch.object(pipeline, "launcher_interpreter", return_value=None),
            patch.object(
                pipeline,
                "audit_interpreter_has_pdfium",
                side_effect=lambda candidate: Path(candidate) == interpreter,
            ),
            patch.object(pipeline, "find_command", return_value=None),
        ):
            self.assertEqual(pipeline.find_audit_interpreter(str(nougat), manifest), str(interpreter))

        previous = Path.cwd()
        os.chdir(self.root)
        try:
            with (
                patch.object(pipeline, "launcher_interpreter", return_value=None),
                patch.object(
                    pipeline,
                    "audit_interpreter_has_pdfium",
                    side_effect=lambda candidate: Path(candidate) == cwd_python,
                ),
                patch.object(pipeline, "find_command", return_value=None),
                patch.object(pipeline.shutil, "which", return_value=None),
            ):
                self.assertIsNone(pipeline.find_audit_interpreter(None, manifest))
        finally:
            os.chdir(previous)

    def test_job_artifacts_omit_absolute_output_root(self) -> None:
        pdf = self.root / "privacy.pdf"
        pdf.write_bytes(b"%PDF-1.7\nprivacy")
        output_dir = (self.root / "outputs").resolve()
        self.assertEqual(self.run_pipeline(pdf, output_dir=output_dir), 0)
        job_dir = next(output_dir.iterdir())
        manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["input"]["path"], pdf.name)
        self.assertEqual(manifest["tools"]["nougat"]["command"], self.nougat.name)
        self.assertEqual(manifest["tools"]["docling"]["command"], self.docling.name)
        self.assertEqual(manifest["config"]["nougat"]["tool"]["command"], self.nougat.name)
        self.assertEqual(manifest["config"]["docling"]["tool"]["command"], self.docling.name)
        self.assertEqual(manifest["runtime"]["python"], Path(sys.executable).name)
        self.assertNotIn(str(output_dir), json.dumps(manifest))
        needle = str(output_dir).encode("utf-8")
        for path in job_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(needle, path.read_bytes(), str(path.relative_to(job_dir)))

        self.assertEqual(self.run_pipeline(pdf, "--resume", output_dir=output_dir), 0)
        self.assertEqual((self.counter_dir / "nougat").read_text(), "1")
        self.assertEqual((self.counter_dir / "docling").read_text(), "1")
        self.assertEqual(self.run_pipeline(pdf, "--merge-only", output_dir=output_dir), 0)
        resumed = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(resumed["input"]["path"], pdf.name)
        self.assertEqual(resumed["tools"]["nougat"]["command"], self.nougat.name)

    def test_diagnostics_report_json_is_machine_readable(self) -> None:
        args = type(
            "Args",
            (),
            {
                "nougat_cmd": str(self.nougat),
                "docling_cmd": str(self.docling),
                "pandoc_cmd": None,
                "output_dir": self.root / "outputs",
            },
        )()
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(check_deps, "launcher_details", return_value={}),
        ):
            report = check_deps.build_report(args)
        self.assertTrue(report["ok"])
        self.assertEqual(report["commands"]["nougat"]["version"], "fake-nougat 1.0")
        self.assertFalse(report["commands"]["pandoc"]["required"])

    def test_full_pipeline_benchmark_reports_cached_run_and_equivalence(self) -> None:
        pdf = self.root / "bench.pdf"
        pdf.write_bytes(b"%PDF-1.7\nbenchmark")
        argv = [
            "benchmark_pipeline.py",
            str(pdf),
            "--profile",
            "fast",
            "--output-dir",
            str(self.root / "benchmarks"),
            "--nougat-cmd",
            str(self.nougat),
            "--docling-cmd",
            str(self.docling),
            "--runs",
            "2",
            "--resume",
        ]
        output = io.StringIO()
        with patch.dict(os.environ, self.env, clear=False), patch.object(sys, "argv", argv):
            with contextlib.redirect_stdout(output):
                self.assertEqual(benchmark_pipeline.main(), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(len(report["runs"]), 2)
        self.assertFalse(report["runs"][0]["stage_timings"]["nougat"]["cached"])
        self.assertTrue(report["runs"][1]["stage_timings"]["nougat"]["cached"])
        self.assertEqual(report["runs"][0]["output_sha256"], report["runs"][1]["output_sha256"])


if __name__ == "__main__":
    unittest.main()
