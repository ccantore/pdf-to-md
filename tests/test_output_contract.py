from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import extract_pdf_to_md as pipeline  # noqa: E402
from parse_docling_figures import normalize_docling_markdown  # noqa: E402
from prepare_md_for_latex import validate_latex_input  # noqa: E402


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "rbc_like"
PNG_BYTES = b"synthetic-rbc-png"


def write_fake_cli(path: Path, tool: str) -> Path:
    """Write a deterministic fake Nougat/Docling launcher."""

    if tool == "nougat":
        source = r'''#!/usr/bin/env python3
from pathlib import Path
import os
import sys

if "--version" in sys.argv:
    print("fake-nougat 1.0")
    raise SystemExit(0)
if "--help" in sys.argv:
    print("fake nougat help")
    raise SystemExit(0)

counter_dir = Path(os.environ["FAKE_COUNTER_DIR"])
counter_dir.mkdir(parents=True, exist_ok=True)
counter = counter_dir / "nougat"
counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))
pdf = next(Path(argument) for argument in sys.argv[1:] if argument.lower().endswith(".pdf"))
out = Path(sys.argv[sys.argv.index("--out") + 1])
out.mkdir(parents=True, exist_ok=True)
text = Path(os.environ["FAKE_NOUGAT_TEXT"]).read_text(encoding="utf-8")
(out / f"{pdf.stem}.mmd").write_text(text, encoding="utf-8")
'''
    else:
        source = r'''#!/usr/bin/env python3
from pathlib import Path
import json
import os
import sys

if "--version" in sys.argv:
    print("fake-docling 1.0")
    raise SystemExit(0)
if "--help" in sys.argv:
    print("fake docling help")
    raise SystemExit(0)

counter_dir = Path(os.environ["FAKE_COUNTER_DIR"])
counter_dir.mkdir(parents=True, exist_ok=True)
counter = counter_dir / "docling"
counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))
pdf = next(Path(argument) for argument in sys.argv[1:] if argument.lower().endswith(".pdf"))
out = Path(sys.argv[sys.argv.index("--output") + 1])
out.mkdir(parents=True, exist_ok=True)
scenario = os.environ.get("FAKE_DOCLING_SCENARIO", "absolute")
assets = out / "assets"
assets.mkdir(parents=True, exist_ok=True)

def image(number, alt):
    path = assets / f"image-{number}.png"
    path.write_bytes(b"synthetic-rbc-png-" + bytes([number]))
    return f"![{alt}](<{path}>)"

if scenario == "missing":
    text = "Figure 1: Missing image\n\n![Missing image](<" + str(assets / "missing.png") + ">)\n"
elif scenario == "outside":
    outside = Path(os.environ["FAKE_OUTSIDE_ASSET"])
    outside.write_bytes(b"outside-staging")
    text = "Figure 1: Outside image\n\n![Outside image](<" + str(outside) + ">)\n"
elif scenario == "ambiguous":
    text = Path(os.environ["FAKE_AMBIGUOUS_DOCLING"]).read_text(encoding="utf-8")
    text = text.replace("{{FIGURE_1}}", image(1, "Ambiguous figure"))
elif scenario == "ordered":
    template = Path(os.environ["FAKE_DOCLING_TEMPLATE"]).read_text(encoding="utf-8")
    text = template.replace("{{FIGURE_1}}", image(1, "First figure"))
    text = text.replace("{{FIGURE_2}}", image(2, "Second figure"))
else:
    template = Path(os.environ["FAKE_DOCLING_TEMPLATE"]).read_text(encoding="utf-8")
    text = template.replace("{{FIGURE_1}}", image(1, "Production function"))

(out / f"{pdf.stem}.md").write_text(text, encoding="utf-8")
raw_json = {"texts": [], "body": {"children": []}}
json_source = os.environ.get("FAKE_DOCLING_JSON")
if json_source:
    raw_json = json.loads(Path(json_source).read_text(encoding="utf-8"))
(out / f"{pdf.stem}.json").write_text(json.dumps(raw_json), encoding="utf-8")
'''
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def write_fake_pandoc(path: Path) -> Path:
    source = r'''#!/usr/bin/env python3
from pathlib import Path
import json
import os
import sys

log = Path(os.environ["FAKE_PANDOC_LOG"])
log.write_text(json.dumps({"cwd": os.getcwd(), "argv": sys.argv[1:]}), encoding="utf-8")
out = Path(sys.argv[sys.argv.index("-o") + 1])
out.write_text("\\documentclass{article}\n\\begin{document}\nSynthetic\\n\\end{document}\n", encoding="utf-8")
'''
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def image_targets(text: str) -> list[str]:
    return re.findall(r"!\[[^\]]*\]\((?:<)?([^)>]+)", text)


def report_path(report: dict[str, object], *paths: tuple[str, ...]) -> object:
    for path in paths:
        value: object = report
        for key in path:
            if not isinstance(value, dict) or key not in value:
                break
            value = value[key]
        else:
            return value
    raise AssertionError(f"merge_report.json has none of the expected fields: {paths}")


class OutputContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        # Keep both the executable and output tree below a path with spaces.
        self.root = Path(self.temp_dir.name) / "synthetic RBC workspace"
        self.root.mkdir(parents=True)
        self.output_dir = self.root / "outputs with spaces"
        self.counter_dir = self.root / "counters"
        self.nougat = write_fake_cli(self.root / "fake nougat.py", "nougat")
        self.docling = write_fake_cli(self.root / "fake docling.py", "docling")
        self.pandoc = write_fake_pandoc(self.root / "fake pandoc.py")
        self.nougat_text = self.root / "nougat.mmd"
        self.nougat_text.write_text(
            (FIXTURE_ROOT / "nougat.mmd").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.docling_template = self.root / "docling.md"
        self.docling_template.write_text(
            (FIXTURE_ROOT / "docling.md").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.env = {
            "FAKE_COUNTER_DIR": str(self.counter_dir),
            "FAKE_NOUGAT_TEXT": str(self.nougat_text),
            "FAKE_DOCLING_TEMPLATE": str(self.docling_template),
            "FAKE_DOCLING_SCENARIO": "absolute",
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_pipeline(
        self,
        pdf: Path,
        *extra: str,
        scenario: str = "absolute",
        nougat_text: str | None = None,
        docling_template: str | None = None,
    ) -> int:
        if nougat_text is not None:
            self.nougat_text.write_text(nougat_text, encoding="utf-8")
        if docling_template is not None:
            self.docling_template.write_text(docling_template, encoding="utf-8")
        env = {
            **self.env,
            "FAKE_DOCLING_SCENARIO": scenario,
            "FAKE_OUTSIDE_ASSET": str(self.root / "outside.png"),
        }
        argv = [
            "extract_pdf_to_md.py",
            str(pdf),
            "--profile",
            "fast",
            "--output-dir",
            str(self.output_dir),
            "--nougat-cmd",
            str(self.nougat),
            "--docling-cmd",
            str(self.docling),
            *extra,
        ]
        with patch.dict(os.environ, env, clear=False), patch.object(sys, "argv", argv):
            return pipeline.main()

    def job_dir(self) -> Path:
        jobs = [path for path in self.output_dir.iterdir() if path.is_dir()]
        self.assertEqual(len(jobs), 1)
        return jobs[0]

    @staticmethod
    def merged_path(job: Path, pdf: Path) -> Path:
        return job / f"{pipeline.safe_stem(pdf)}.md"

    def test_absolute_staging_assets_are_durable_and_staging_is_removed(self) -> None:
        pdf = self.root / "synthetic RBC paper.pdf"
        pdf.write_bytes(b"%PDF-1.7 synthetic RBC")

        self.assertEqual(self.run_pipeline(pdf), 0)
        job = self.job_dir()
        self.assertFalse((job / ".staging").exists())
        self.assertTrue((job / "docling_assets" / "assets" / "image-1.png").is_file())
        self.assertTrue((job / "docling_code.json").is_file())
        self.assertTrue((job / "synthetic_RBC_paper.md").is_file())
        self.assertFalse((job / "combined.md").exists())

        docling = (job / "docling.md").read_text(encoding="utf-8")
        outputs = (job / "docling.md", job / "docling_decoded.md", self.merged_path(job, pdf))
        for output in outputs:
            text = output.read_text(encoding="utf-8")
            self.assertNotIn(".staging", text, output.name)
            self.assertNotIn(str(self.root), text, output.name)
        self.assertIn("docling_assets/assets/image-1.png", docling)
        self.assertTrue((job / "figures" / "figure-001.png").is_file())

        report = json.loads((job / "merge_report.json").read_text(encoding="utf-8"))
        self.assertIsInstance(report, dict)
        self.assertEqual(report["schema"], "6")

    def test_resume_and_merge_only_repair_stale_absolute_cached_docling(self) -> None:
        pdf = self.root / "repair.pdf"
        pdf.write_bytes(b"%PDF-1.7 repair")
        self.assertEqual(self.run_pipeline(pdf), 0)
        job = self.job_dir()
        self.assertEqual((self.counter_dir / "nougat").read_text(), "1")
        self.assertEqual((self.counter_dir / "docling").read_text(), "1")

        # The old v2 job retained a durable copy but left the Markdown target
        # pointing at the staging path that had already been deleted.
        stale_root = job / ".staging" / "docling"
        durable_asset = job / "docling_assets" / "assets" / "repair.png"
        durable_asset.write_bytes(PNG_BYTES)
        stale_markdown = f"Figure 1: Repaired\n\n![Repaired](<{stale_root / 'assets' / 'repair.png'}>)\n"
        (job / "docling.md").write_text(stale_markdown, encoding="utf-8")
        # Emulate the raw cached artifact produced by the older v2
        # implementation while retaining the completed stage manifest.
        cached_raw = job / "docling_assets" / "cached.md"
        cached_raw.write_text(stale_markdown, encoding="utf-8")

        self.assertEqual(self.run_pipeline(pdf, "--resume"), 0)
        self.assertEqual((self.counter_dir / "nougat").read_text(), "1")
        self.assertEqual((self.counter_dir / "docling").read_text(), "1")
        self.assertNotIn(".staging", (job / "docling.md").read_text(encoding="utf-8"))
        self.assertTrue((job / "docling_assets" / "assets" / "repair.png").is_file())
        self.assertFalse((job / ".staging").exists())

        self.assertEqual(self.run_pipeline(pdf, "--merge-only"), 0)
        self.assertEqual((self.counter_dir / "nougat").read_text(), "1")
        self.assertEqual((self.counter_dir / "docling").read_text(), "1")
        self.assertNotIn(".staging", self.merged_path(job, pdf).read_text(encoding="utf-8"))

    def test_inline_uses_nougat_body_without_duplicate_structured_content(self) -> None:
        nougat = (FIXTURE_ROOT / "nougat.mmd").read_text(encoding="utf-8")
        docling = (FIXTURE_ROOT / "docling.md").read_text(encoding="utf-8").replace(
            "{{FIGURE_1}}", "![Production function](figures/figure-001.png)"
        )
        docling += "\nDocling-only diagnostic prose should never become the document body.\n"

        merged = pipeline.merge_outputs(nougat, docling, "inline", False)
        self.assertIn("# RBC Quantitative Benchmark", merged)
        self.assertEqual(merged.count("The representative household chooses consumption"), 1)
        self.assertEqual(merged.count("c_t^{-\\sigma}"), 1)
        self.assertEqual(merged.count("| Parameter | Value |"), 1)
        self.assertNotIn("Docling-only diagnostic prose", merged)
        self.assertEqual(len(image_targets(merged)), 1)
        self.assertEqual(merged.count("Production function"), 1)

    def test_inline_figure_anchors_are_monotonic(self) -> None:
        nougat = "Intro alpha comes first. Middle beta follows. Conclusion gamma comes last.\n"
        docling = (
            "Intro alpha comes first.\n\n"
            "Figure 1: First figure\n\n"
            "![First figure](figures/figure-001.png)\n\n"
            "Middle beta follows.\n\n"
            "Figure 2: Second figure\n\n"
            "![Second figure](figures/figure-002.png)\n\n"
            "Conclusion gamma comes last.\n"
        )
        merged = pipeline.merge_outputs(nougat, docling, "inline", False)
        first = merged.find("figures/figure-001.png")
        second = merged.find("figures/figure-002.png")
        self.assertGreaterEqual(first, 0)
        self.assertGreater(second, first)
        self.assertEqual(len(image_targets(merged)), 2)

    def test_ambiguous_inline_anchor_is_appended_with_report_warning(self) -> None:
        nougat = (
            "The repeated anchor appears before the repeated anchor and after the repeated anchor.\n"
        )
        docling = (
            "The repeated anchor appears before the repeated anchor and after the repeated anchor.\n\n"
            "Figure 1: Ambiguous figure\n{{FIGURE_1}}\n"
        )
        ambiguous_path = self.root / "ambiguous_docling.md"
        ambiguous_path.write_text(docling, encoding="utf-8")
        self.env["FAKE_AMBIGUOUS_DOCLING"] = str(ambiguous_path)
        pdf = self.root / "ambiguous.pdf"
        pdf.write_bytes(b"%PDF-1.7 ambiguous")

        self.assertEqual(
            self.run_pipeline(
                pdf,
                "--figure-placement",
                "inline",
                nougat_text=nougat,
                scenario="ambiguous",
            ),
            0,
        )
        job = self.job_dir()
        combined = self.merged_path(job, pdf).read_text(encoding="utf-8")
        self.assertIn("Unplaced Figures", combined)
        report = json.loads((job / "merge_report.json").read_text(encoding="utf-8"))
        unplaced = report_path(report, ("figures", "unplaced"))
        self.assertEqual(unplaced, 1)
        self.assertTrue(report["warnings"])

    def test_footnote_reconstruction_is_complete_in_both_placement_modes(self) -> None:
        nougat = "The representative household chooses consumption and labor in each period.\n"
        figures = (
            "The representative household chooses consumption and labor in each period 1\n\n"
            "1. This footnote begins on the first page and cites prior work.\n"
            "Figure 1: Production function\n"
            "![Production function](figures/figure-001.png)\n"
            "The continuation includes the estimate URL https://example.com/rbc.\n"
        )
        for placement in ("appended", "inline"):
            with self.subTest(placement=placement):
                merged = pipeline.merge_outputs(nougat, figures, placement, False)
                self.assertEqual(merged.count("[^1]"), 2)
                self.assertIn("This footnote begins on the first page", merged)
                self.assertIn("The continuation includes the estimate URL", merged)
                self.assertIn("https://example.com/rbc", merged)
                self.assertNotIn("Footnotes (Unplaced)", merged)

    def test_figure_caption_is_emitted_once(self) -> None:
        figures = "Figure 1: Production function\n\n![Production function](figures/figure-001.png)\n"
        merged = pipeline.merge_outputs("The production model follows.\n", figures, "appended", False)
        self.assertEqual(merged.count("Production function"), 1)
        self.assertEqual(len(image_targets(merged)), 1)

    def test_missing_and_outside_staging_assets_are_rejected(self) -> None:
        stage = self.root / ".staging" / "docling"
        stage.mkdir(parents=True)
        assets = self.root / "docling_assets"
        source = stage / "paper.md"
        output = self.root / "normalized.md"

        source.write_text("![missing](<" + str(stage / "missing.png") + ">)\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            normalize_docling_markdown(source, output, assets, stage)

        outside = self.root / "outside.png"
        outside.write_bytes(PNG_BYTES)
        source.write_text("![outside](<" + str(outside) + ">)\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            normalize_docling_markdown(source, output, assets, stage)

    def test_latex_static_validation_rejects_broken_inputs(self) -> None:
        for text in (
            "![absolute](/tmp/figure.png)\n",
            "![staged](.staging/docling/image.png)\n",
            "<!-- formula-not-decoded -->\n",
            "Unsupported raw Unicode: ✓\n",
        ):
            with self.subTest(text=text):
                self.assertTrue(validate_latex_input(text))
        self.assertFalse(validate_latex_input("A valid sentence with $x+y$.\n"))

    def test_latex_pandoc_runs_from_job_with_explicit_resource_paths(self) -> None:
        pdf = self.root / "latex.pdf"
        pdf.write_bytes(b"%PDF-1.7 latex")
        log = self.root / "pandoc.json"
        self.env["FAKE_PANDOC_LOG"] = str(log)

        self.assertEqual(
            self.run_pipeline(pdf, "--to-latex", "--pandoc-cmd", str(self.pandoc)),
            0,
        )
        job = self.job_dir()
        invocation = json.loads(log.read_text(encoding="utf-8"))
        self.assertEqual(Path(invocation["cwd"]).resolve(), job.resolve())
        resource_args = [arg for arg in invocation["argv"] if "resource-path" in arg]
        resource_text = " ".join(resource_args)
        self.assertIn("figures", resource_text)
        self.assertIn("docling_assets", resource_text)
        self.assertTrue((job / "latex_for_tex.md").is_file())
        self.assertTrue((job / "latex.tex").is_file())


if __name__ == "__main__":
    unittest.main()
