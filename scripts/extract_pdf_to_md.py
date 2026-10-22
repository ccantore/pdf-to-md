#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from bisect import bisect_right
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import unquote

from completeness import apply_recoveries, digest, fallback, WARNING

from check_deps import launcher_interpreter, package_version_from_command
from parse_docling_figures import decode_docling_markdown, normalize_docling_markdown
from prepare_md_for_latex import fix_fences, validate_latex_input
from prepare_md_for_latex import pdftex_header_includes


V2_VERSION = "2"
# This changes merge/normalization behavior only.  It deliberately does not
# participate in the expensive Nougat/Docling cache keys.
MERGE_SCHEMA_VERSION = "6"
DOCLING_CODE_SCHEMA_VERSION = "1"
PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "fast": {
        "nougat_args": ["-m", "0.1.0-small", "--batchsize", "1"],
        "docling_args": ["--page-batch-size", "1", "--num-threads", "2"],
    },
    "quality": {
        "nougat_args": ["-m", "0.1.0-base"],
        "docling_args": ["--page-batch-size", "4", "--num-threads", "4"],
    },
}


def executable_file(path: Path) -> bool:
    return path.is_file() and (os.name == "nt" or os.access(path, os.X_OK))


def find_command(cmd: str) -> str | None:
    bin_dir = Path(sys.executable).parent
    candidate = bin_dir / cmd
    if executable_file(candidate):
        return str(candidate)
    return shutil.which(cmd)


def require_command(cmd: str, override: str | None) -> str:
    if override:
        path = Path(override).expanduser()
        if not executable_file(path):
            print(f"Missing required command at path: {path}", file=sys.stderr)
            raise SystemExit(1)
        return str(path)

    resolved = find_command(cmd)
    if resolved is None:
        print(f"Missing required command: {cmd}", file=sys.stderr)
        raise SystemExit(1)
    return resolved


def resolve_command_path(command: str) -> Path | None:
    """Resolve a command without treating a bare name as a relative path."""

    raw = command.strip()
    if not raw:
        return None
    expanded = Path(raw).expanduser()
    if expanded.is_absolute() or raw.startswith("~") or any(separator in raw for separator in ("/", "\\")):
        return expanded if executable_file(expanded) else None
    found = find_command(raw)
    if found is None:
        return None
    return Path(found)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_stem(path: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", path.stem).strip("._")
    return stem or "document"


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=destination.parent, prefix=f".{destination.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            with source.open("rb") as source_handle:
                shutil.copyfileobj(source_handle, handle)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def reset_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def cleanup_staging_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _executable_name(command: str) -> str:
    return Path(command).name or command


def _scrub_command_field(record: Any) -> None:
    if isinstance(record, dict) and isinstance(record.get("command"), str) and record["command"].strip():
        record["command"] = _executable_name(record["command"])


def persisted_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Copy a manifest for disk. Absolute paths stay in the in-memory object."""

    cleaned: dict[str, Any] = json.loads(json.dumps(manifest))
    input_record = cleaned.get("input")
    if isinstance(input_record, dict) and isinstance(input_record.get("path"), str) and input_record["path"].strip():
        input_record["path"] = Path(input_record["path"]).name
    tools = cleaned.get("tools")
    if isinstance(tools, dict):
        for tool in tools.values():
            _scrub_command_field(tool)
    config = cleaned.get("config")
    if isinstance(config, dict):
        for stage in config.values():
            if isinstance(stage, dict):
                _scrub_command_field(stage.get("tool"))
    runtime = cleaned.get("runtime")
    if isinstance(runtime, dict) and isinstance(runtime.get("python"), str) and runtime["python"].strip():
        runtime["python"] = Path(runtime["python"]).name
    return cleaned


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    write_json(path, persisted_manifest(manifest))


def scrub_report_files(job_dir: Path, output_root: Path, pdf_path: Path) -> None:
    """Drop absolute job and input paths from reports if a tool echoed them."""

    replacements = [
        (str(job_dir.resolve()), job_dir.name),
        (str(output_root.resolve()), output_root.name),
        (str(pdf_path.resolve()), pdf_path.name),
    ]
    replacements = [(old, new) for old, new in replacements if old and old != new]
    replacements.sort(key=lambda item: len(item[0]), reverse=True)
    for name in (
        "merge_report.json",
        "page_map.json",
        "ocr_audit.json",
        "ocr_audit.md",
        "ocr_audit_review.json",
        "timings.json",
    ):
        path = job_dir / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        redacted = text
        for old, new in replacements:
            redacted = redacted.replace(old, new)
        if redacted != text:
            atomic_write_text(path, redacted)


def command_version(command: str, package: str) -> str:
    package_version = package_version_from_command(command, package)
    if package_version:
        return package_version
    try:
        result = subprocess.run(
            [command, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    output = (result.stdout or result.stderr).strip()
    return output.splitlines()[0][:500] if output else "unknown"


def apply_profile_args(profile: str, extra: list[str], key: str) -> list[str]:
    return [*PROFILE_DEFAULTS[profile][key], *extra]


def find_single_output(directory: Path, suffix: str, input_stem: str) -> Path:
    expected = directory / f"{input_stem}{suffix}"
    if expected.exists():
        return expected
    candidates = sorted(directory.glob(f"*{suffix}"))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise RuntimeError(f"No {suffix} output found in {directory}.")
    names = ", ".join(candidate.name for candidate in candidates)
    raise RuntimeError(f"Expected one {suffix} output in {directory}; found: {names}")


def run_nougat(
    nougat_cmd: str,
    pdf_path: Path,
    text_md: Path,
    job_dir: Path,
    nougat_args: list[str],
) -> None:
    stage_dir = job_dir / ".staging" / "nougat"
    reset_directory(stage_dir)
    command = [
        nougat_cmd,
        *nougat_args,
        str(pdf_path),
        "--markdown",
        "--out",
        str(stage_dir),
    ]
    subprocess.run(command, check=True)
    output = find_single_output(stage_dir, ".mmd", pdf_path.stem)
    if output.stat().st_size == 0:
        raise RuntimeError("Nougat produced an empty Markdown output.")
    atomic_copy(output, text_md)
    cleanup_staging_dir(stage_dir)


def run_docling(
    docling_cmd: str,
    pdf_path: Path,
    figures_md: Path,
    job_dir: Path,
    docling_args: list[str],
    image_mode: str,
) -> tuple[Path, str]:
    stage_dir = job_dir / ".staging" / "docling"

    def run_once(mode: str, include_image_mode: bool = True) -> None:
        reset_directory(stage_dir)
        command = [docling_cmd, *docling_args, "--to", "md", "--to", "json"]
        if include_image_mode:
            command.extend(["--image-export-mode", mode])
        command.extend(["--output", str(stage_dir), str(pdf_path)])
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if result.returncode:
            raise subprocess.CalledProcessError(
                result.returncode,
                command,
                output=result.stdout,
                stderr=result.stderr,
            )

        output = find_single_output(stage_dir, ".md", pdf_path.stem)
        if output.stat().st_size == 0:
            raise RuntimeError("Docling produced an empty Markdown output.")
        raw_json = find_single_output(stage_dir, ".json", pdf_path.stem)
        if raw_json.stat().st_size == 0:
            raise RuntimeError("Docling produced an empty JSON output.")

    actual_mode = image_mode
    try:
        run_once(image_mode)
    except (RuntimeError, subprocess.CalledProcessError):
        if image_mode != "referenced":
            raise
        print(
            "Docling referenced-image export failed; retrying with embedded images.",
            file=sys.stderr,
        )
        actual_mode = "embedded"
        try:
            run_once(actual_mode)
        except subprocess.CalledProcessError as error:
            error_text = f"{error.output or ''}\n{error.stderr or ''}".lower()
            if "--image-export-mode" not in error_text:
                raise
            print(
                "Docling does not recognize --image-export-mode; using its embedded default.",
                file=sys.stderr,
            )
            run_once(actual_mode, include_image_mode=False)
    output = find_single_output(stage_dir, ".md", pdf_path.stem)
    raw_json = find_single_output(stage_dir, ".json", pdf_path.stem)

    assets_dir = job_dir / "docling_assets"
    reset_directory(assets_dir)
    # Preserve only assets referenced by Markdown.  In particular, never let
    # absolute paths back into the cached output after .staging is removed.
    normalize_docling_markdown(output, figures_md, assets_dir, stage_dir)
    write_docling_code_sidecar(raw_json, job_dir / "docling_code.json")
    cleanup_staging_dir(stage_dir)
    return figures_md, actual_mode


def _docling_text_entries(document: dict[str, Any]) -> list[dict[str, Any]]:
    texts = document.get("texts")
    if not isinstance(texts, list):
        return []
    by_ref = {
        item.get("self_ref"): item
        for item in texts
        if isinstance(item, dict) and isinstance(item.get("self_ref"), str)
    }
    body = document.get("body")
    children = body.get("children") if isinstance(body, dict) else None
    if not isinstance(children, list):
        return [item for item in texts if isinstance(item, dict)]
    ordered: list[dict[str, Any]] = []
    for child in children:
        reference = child.get("$ref") if isinstance(child, dict) else None
        item = by_ref.get(reference)
        if item is not None:
            ordered.append(item)
    return ordered


def _code_provenance(item: dict[str, Any]) -> list[dict[str, Any]]:
    provenance = item.get("prov")
    if not isinstance(provenance, list):
        return []
    result: list[dict[str, Any]] = []
    for entry in provenance:
        if not isinstance(entry, dict) or not isinstance(entry.get("page_no"), int):
            continue
        compact = {"page": entry["page_no"]}
        bbox = entry.get("bbox")
        if isinstance(bbox, dict):
            compact["bbox"] = {
                key: bbox[key]
                for key in ("l", "t", "r", "b", "coord_origin")
                if key in bbox
            }
        result.append(compact)
    return result


def extract_docling_code_groups(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep page-ordered code and its nearest body-text anchors."""

    entries = _docling_text_entries(document)
    furniture = {"page_header", "page_footer"}
    groups: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    before_text = ""

    def finish(after_text: str) -> None:
        nonlocal current
        if not current:
            return
        groups.append(
            {
                "order": len(groups),
                "before_text": before_text,
                "after_text": after_text,
                "blocks": current,
            }
        )
        current = []

    for item in entries:
        label = item.get("label")
        text = item.get("orig") or item.get("text") or ""
        if label == "code" and isinstance(text, str) and text.strip():
            current.append(
                {
                    "text": text,
                    "language": item.get("code_language") or "unknown",
                    "provenance": _code_provenance(item),
                }
            )
            continue
        if label in furniture or not isinstance(text, str) or not text.strip():
            continue
        if current:
            finish(text)
        before_text = text
    finish("")
    return groups


def write_docling_code_sidecar(source: Path, destination: Path) -> None:
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Cannot read Docling JSON output: {exc}") from exc
    if not isinstance(document, dict):
        raise RuntimeError("Docling JSON output is not an object.")
    write_json(
        destination,
        {
            "schema": DOCLING_CODE_SCHEMA_VERSION,
            "groups": extract_docling_code_groups(document),
        },
    )


def load_docling_code_groups(path: Path) -> list[dict[str, Any]] | None:
    sidecar = load_manifest(path)
    if sidecar.get("schema") != DOCLING_CODE_SCHEMA_VERSION:
        return None
    groups = sidecar.get("groups")
    if not isinstance(groups, list) or not all(isinstance(group, dict) for group in groups):
        return None
    return groups


FORMULA_RE = re.compile(
    r"\$\$.*?\$\$"
    r"|\\\[.*?\\\]"
    r"|\\begin\{(equation\*?|align\*?|gather\*?|multline\*?|eqnarray\*?)\}.*?\\end\{\1\}"
    r"|\\\(.*?\\\)"
    r"|(?<!\$)\$(?!\$)[^\n]+?(?<!\$)\$(?!\$)",
    re.DOTALL,
)

FENCED_RE = re.compile(r"```.*?```", re.DOTALL)
TRIPLE_QUOTE_RE = re.compile(r"'''[\\s\\S]*?'''", re.DOTALL)
VERBATIM_RE = re.compile(r"\\begin\{verbatim\}.*?\\end\{verbatim\}", re.DOTALL)
TABULAR_RE = re.compile(r"\\begin\{tabular\}.*?\\end\{tabular\}", re.DOTALL)

STRUCTURED_RE = re.compile(
    r"\$\$|\\\[|\\\(|\\begin\{|```|'''|\\begin\{tabular\}",
    re.DOTALL,
)
IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^\n]*\)(?=\s*$)", re.MULTILINE)
MARKDOWN_IMAGE_TARGET_RE = re.compile(
    r"!\[[^\]]*\]\((?P<target>[^\n]*)\)(?=\s*$)", re.MULTILINE
)
CAPTION_RE = re.compile(r"^(fig(?:ure)?\.?)\b", re.IGNORECASE)
TABLE_RE = re.compile(r"^(table\.?)\b", re.IGNORECASE)
TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{1,}:?\s*(?:\|\s*:?-{1,}:?\s*)+\|?\s*$"
)
HEADING_RE = re.compile(r"^#{1,6}\s")
FIGURE_CAP_RE = re.compile(r"^Figure\s+\d+:\s+(.+)$")
CANONICAL_FIGURE_CAP_RE = re.compile(r"^(?:fig(?:ure)?\.?\s*)\d+\s*[:.\-–]\s*(.+)$", re.IGNORECASE)
TABLE_CAP_RE = re.compile(r"^Table\s+\d+:\s+(.+)$")
FOOTNOTE_START_RE = re.compile(r"^(?P<num>\d+)[\).]?\s+(?P<body>.+)")


@dataclass
class Token:
    type: str
    text: str
    start: int
    end: int
    inline: bool = False


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < s_end and end > s_start for s_start, s_end in spans)


def extract_markdown_tables(text: str, occupied: list[tuple[int, int]]) -> list[Token]:
    tokens: list[Token] = []
    lines = text.splitlines(keepends=True)
    line_starts: list[int] = []
    pos = 0
    for line in lines:
        line_starts.append(pos)
        pos += len(line)

    i = 0
    while i < len(lines) - 1:
        line = lines[i]
        next_line = lines[i + 1]
        if "|" in line and TABLE_SEPARATOR_RE.match(next_line):
            start = line_starts[i]
            j = i + 2
            while j < len(lines) and "|" in lines[j]:
                j += 1
            end = line_starts[j] if j < len(lines) else len(text)
            if not _overlaps(start, end, occupied):
                tokens.append(Token(type="table", text=text[start:end], start=start, end=end))
                occupied.append((start, end))
            i = j
            continue
        i += 1
    return tokens


def extract_tokens(text: str) -> list[Token]:
    tokens: list[Token] = []
    occupied: list[tuple[int, int]] = []

    for regex in (FENCED_RE, TRIPLE_QUOTE_RE, VERBATIM_RE):
        for m in regex.finditer(text):
            tokens.append(Token(type="verbatim", text=m.group(0), start=m.start(), end=m.end()))
            occupied.append((m.start(), m.end()))

    for m in TABULAR_RE.finditer(text):
        tokens.append(Token(type="latex_table", text=m.group(0), start=m.start(), end=m.end()))
        occupied.append((m.start(), m.end()))

    tokens.extend(extract_markdown_tables(text, occupied))

    for m in FORMULA_RE.finditer(text):
        if _overlaps(m.start(), m.end(), occupied):
            continue
        f = m.group(0).strip()
        inline = f.startswith("\\(") or (f.startswith("$") and not f.startswith("$$"))
        tokens.append(Token(type="formula", text=f, start=m.start(), end=m.end(), inline=inline))
        occupied.append((m.start(), m.end()))

    return sorted(tokens, key=lambda t: t.start)


def normalize_display(formula: str) -> str:
    f = formula.strip()
    if f.startswith("\\[") and f.endswith("\\]"):
        inner = f[2:-2].strip()
        return f"$$\n{inner}\n$$"
    return f


def normalize_inline(formula: str) -> str:
    f = formula.strip()
    if f.startswith("\\(") and f.endswith("\\)"):
        inner = f[2:-2].strip()
        return f"${inner}$"
    return f


def format_formula_for_placeholder(token: Token) -> str:
    if token.inline:
        return normalize_inline(token.text)
    return normalize_display(token.text)


def replace_placeholders_with_formulas(content: str, formulas: list[Token]) -> tuple[str, int]:
    marker = "## Formulas (Nougat)"
    if marker in content:
        content = content.split(marker, 1)[0].rstrip() + "\n"

    placeholders = list(re.finditer(r"<!-- formula-not-decoded -->", content))
    if not placeholders:
        return content, 0

    out_parts: list[str] = []
    last_idx = 0
    inserted = 0
    for ph in placeholders:
        out_parts.append(content[last_idx:ph.start()])
        if inserted < len(formulas):
            out_parts.append("\n" + format_formula_for_placeholder(formulas[inserted]) + "\n")
        else:
            out_parts.append("\n<!-- formula-missing -->\n")
        inserted += 1
        last_idx = ph.end()
    out_parts.append(content[last_idx:])
    return "".join(out_parts), inserted


def normalize_bracket_math(text: str) -> str:
    return re.sub(r"\\\[(.*?)\\\]", lambda m: f"$$\n{m.group(1).strip()}\n$$", text, flags=re.DOTALL)


def normalize_inline_math(text: str) -> str:
    return re.sub(r"\\\((.*?)\\\)", lambda m: f"${m.group(1).strip()}$", text, flags=re.DOTALL)


def yaml_escape(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("\"", "\\\"")
    return escaped.replace("\n", " ")


def build_yaml_metadata(title: str | None, author: str | None, date: str | None) -> str:
    items: list[str] = []
    if title:
        items.append(f"title: \"{yaml_escape(title)}\"")
    if author:
        items.append(f"author: \"{yaml_escape(author)}\"")
    if date:
        items.append(f"date: \"{yaml_escape(date)}\"")
    if not items:
        return ""
    return "---\n" + "\n".join(items) + "\n---\n\n"


def extract_figure_blocks(text: str) -> list[str]:
    lines = text.splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if IMAGE_RE.search(line):
            block_lines: list[str] = []
            caption_candidates: list[str] = []
            prev = i - 1
            while prev >= 0 and not lines[prev].strip():
                prev -= 1
            if prev >= 0:
                prev_text = lines[prev].strip()
                if TABLE_RE.match(prev_text):
                    i = i + 1
                    continue
                if CAPTION_RE.match(prev_text):
                    caption_line = lines[prev].rstrip()
                    block_lines.append(caption_line)
                    caption_candidates.append(caption_line)
            block_lines.append(line.rstrip())
            j = i + 1
            while j < len(lines):
                if IMAGE_RE.search(lines[j]) and lines[j].strip():
                    break
                if HEADING_RE.match(lines[j].strip()):
                    break
                if not lines[j].strip():
                    break
                caption_line = lines[j].rstrip()
                block_lines.append(caption_line)
                caption_candidates.append(caption_line)
                j += 1
            if any(TABLE_RE.match(c.strip()) for c in caption_candidates):
                i = j
                continue
            block = "\n".join(block_lines).strip()
            if block:
                blocks.append(block)
            i = j
            continue
        i += 1
    return blocks


@dataclass
class FigureBlock:
    markdown: str
    source_offset: int


def canonical_figure_caption(caption: str) -> str:
    """Leave figure numbering to Pandoc instead of duplicating its caption prefix."""

    match = CANONICAL_FIGURE_CAP_RE.match(caption.strip())
    return match.group(1).strip() if match else caption.strip()


def extract_captioned_figures(text: str) -> list[FigureBlock]:
    """Return just the durable image and its caption, never Docling prose."""

    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line)

    figures: list[FigureBlock] = []
    for index, line in enumerate(lines):
        image = IMAGE_RE.search(line)
        if image is None:
            continue
        caption = ""
        # Docling occasionally leaves one or two OCR artefact lines between a
        # figure caption and its image.  Use the closest nearby caption, but do
        # not cross another image or heading.
        for before in range(index - 1, max(-1, index - 13), -1):
            candidate = lines[before].strip()
            if IMAGE_RE.search(lines[before]) or HEADING_RE.match(candidate):
                break
            if CAPTION_RE.match(candidate) and not TABLE_RE.match(candidate):
                caption = candidate
                break
        if not caption:
            after = index + 1
            while after < len(lines) and not lines[after].strip():
                after += 1
            if after < len(lines) and CAPTION_RE.match(lines[after].strip()) and not TABLE_RE.match(lines[after].strip()):
                caption = lines[after].strip()
        alt = image.group(0).split("](", 1)[0][2:]
        caption = canonical_figure_caption(caption or alt or "Figure")
        target = image.group(0).split("](", 1)[1].rsplit(")", 1)[0]
        figures.append(FigureBlock(f"![{caption}]({target})", offsets[index] + image.start()))
    return figures


def extract_footnote_defs(text: str) -> dict[str, str]:
    lines = text.splitlines()
    foot_defs: dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        m = FOOTNOTE_START_RE.match(line.strip())
        if not m:
            i += 1
            continue

        num = m.group("num")
        prev = i - 1
        while prev >= 0 and not lines[prev].strip():
            prev -= 1
        prev_line = lines[prev].rstrip() if prev >= 0 else ""
        prev_match = bool(re.search(rf"(?:\b{re.escape(num)}\b)[\s.]*$", prev_line))

        body = m.group("body").strip()
        block = [body] if body else []
        j = i + 1
        # First consume the actual footnote paragraph.  A blank line normally
        # ends it.  An unfinished footnote can continue after a page break,
        # figures, and intervening body paragraphs, but only as a lower-case
        # orphan paragraph nearby; Docling body prose normally starts a new
        # sentence/paragraph with an upper-case character.
        while j < len(lines) and lines[j].strip():
            candidate = lines[j].strip()
            if HEADING_RE.match(candidate) or FOOTNOTE_START_RE.match(candidate):
                break
            block.append(candidate)
            j += 1
        initial_complete = bool(block and re.search(r"[.!?][\"')\]]*$", block[-1]))
        if not initial_complete:
            scanned = 0
            intervening_prose = 0
            while j < len(lines) and scanned < 28 and intervening_prose <= 2:
                candidate = lines[j].strip()
                j += 1
                scanned += 1
                if not candidate:
                    continue
                if HEADING_RE.match(candidate) or FOOTNOTE_START_RE.match(candidate):
                    break
                if (
                    IMAGE_RE.search(candidate)
                    or CAPTION_RE.match(candidate)
                    or TABLE_RE.match(candidate)
                    or candidate.startswith(("|", "<!--", "```", "\\begin{"))
                ):
                    continue
                if candidate[0].islower() and len(re.findall(r"[A-Za-z]+", candidate)) >= 4:
                    block.append(candidate)
                    # A wrapped continuation is allowed only until it reaches
                    # an ordinary sentence terminator.
                    while not re.search(r"[.!?][\"')\]]*$", block[-1]) and j < len(lines):
                        continuation = lines[j].strip()
                        j += 1
                        if not continuation or HEADING_RE.match(continuation) or FOOTNOTE_START_RE.match(continuation):
                            break
                        if continuation[0].islower():
                            block.append(continuation)
                        else:
                            break
                    break
                intervening_prose += 1
        block_text = " ".join(s for s in block if s).strip()

        word_count = len(re.findall(r"[A-Za-z]+", block_text))
        has_url = "http" in block_text or "www." in block_text
        has_period = block_text.count(".") >= 1

        if (prev_match or has_url) and word_count >= 8 and has_period:
            foot_defs.setdefault(num, block_text)

        i = j

    return foot_defs


def extract_footnote_markers(text: str, foot_defs: dict[str, str]) -> dict[str, list[str]]:
    markers: dict[str, list[str]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        m = re.search(r"(.*?)(?:\s|\u00A0)(\d+)\s*$", stripped)
        if not m:
            continue
        num = m.group(2)
        if num not in foot_defs or num in markers:
            continue
        words = re.findall(r"[A-Za-z]+", m.group(1))
        if len(words) < 3:
            continue
        markers[num] = words[-6:]
    return markers


def in_code_fence(text: str, pos: int) -> bool:
    return text[:pos].count("```") % 2 == 1


def line_at(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if end == -1:
        end = len(text)
    return text[start:end]


def find_replacement(text: str, context_words: list[str], num: str) -> tuple[int, int] | None:
    pattern = r"\b" + r"\W+".join(map(re.escape, context_words)) + r"\b"
    fallback_locations: list[tuple[int, int]] = []
    for m in re.finditer(pattern, text, flags=re.IGNORECASE):
        pos = m.end()
        line = line_at(text, pos).lstrip()
        if line.startswith("#"):
            continue
        if in_code_fence(text, pos):
            continue
        tail = text[pos:pos + 10]
        mnum = re.search(rf"\s*{re.escape(num)}(?!\d)", tail)
        if mnum:
            start = pos + mnum.start()
            end = pos + mnum.end()
            return start, end
        while pos < len(text) and text[pos] in ".;,!?":
            pos += 1
        fallback_locations.append((pos, pos))
    # Without an explicit marker, insertion is safe only when the surrounding
    # prose appears exactly once.  Repeated context is intentionally left
    # unplaced rather than attaching a footnote to the wrong sentence.
    return fallback_locations[0] if len(fallback_locations) == 1 else None


def footnote_definition_is_canonical_prose(nougat_text: str, definition: str) -> bool:
    """Reject list items that Docling's loose footnote heuristic misclassifies."""

    definition_words = re.findall(r"[A-Za-z0-9]+", definition.lower())
    if len(definition_words) < 6:
        return False
    # Six consecutive normalized words are distinctive enough to identify a
    # duplicated numbered list item without suppressing a genuine footnote.
    prefix = definition_words[:8]
    nougat_words = re.findall(r"[A-Za-z0-9]+", nougat_text.lower())
    width = min(8, len(prefix))
    return any(nougat_words[index:index + width] == prefix[:width] for index in range(len(nougat_words) - width + 1))


def apply_footnotes(nougat_text: str, docling_text: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """Reconstruct Docling footnotes in the canonical Nougat narrative."""

    definitions = {
        number: definition
        for number, definition in extract_footnote_defs(docling_text).items()
        if not footnote_definition_is_canonical_prose(nougat_text, definition)
    }
    # A complete Nougat definition is already authoritative.  A marker alone
    # is different: it still needs the Docling definition appended below.
    definitions = {
        number: definition
        for number, definition in definitions.items()
        if not re.search(rf"(?m)^\[\^{re.escape(number)}\]:", nougat_text)
    }
    markers = extract_footnote_markers(docling_text, definitions)
    combined = nougat_text.rstrip()
    placed: dict[str, str] = {}
    edits: list[tuple[int, int, str]] = []
    for number, context in markers.items():
        body = definitions[number]
        if f"[^{number}]" in combined:
            placed[number] = body
            continue
        location = find_replacement(combined, context, number)
        if location is None:
            continue
        start, end = location
        marker = f"[^{number}]"
        if end < len(combined) and combined[end:end + 1].isalnum():
            marker += " "
        edits.append((start, end, marker))
        placed[number] = body
    combined = apply_edits(combined, edits)
    unplaced = {number: body for number, body in definitions.items() if number not in placed}
    return combined, placed, unplaced


def extract_figure_images_from_section(lines: list[str]) -> list[str]:
    images: list[str] = []
    for line in lines:
        if "\\includegraphics" in line or "\\pandocbounded" in line:
            images.append(line)
    return images


def split_caption_text(text: str) -> tuple[str, str]:
    m = re.search(r"([a-z])([A-Z])", text)
    if m:
        idx = m.start(2)
        return text[:idx].rstrip(), text[idx:].lstrip()
    return text.strip(), ""


def split_caption_with_next_line(caption: str, next_line: str) -> tuple[str, str]:
    m = re.match(r"^([A-Z][a-z]+)(\\\(|\\\[|\\$)", next_line.strip())
    if not m:
        return caption, ""
    word = m.group(1)
    remainder = next_line.strip()[len(word):].lstrip()
    return f"{caption} {word}".strip(), remainder


def postprocess_latex(latex_text: str) -> str:
    # Captioned Markdown images are now native Pandoc figures.  The old
    # postprocessor removed the Figures section and reattached images by order,
    # which was the source of duplicate/missing captions.
    return latex_text

    lines = latex_text.splitlines()

    fig_start = None
    for idx, line in enumerate(lines):
        if line.strip().startswith("\\subsection{Figures}") or line.strip().startswith("\\section{Figures}"):
            fig_start = idx
            break

    figure_images: list[str] = []
    if fig_start is not None:
        fig_end = None
        for idx in range(fig_start + 1, len(lines)):
            if lines[idx].strip().startswith("\\section{") or lines[idx].strip().startswith("\\subsection{"):
                fig_end = idx
                break
            if lines[idx].strip() == "\\end{document}":
                fig_end = idx
                break
        if fig_end is None:
            fig_end = len(lines)
        figure_images = extract_figure_images_from_section(lines[fig_start + 1:fig_end])
        lines = lines[:fig_start] + lines[fig_end:]

    out: list[str] = []
    fig_idx = 0
    i = 0
    in_verbatim = False
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("\\begin{verbatim}"):
            in_verbatim = True
            out.append(line)
            i += 1
            continue
        if in_verbatim:
            out.append(line)
            if line.strip().startswith("\\end{verbatim}"):
                in_verbatim = False
            i += 1
            continue
        fig_match = FIGURE_CAP_RE.match(line.strip())
        if fig_match and fig_idx < len(figure_images):
            caption_full = fig_match.group(1).strip()
            caption, remainder = split_caption_text(caption_full)
            image_line = figure_images[fig_idx]
            fig_idx += 1
            out.append("\\begin{figure}")
            out.append("\\centering")
            out.append(image_line)
            out.append(f"\\caption{{{caption}}}")
            out.append("\\end{figure}")
            if remainder:
                out.append(remainder)
            i += 1
            continue

        if line.strip().startswith("\\begin{table}"):
            table_lines = [line]
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("\\end{table}"):
                table_lines.append(lines[i])
                i += 1
            if i < len(lines):
                table_lines.append(lines[i])
                i += 1

            j = i
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                cap_match = TABLE_CAP_RE.match(lines[j].strip())
                if cap_match:
                    caption_full = cap_match.group(1).strip()
                    caption, remainder = split_caption_text(caption_full)
                    if not remainder and j + 1 < len(lines):
                        caption, remainder = split_caption_with_next_line(caption, lines[j + 1])
                    for idx in range(len(table_lines) - 1, -1, -1):
                        if table_lines[idx].strip().startswith("\\end{table}"):
                            table_lines.insert(idx, f"\\caption{{{caption}}}")
                            break
                    out.extend(table_lines)
                    if remainder:
                        out.append(remainder)
                    i = j + (2 if remainder else 1)
                    continue
            out.extend(table_lines)
            i = j
            continue

        out.append(line)
        i += 1

    if fig_idx < len(figure_images):
        out.append("\\section*{Figures (Unplaced)}")
        for image_line in figure_images[fig_idx:]:
            out.append("\\begin{figure}")
            out.append("\\centering")
            out.append(image_line)
            out.append("\\caption{Unplaced Figure}")
            out.append("\\end{figure}")

    return "\n".join(out) + "\n"


def mask_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text
    masked: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        if end <= cursor:
            continue
        start = max(start, cursor)
        masked.append(text[cursor:start])
        masked.append(" " * (end - start))
        cursor = end
    masked.append(text[cursor:])
    return "".join(masked)


def word_positions(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in re.finditer(r"[A-Za-z0-9]+", text)]


def build_word_map(
    nougat_text: str,
    docling_text: str,
    nougat_spans: list[tuple[int, int]],
    docling_spans: list[tuple[int, int]],
) -> tuple[list[tuple[int, int]], list[tuple[int, int]], list[tuple[int, int, int]]]:
    masked_nougat = mask_spans(nougat_text, nougat_spans)
    masked_docling = mask_spans(docling_text, docling_spans)

    nougat_positions = word_positions(masked_nougat)
    docling_positions = word_positions(masked_docling)

    nougat_words = [masked_nougat[s:e].lower() for s, e in nougat_positions]
    docling_words = [masked_docling[s:e].lower() for s, e in docling_positions]

    matcher = SequenceMatcher(None, nougat_words, docling_words, autojunk=False)
    blocks = [(b.a, b.b, b.size) for b in matcher.get_matching_blocks() if b.size > 0]
    return nougat_positions, docling_positions, blocks


def map_word_index(
    word_idx: int,
    blocks: list[tuple[int, int, int]],
    block_starts: list[int] | None = None,
) -> int | None:
    if not blocks:
        return None
    starts = block_starts if block_starts is not None else [a for a, _, _ in blocks]
    block_idx = bisect_right(starts, word_idx) - 1
    if block_idx >= 0:
        a, b, size = blocks[block_idx]
        if word_idx < a + size:
            return b + (word_idx - a)
        return b + size
    return blocks[0][1]


def spans_from_regex(text: str, regex: re.Pattern[str]) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in regex.finditer(text)]


def apply_edits(content: str, edits: list[tuple[int, int, str]]) -> str:
    """Apply non-overlapping replacements/insertions in one forward pass."""
    if not edits:
        return content
    output: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(edits, key=lambda edit: (edit[0], edit[1])):
        if start < cursor:
            continue
        output.append(content[cursor:start])
        output.append(replacement)
        cursor = end
    output.append(content[cursor:])
    return "".join(output)


def insert_tokens_by_alignment(content: str, tokens: list[Token], source_text: str) -> str:
    if not tokens:
        return content

    # Mask Docling images to avoid huge base64 words
    image_spans = spans_from_regex(content, re.compile(r"!\[[^\]]*\]\([^)]+\)"))

    # Mask structured spans in both source and target
    src_spans = spans_from_regex(source_text, FENCED_RE)
    src_spans += spans_from_regex(source_text, TRIPLE_QUOTE_RE)
    src_spans += spans_from_regex(source_text, VERBATIM_RE)
    src_spans += spans_from_regex(source_text, TABULAR_RE)
    src_spans += spans_from_regex(source_text, FORMULA_RE)

    tgt_spans = image_spans
    tgt_spans += spans_from_regex(content, FENCED_RE)
    tgt_spans += spans_from_regex(content, TRIPLE_QUOTE_RE)
    tgt_spans += spans_from_regex(content, VERBATIM_RE)
    tgt_spans += spans_from_regex(content, TABULAR_RE)
    tgt_spans += spans_from_regex(content, FORMULA_RE)

    nougat_positions, docling_positions, blocks = build_word_map(source_text, content, src_spans, tgt_spans)
    if not nougat_positions or not docling_positions:
        return content

    nougat_starts = [s for s, _ in nougat_positions]
    block_starts = [a for a, _, _ in blocks]
    edits: list[tuple[int, int, str]] = []

    table_spans = extract_markdown_tables(content, [])

    def replacement_near_table(pos: int, table_text: str) -> tuple[int, int, str] | None:
        if not table_spans:
            return None
        nearest = min(table_spans, key=lambda s: abs(s.start - pos))
        if abs(nearest.start - pos) > 400:
            return None
        return nearest.start, nearest.end, table_text

    for token in tokens:
        if token.type == "formula":
            if token.inline:
                if token.text in content or normalize_inline(token.text) in content:
                    continue
            else:
                if token.text in content or normalize_display(token.text) in content:
                    continue
        elif token.text in content:
            continue

        word_idx = bisect_right(nougat_starts, token.start) - 1
        if word_idx < 0:
            word_idx = 0

        doc_idx = map_word_index(word_idx, blocks, block_starts)
        if doc_idx is None or doc_idx >= len(docling_positions):
            continue

        insert_pos = docling_positions[doc_idx][1]
        if _overlaps(insert_pos, insert_pos + 1, tgt_spans):
            continue

        if token.type == "formula":
            if token.inline:
                insert_text = normalize_inline(token.text)
                before = content[insert_pos - 1] if insert_pos > 0 else ""
                after = content[insert_pos] if insert_pos < len(content) else ""
                if before and before.isalnum():
                    insert_text = " " + insert_text
                if after and after.isalnum():
                    insert_text = insert_text + " "
            else:
                insert_text = "\n\n" + normalize_display(token.text) + "\n\n"
        elif token.type == "latex_table":
            insert_text = "\n\n" + token.text.strip() + "\n\n"
        else:
            insert_text = "\n\n" + token.text.strip() + "\n\n"
        if token.type == "latex_table":
            replacement = replacement_near_table(insert_pos, insert_text)
            if replacement is not None:
                edits.append(replacement)
                continue
        edits.append((insert_pos, insert_pos, insert_text))

    return apply_edits(content, edits)


def find_insertion_point(content: str, left_words: list[str], right_words: list[str], start_pos: int) -> int | None:
    if not left_words or not right_words:
        return None
    left_pattern = r"\b" + r"\W+".join(map(re.escape, left_words)) + r"\b"
    right_pattern = r"\b" + r"\W+".join(map(re.escape, right_words)) + r"\b"
    left_match = re.search(left_pattern, content[start_pos:], flags=re.IGNORECASE)
    if not left_match:
        return None
    search_start = start_pos + left_match.end()
    right_match = re.search(right_pattern, content[search_start:], flags=re.IGNORECASE)
    if not right_match:
        return None
    gap = right_match.start()
    if gap > 800:
        return None
    insert_pos = search_start + right_match.start()
    between = content[search_start:insert_pos]
    if STRUCTURED_RE.search(between):
        return None
    return insert_pos


def insert_tokens_by_context(content: str, tokens: list[Token], source_text: str) -> str:
    source_matches = list(re.finditer(r"[A-Za-z0-9]+", source_text))
    source_starts = [match.start() for match in source_matches]
    source_ends = [match.end() for match in source_matches]

    def indexed_context(start: int, end: int, n: int = 6) -> tuple[list[str], list[str]]:
        left_end = bisect_right(source_ends, start)
        right_start = bisect_right(source_starts, end - 1)
        left = [match.group(0) for match in source_matches[max(0, left_end - n):left_end]]
        right = [match.group(0) for match in source_matches[right_start:right_start + n]]
        return left, right

    edits: list[tuple[int, int, str]] = []
    cursor = 0
    for token in tokens:
        if token.type == "formula":
            if token.inline:
                if token.text in content or normalize_inline(token.text) in content:
                    continue
            else:
                if token.text in content or normalize_display(token.text) in content:
                    continue
        elif token.text in content:
            continue
        left_words, right_words = indexed_context(token.start, token.end)
        insert_pos = find_insertion_point(content, left_words, right_words, cursor)
        if insert_pos is None:
            continue
        if token.type == "formula":
            if token.inline:
                insert_text = token.text
                before = content[insert_pos - 1] if insert_pos > 0 else ""
                after = content[insert_pos] if insert_pos < len(content) else ""
                if before and before.isalnum():
                    insert_text = " " + insert_text
                if after and after.isalnum():
                    insert_text = insert_text + " "
            else:
                insert_text = "\n\n" + normalize_display(token.text) + "\n\n"
        else:
            insert_text = "\n\n" + token.text.strip() + "\n\n"
        edits.append((insert_pos, insert_pos, insert_text))
        cursor = insert_pos
    return apply_edits(content, edits)


def cache_digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def nonempty_file(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def docling_assets_complete(source_path: Path) -> bool:
    """Return false when a referenced Docling image has gone missing."""

    try:
        text = source_path.read_text(encoding="utf-8")
    except OSError:
        return False
    source_root = source_path.parent.resolve()
    for match in MARKDOWN_IMAGE_TARGET_RE.finditer(text):
        target = match.group("target").strip().strip("<>")
        if target.startswith(("data:", "http://", "https://", "//")):
            continue
        if target.startswith("/") or ".staging/" in target:
            return False
        candidate = (source_root / unquote(target.split("#", 1)[0])).resolve()
        try:
            candidate.relative_to(source_root)
        except ValueError:
            return False
        if not candidate.is_file():
            return False
    return True


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def audit_interpreter_has_pdfium(interpreter: str) -> bool:
    """Check an interpreter without importing optional audit dependencies here."""

    try:
        result = subprocess.run(
            [interpreter, "-c", "import pypdfium2"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def find_audit_interpreter(nougat_cmd: str | None, manifest: dict[str, Any]) -> str | None:
    """Reuse the Nougat environment's Python for pypdfium2-backed auditing.

    The manifest stores an executable name. Resume and merge-only audits resolve
    that name from ``--nougat-cmd`` or ``PATH`` rather than a saved absolute path.
    """

    commands: list[str] = []
    if nougat_cmd:
        commands.append(nougat_cmd)
    manifest_tool = manifest.get("tools", {}).get("nougat", {}) if isinstance(manifest.get("tools"), dict) else {}
    if isinstance(manifest_tool, dict) and isinstance(manifest_tool.get("command"), str):
        commands.append(manifest_tool["command"])

    candidates: list[str] = []
    for command in commands:
        command_path = resolve_command_path(command)
        if command_path is None:
            continue
        interpreter = launcher_interpreter(str(command_path))
        if interpreter:
            candidates.append(interpreter)
        for interpreter_name in ("python", "python.exe"):
            sibling = command_path.parent / interpreter_name
            if executable_file(sibling):
                candidates.append(str(sibling))
    candidates.extend(
        candidate
        for candidate in (sys.executable, shutil.which("python3"), shutil.which("python"))
        if candidate
    )
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if audit_interpreter_has_pdfium(candidate):
            return candidate
    return None


def audit_execution_failure_report(pdf_path: Path, message: str) -> dict[str, Any]:
    try:
        input_hash = sha256_file(pdf_path)
    except OSError:
        input_hash = None
    return {
        "schema": "2",
        "status": "review_required",
        "pdf_sha256": input_hash,
        "coverage": {},
        "coverage_by_page": {},
        "findings": [],
        "limitations": [
            "OCR fidelity audit could not execute; inspect the extracted document manually.",
            message,
        ],
        "execution": {"ok": False, "error": message},
        "summary": {"detected_findings": 0, "active_findings": 0, "suppressed_findings": 0},
    }


def audit_report_summary(report: dict[str, Any]) -> dict[str, Any]:
    execution = report.get("execution")
    execution_ok = execution.get("ok", False) if isinstance(execution, dict) else False
    summary = report.get("summary")
    if not isinstance(summary, dict):
        findings = report.get("findings")
        findings_list = findings if isinstance(findings, list) else []
        summary = {
            "detected_findings": len(findings_list),
            "active_findings": sum(1 for finding in findings_list if isinstance(finding, dict) and finding.get("active", True)),
            "suppressed_findings": 0,
        }
    coverage = report.get("coverage_summary")
    if not isinstance(coverage, dict):
        by_page = report.get("coverage")
        values = by_page.values() if isinstance(by_page, dict) else []
        coverage = {status: sum(1 for value in values if value == status) for status in ("full", "partial", "unavailable")}
    return {
        "status": report.get("status", "review_required"),
        "execution_ok": execution_ok,
        "detected_findings": summary.get("detected_findings", 0),
        "active_findings": summary.get("active_findings", 0),
        "suppressed_findings": summary.get("suppressed_findings", 0),
        "coverage": coverage,
    }


def run_ocr_audit(
    pdf_path: Path,
    job_dir: Path,
    combined_md: Path,
    nougat_cmd: str | None,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Run the audit subprocess and keep audit failures nonfatal."""

    audit_json = job_dir / "ocr_audit.json"
    audit_markdown = job_dir / "ocr_audit.md"
    audit_json.unlink(missing_ok=True)
    audit_markdown.unlink(missing_ok=True)
    audit_script = Path(__file__).with_name("audit_ocr.py")
    interpreter = find_audit_interpreter(nougat_cmd, manifest)
    if interpreter is None:
        message = "No Python interpreter with pypdfium2 was found for the OCR audit."
        report = audit_execution_failure_report(pdf_path, message)
        write_json(audit_json, report)
        if not (job_dir / "ocr_audit_review.json").is_file():
            write_json(job_dir / "ocr_audit_review.json", {"schema": "1", "resolutions": {}})
        atomic_write_text(
            audit_markdown,
            "# OCR fidelity audit\n\nStatus: REVIEW REQUIRED\n\n"
            f"Audit execution: failed — {message}\n",
        )
        print(f"OCR audit warning: {message}", file=sys.stderr)
        return report

    command = [
        interpreter,
        str(audit_script),
        str(pdf_path),
        "--output-dir",
        str(job_dir),
        "--nougat-md",
        str(job_dir / "nougat.mmd"),
        "--docling-md",
        str(job_dir / "docling_decoded.md"),
        "--combined-md",
        str(combined_md),
    ]
    audit_stderr = False
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        result = None
        message = f"could not start OCR audit: {exc}"
    else:
        message = f"OCR audit exited with status {result.returncode}" if result.returncode else ""
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            audit_stderr = True
            print(result.stderr, end="", file=sys.stderr)

    report = load_manifest(audit_json)
    if not report or not audit_json.is_file():
        report = audit_execution_failure_report(pdf_path, message or "OCR audit produced no report.")
        write_json(audit_json, report)
        atomic_write_text(
            audit_markdown,
            "# OCR fidelity audit\n\nStatus: REVIEW REQUIRED\n\n"
            f"Audit execution: failed — {report['execution']['error']}\n",
        )
    execution = report.get("execution")
    if not isinstance(execution, dict) or not execution.get("ok", False):
        error = execution.get("error", message or "audit failed") if isinstance(execution, dict) else (message or "audit failed")
        if not audit_stderr:
            print(f"OCR audit warning: {error}", file=sys.stderr)
    elif message:
        print(f"OCR audit warning: {message}", file=sys.stderr)
    review_path = job_dir / "ocr_audit_review.json"
    if not review_path.is_file():
        write_json(review_path, {"schema": "1", "resolutions": {}})
    return report


def manifest_stage(manifest: dict[str, Any], stage: str) -> dict[str, Any]:
    stages = manifest.get("stages")
    if not isinstance(stages, dict):
        return {}
    record = stages.get(stage)
    return record if isinstance(record, dict) else {}


def stage_is_cached(
    manifest: dict[str, Any], stage: str, key: str, paths: list[Path]
) -> bool:
    record = manifest_stage(manifest, stage)
    return (
        isinstance(record, dict)
        and record.get("status") == "complete"
        and record.get("cache_key") == key
        and all(nonempty_file(path) for path in paths)
    )


def stage_record(
    manifest: dict[str, Any],
    stage: str,
    cache_key: str,
    duration: float,
    paths: list[Path],
    **extra: Any,
) -> None:
    stages = manifest.setdefault("stages", {})
    if not isinstance(stages, dict):
        stages = {}
        manifest["stages"] = stages
    stages[stage] = {
        "status": "complete",
        "cache_key": cache_key,
        "duration_seconds": round(duration, 3),
        "outputs": [str(path.name) for path in paths],
        "completed_at": now_utc(),
        **extra,
    }


def stage_pending(manifest: dict[str, Any], stage: str, cache_key: str) -> None:
    stages = manifest.setdefault("stages", {})
    if not isinstance(stages, dict):
        stages = {}
        manifest["stages"] = stages
    stages[stage] = {
        "status": "running",
        "cache_key": cache_key,
        "started_at": now_utc(),
    }


def invalidate_stage_outputs(job_dir: Path, stage: str) -> None:
    if stage == "nougat":
        (job_dir / "nougat.mmd").unlink(missing_ok=True)
        staging_dir = job_dir / ".staging" / "nougat"
    elif stage == "docling":
        (job_dir / "docling.md").unlink(missing_ok=True)
        (job_dir / "docling_code.json").unlink(missing_ok=True)
        assets_dir = job_dir / "docling_assets"
        if assets_dir.exists():
            shutil.rmtree(assets_dir)
        staging_dir = job_dir / ".staging" / "docling"
    else:
        raise ValueError(f"Unknown stage: {stage}")
    if staging_dir.exists():
        cleanup_staging_dir(staging_dir)

    for path in (
        job_dir / "docling_decoded.md",
        job_dir / "merge_report.json",
        job_dir / "timings.json",
        job_dir / "ocr_audit.json",
        job_dir / "ocr_audit.md",
    ):
        path.unlink(missing_ok=True)
    figures_dir = job_dir / "figures"
    if figures_dir.exists():
        shutil.rmtree(figures_dir)


def find_docling_source(job_dir: Path, input_stem: str) -> Path:
    canonical = job_dir / "docling.md"
    if nonempty_file(canonical):
        return canonical
    # Compatibility with v2 jobs produced before raw Markdown was kept in the
    # job root.  These are normalized during merge-only/resume repair.
    return find_single_output(job_dir / "docling_assets", ".md", input_stem)


def repair_cached_docling_markdown(job_dir: Path, input_stem: str) -> Path:
    """Upgrade a cached v2 Docling result without invoking either OCR engine."""

    source = find_docling_source(job_dir, input_stem)
    canonical = job_dir / "docling.md"
    normalize_docling_markdown(
        source,
        canonical,
        job_dir / "docling_assets",
        job_dir,
        allow_cached_repair=True,
    )
    return canonical


def markdown_reference_errors(text: str, base_dir: Path) -> list[str]:
    errors: list[str] = []
    if ".staging/" in text or "/.staging/" in text:
        errors.append("contains a staging path")
    for match in MARKDOWN_IMAGE_TARGET_RE.finditer(text):
        target = unquote(match.group("target").strip().strip("<>").split("#", 1)[0])
        if not target or target.startswith(("data:", "http://", "https://", "//")):
            continue
        path = Path(target)
        if path.is_absolute():
            errors.append(f"contains an absolute local image path: {target}")
            continue
        candidate = (base_dir / path).resolve()
        try:
            candidate.relative_to(base_dir.resolve())
        except ValueError:
            errors.append(f"contains an image path outside the job: {target}")
            continue
        if not candidate.is_file():
            errors.append(f"contains an unresolved image path: {target}")
    return errors


def structured_spans_for_alignment(text: str, *, include_images: bool) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for regex in (FENCED_RE, TRIPLE_QUOTE_RE, VERBATIM_RE, TABULAR_RE, FORMULA_RE):
        spans.extend(spans_from_regex(text, regex))
    spans.extend((token.start, token.end) for token in extract_markdown_tables(text, []))
    if include_images:
        spans.extend(spans_from_regex(text, IMAGE_RE))
    return spans


def paragraph_boundary_before(text: str, offset: int) -> int:
    boundary = text.rfind("\n\n", 0, offset)
    if boundary >= 0:
        return boundary + 2
    line = text.rfind("\n", 0, offset)
    return line + 1 if line >= 0 else 0


def inline_figure_edits(nougat_text: str, docling_text: str, figures: list[FigureBlock]) -> tuple[list[tuple[int, int, str]], list[FigureBlock]]:
    """Anchor figures using a single monotonic, structured-content-masked map."""

    if not figures:
        return [], []
    nougat_spans = structured_spans_for_alignment(nougat_text, include_images=False)
    docling_spans = structured_spans_for_alignment(docling_text, include_images=True)
    nougat_words, docling_words, blocks = build_word_map(
        nougat_text,
        docling_text,
        nougat_spans,
        docling_spans,
    )
    if not nougat_words or not docling_words or not blocks:
        return [], figures

    docling_starts = [start for start, _ in docling_words]
    masked_nougat = mask_spans(nougat_text, nougat_spans)
    aligned_nougat_words = [masked_nougat[start:end].lower() for start, end in nougat_words]

    def unique_context_anchor(left: tuple[int, int, int], right: tuple[int, int, int]) -> int | None:
        left_context = aligned_nougat_words[max(left[0], left[0] + left[2] - 4):left[0] + left[2]]
        right_context = aligned_nougat_words[right[0]:right[0] + min(4, right[2])]
        if len(left_context) < 3 or len(right_context) < 3:
            return None
        anchors: set[int] = set()
        width = len(left_context)
        for start in range(len(aligned_nougat_words) - width + 1):
            if aligned_nougat_words[start:start + width] != left_context:
                continue
            gap_end = min(len(aligned_nougat_words) - len(right_context) + 1, start + width + 121)
            for next_start in range(start + width, gap_end):
                if aligned_nougat_words[next_start:next_start + len(right_context)] == right_context:
                    anchors.add(paragraph_boundary_before(nougat_text, nougat_words[next_start][0]))
        return next(iter(anchors)) if len(anchors) == 1 else None
    edits: list[tuple[int, int, str]] = []
    unplaced: list[FigureBlock] = []
    last_anchor = -1
    for figure in figures:
        word_index = bisect_right(docling_starts, figure.source_offset) - 1
        if word_index < 0:
            unplaced.append(figure)
            continue
        left = next((block for block in reversed(blocks) if block[1] + block[2] <= word_index), None)
        right = next((block for block in blocks if block[1] >= word_index), None)
        # Both sides must carry enough unique surrounding prose and be close to
        # the figure.  Anything weaker is intentionally appended, never guessed.
        if (
            left is None
            or right is None
            or left[2] < 3
            or right[2] < 3
            or word_index - (left[1] + left[2]) > 80
            or right[1] - word_index > 80
            or right[0] >= len(nougat_words)
        ):
            unplaced.append(figure)
            continue
        anchor = unique_context_anchor(left, right)
        if anchor is None:
            unplaced.append(figure)
            continue
        if anchor < last_anchor:
            unplaced.append(figure)
            continue
        edits.append((anchor, anchor, figure.markdown + "\n\n"))
        last_anchor = anchor
    return edits, unplaced


FENCE_LINE_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>`{3,}|'{3})(?P<info>[^\n]*)$", re.MULTILINE)
ANCHOR_WORD_RE = re.compile(r"[A-Za-z0-9]+")
NUMBERED_CODE_START_RE = re.compile(r"^\s*(?P<number>\d+)(?=\s|$)")


@dataclass(frozen=True)
class ReflowedCode:
    text: str
    first_line: int | None
    last_line: int | None


def normalize_code_text(text: str) -> str:
    """Normalize OCR lookalikes without transliterating intentional symbols."""

    normalized = text.replace("ˆ", "^").replace("−", "-")
    normalized = re.sub(
        r"(?P<quote>['\"])\s+(?P<body>[^'\"\n]*?)\s+(?P=quote)",
        lambda match: f"{match.group('quote')}{match.group('body')}{match.group('quote')}",
        normalized,
    )
    return re.sub(r"\s+([),;])", r"\1", normalized).strip()


def underscore_identifiers(text: str) -> list[str]:
    identifiers = set(
        re.findall(r"(?<![A-Za-z])[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b", text)
    )
    return sorted(identifiers, key=lambda value: (-len(value), value))


def repair_underscores(text: str, identifiers: list[str]) -> str:
    repaired = text
    for identifier in identifiers:
        components = identifier.split("_")
        spaced = re.compile(r"\b" + r"\s+".join(re.escape(part) for part in components) + r"\b")
        matches = list(spaced.finditer(repaired))
        if len(matches) == 1:
            repaired = spaced.sub(identifier, repaired, count=1)
            continue
        for left, right in zip(components, components[1:]):
            words = set(re.findall(r"\b[A-Za-z][A-Za-z0-9]*\b", repaired))
            suffixes = [left]
            suffixes.extend(
                word for word in words if len(word) >= 3 and left.endswith(word) and word != left
            )
            for suffix in sorted(suffixes, key=len, reverse=True):
                pair = re.compile(
                    rf"(?<![A-Za-z0-9]){re.escape(suffix)}\s+{re.escape(right)}(?![A-Za-z0-9])"
                )
                if len(list(pair.finditer(repaired))) == 1:
                    repaired = pair.sub(f"{suffix}_{right}", repaired, count=1)
                    break
            else:
                approximate = re.compile(
                    rf"(?<![A-Za-z0-9])(?P<left>[A-Za-z][A-Za-z0-9]*)\s+"
                    rf"{re.escape(right)}(?![A-Za-z0-9])"
                )
                candidates = [
                    match
                    for match in approximate.finditer(repaired)
                    if len(left) >= 4
                    and len(match.group("left")) >= 4
                    and SequenceMatcher(None, left, match.group("left")).ratio() >= 0.8
                ]
                if len(candidates) == 1:
                    match = candidates[0]
                    replacement = f"{match.group('left')}_{right}"
                    repaired = repaired[:match.start()] + replacement + repaired[match.end():]
    return repaired


def split_numbered_code_item(text: str) -> list[tuple[int, str]] | None:
    first = NUMBERED_CODE_START_RE.match(text)
    if first is None:
        return None
    markers: list[tuple[int, int, int]] = [
        (int(first.group("number")), first.start("number"), first.end("number"))
    ]
    expected = markers[0][0] + 1
    cursor = first.end("number")
    while True:
        marker = re.search(rf"(?<![\w.]){expected}(?=\s|$)", text[cursor:])
        if marker is None:
            break
        start = cursor + marker.start()
        end = cursor + marker.end()
        markers.append((expected, start, end))
        expected += 1
        cursor = end
    if len(markers) < 3:
        return None

    lines: list[tuple[int, str]] = []
    for index, (number, _, marker_end) in enumerate(markers):
        content_end = markers[index + 1][1] if index + 1 < len(markers) else len(text)
        lines.append((number, text[marker_end:content_end].strip()))
    return lines


def leading_nougat_line_numbers(text: str) -> list[int]:
    """Read the contiguous printed-number skeleton at a Nougat fence's start."""

    numbers: list[int] = []
    for line in text.splitlines():
        if not line.strip() and not numbers:
            continue
        match = re.match(r"^\s*(?P<number>\d+)(?=\D|$)", line)
        if match is None:
            if numbers:
                break
            continue
        number = int(match.group("number"))
        if numbers and number != numbers[-1] + 1:
            break
        numbers.append(number)
    return numbers


def code_group_pages(group: dict[str, Any]) -> list[int]:
    pages: set[int] = set()
    blocks = group.get("blocks")
    if not isinstance(blocks, list):
        return []
    for block in blocks:
        provenance = block.get("provenance") if isinstance(block, dict) else None
        if not isinstance(provenance, list):
            continue
        for entry in provenance:
            page = entry.get("page") if isinstance(entry, dict) else None
            if isinstance(page, int):
                pages.add(page)
    return sorted(pages)


def reflow_docling_code_group(
    group: dict[str, Any], nougat_context: str
) -> tuple[ReflowedCode | None, str | None]:
    blocks = group.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return None, "Docling code group has no blocks"
    texts = [block.get("text") for block in blocks if isinstance(block, dict)]
    if len(texts) != len(blocks) or not all(isinstance(text, str) and text.strip() for text in texts):
        return None, "Docling code group contains an empty block"
    pages = code_group_pages(group)
    if any(current != previous + 1 for previous, current in zip(pages, pages[1:])):
        return None, "Docling code blocks are not on consecutive pages"

    numbered = [split_numbered_code_item(text) for text in texts]
    identifiers = underscore_identifiers(nougat_context)
    if all(lines is not None for lines in numbered):
        flattened: list[tuple[int, str]] = []
        for lines in numbered:
            assert lines is not None
            if flattened and lines[0][0] != flattened[-1][0] + 1:
                return None, "numbered Docling blocks are not consecutive"
            flattened.extend(lines)
        nougat_numbers = leading_nougat_line_numbers(nougat_context)
        docling_numbers = [number for number, _ in flattened]
        if len(nougat_numbers) < 3 or docling_numbers[:len(nougat_numbers)] != nougat_numbers:
            return None, "Nougat has no matching leading line-number sequence"
        rendered: list[str] = []
        for number, content in flattened:
            normalized = repair_underscores(normalize_code_text(content), identifiers)
            rendered.append(str(number) if not normalized else f"{number} {normalized}")
        return ReflowedCode("\n".join(rendered), flattened[0][0], flattened[-1][0]), None

    if any(lines is not None for lines in numbered):
        return None, "Docling code group mixes numbered and unnumbered blocks"
    if not all("\n" in text for text in texts):
        return None, "unnumbered Docling code is flattened"
    rendered_blocks = []
    for text in texts:
        lines = [repair_underscores(normalize_code_text(line), identifiers) for line in text.splitlines()]
        rendered_blocks.append("\n".join(lines).strip())
    return ReflowedCode("\n".join(rendered_blocks), None, None), None


def _anchor_words(text: str) -> list[str]:
    return [match.group(0).lower() for match in ANCHOR_WORD_RE.finditer(text)]


def find_unique_anchor(
    text: str,
    anchor: str,
    *,
    start: int,
    prefer_tail: bool,
) -> tuple[int, int] | None:
    anchor_words = _anchor_words(anchor)
    if len(anchor_words) < 4:
        return None
    positions = [(match.start(), match.end()) for match in ANCHOR_WORD_RE.finditer(text, start)]
    target_words = [text[left:right].lower() for left, right in positions]
    max_width = min(12, len(anchor_words))
    for width in range(max_width, 3, -1):
        starts = range(len(anchor_words) - width, -1, -1) if prefer_tail else range(len(anchor_words) - width + 1)
        for anchor_start in starts:
            needle = anchor_words[anchor_start:anchor_start + width]
            matches = [
                index
                for index in range(len(target_words) - width + 1)
                if target_words[index:index + width] == needle
            ]
            if len(matches) == 1:
                index = matches[0]
                return positions[index][0], positions[index + width - 1][1]
    return None


def count_nougat_code_blocks(text: str) -> int:
    markers = list(FENCE_LINE_RE.finditer(text))
    return (len(markers) + 1) // 2


def hybridize_code_blocks(
    nougat_text: str,
    code_groups: list[dict[str, Any]] | None,
) -> tuple[str, dict[str, Any], list[str]]:
    nougat_count = count_nougat_code_blocks(nougat_text)
    report: dict[str, Any] = {
        "source": "docling_code.json" if code_groups is not None else "unavailable",
        "total": len(code_groups) if code_groups is not None else nougat_count,
        "replaced": 0,
        "fallback": 0,
        "blocks": [],
    }
    warnings: list[str] = []
    if code_groups is None:
        report["fallback"] = nougat_count
        if nougat_count:
            reason = "docling_code.json is unavailable; retained Nougat code"
            warnings.append(reason)
            report["blocks"].append(
                {
                    "status": "fallback",
                    "pages": [],
                    "line_range": None,
                    "replacement_count": 0,
                    "fallback_reason": reason,
                }
            )
        return nougat_text, report, warnings

    edits: list[tuple[int, int, str]] = []
    cursor = 0
    for index, group in enumerate(code_groups, start=1):
        pages = code_group_pages(group)
        detail: dict[str, Any] = {"index": index, "pages": pages, "line_range": None}
        before_text = group.get("before_text")
        after_text = group.get("after_text")
        before = (
            find_unique_anchor(nougat_text, before_text, start=cursor, prefer_tail=True)
            if isinstance(before_text, str) and before_text.strip()
            else None
        )
        if before is None:
            detail.update(
                status="fallback",
                replacement_count=0,
                fallback_reason="preceding prose anchor is not unique",
            )
            report["fallback"] += 1
            report["blocks"].append(detail)
            warnings.append(f"Code block {index}: {detail['fallback_reason']}.")
            continue
        after = (
            find_unique_anchor(nougat_text, after_text, start=before[1], prefer_tail=False)
            if isinstance(after_text, str) and after_text.strip()
            else None
        )
        if after is None:
            detail.update(
                status="fallback",
                replacement_count=0,
                fallback_reason="following prose anchor is not unique",
            )
            report["fallback"] += 1
            report["blocks"].append(detail)
            warnings.append(f"Code block {index}: {detail['fallback_reason']}.")
            continue

        markers = [
            marker
            for marker in FENCE_LINE_RE.finditer(nougat_text, before[1], after[0])
        ]
        if len(markers) not in {1, 2}:
            detail.update(
                status="fallback",
                replacement_count=0,
                fallback_reason="expected one fenced Nougat code region between anchors",
            )
            report["fallback"] += 1
            report["blocks"].append(detail)
            warnings.append(f"Code block {index}: {detail['fallback_reason']}.")
            cursor = after[1]
            continue
        if len(markers) == 2 and (
            markers[0].group("marker")[0] != markers[1].group("marker")[0]
            or markers[1].group("info").strip()
        ):
            detail.update(
                status="fallback",
                replacement_count=0,
                fallback_reason="Nougat fence delimiters are ambiguous",
            )
            report["fallback"] += 1
            report["blocks"].append(detail)
            warnings.append(f"Code block {index}: {detail['fallback_reason']}.")
            cursor = after[1]
            continue

        region_end = markers[1].end() if len(markers) == 2 else after[0]
        context = nougat_text[markers[0].end():region_end]
        reflowed, reason = reflow_docling_code_group(group, context)
        if reflowed is None:
            detail.update(
                status="fallback",
                replacement_count=0,
                fallback_reason=reason or "Docling code could not be reflowed",
            )
            report["fallback"] += 1
            report["blocks"].append(detail)
            warnings.append(f"Code block {index}: {detail['fallback_reason']}.")
            cursor = after[1]
            continue

        marker = "```"
        replacement = f"{marker}\n{reflowed.text}\n{marker}\n\n"
        edits.append((markers[0].start(), region_end, replacement))
        line_range = (
            [reflowed.first_line, reflowed.last_line]
            if reflowed.first_line is not None and reflowed.last_line is not None
            else None
        )
        detail.update(
            status="replaced",
            line_range=line_range,
            replacement_count=1,
            fallback_reason=None,
        )
        report["replaced"] += 1
        report["blocks"].append(detail)
        cursor = after[1]

    return apply_edits(nougat_text, edits), report, warnings


def merge_outputs_with_report(
    nougat_text: str,
    figures_text: str,
    figure_placement: str,
    normalize_math: bool,
    code_groups: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Merge Docling annotations and confident code into the Nougat body."""

    figures = extract_captioned_figures(figures_text)
    code_text, code_report, code_warnings = hybridize_code_blocks(nougat_text, code_groups)
    combined_text, placed_footnotes, unplaced_footnotes = apply_footnotes(code_text, figures_text)
    warnings: list[str] = list(code_warnings)
    if figure_placement == "inline":
        edits, unplaced_figures = inline_figure_edits(combined_text, figures_text, figures)
        combined_text = apply_edits(combined_text, edits)
        placed_figures = len(figures) - len(unplaced_figures)
        if unplaced_figures:
            warnings.append(f"{len(unplaced_figures)} figure(s) had ambiguous inline anchors and were appended.")
    else:
        # Appended placement is the selected, successful placement policy; it
        # is not a low-confidence inline fallback.
        unplaced_figures = []
        placed_figures = len(figures)

    figures_to_append = unplaced_figures if figure_placement == "inline" else figures
    if figures_to_append:
        heading = "Figures" if figure_placement == "appended" else "Unplaced Figures"
        combined_text = combined_text.rstrip() + f"\n\n## {heading}\n\n" + "\n\n".join(
            figure.markdown for figure in figures_to_append
        )
    if placed_footnotes:
        definitions = [
            f"[^{number}]: {body}"
            for number, body in sorted(placed_footnotes.items(), key=lambda item: int(item[0]))
        ]
        combined_text = combined_text.rstrip() + "\n\n" + "\n\n".join(definitions)
    if unplaced_footnotes:
        warnings.append(f"{len(unplaced_footnotes)} footnote(s) could not be anchored in Nougat text.")
        definitions = [
            f"[^{number}]: {body}"
            for number, body in sorted(unplaced_footnotes.items(), key=lambda item: int(item[0]))
        ]
        combined_text = combined_text.rstrip() + "\n\n## Footnotes (Unplaced)\n\n" + "\n\n".join(definitions)
    if normalize_math:
        combined_text = normalize_bracket_math(combined_text)
        combined_text = normalize_inline_math(combined_text)
    report = {
        "schema": MERGE_SCHEMA_VERSION,
        "figure_placement": figure_placement,
        "code_blocks": code_report,
        "figures": {"total": len(figures), "placed": placed_figures, "unplaced": len(unplaced_figures)},
        "footnotes": {"placed": len(placed_footnotes), "unplaced": len(unplaced_footnotes)},
        "warnings": warnings,
    }
    return combined_text, report


def merge_outputs(
    nougat_text: str,
    figures_text: str,
    figure_placement: str,
    normalize_math: bool,
) -> str:
    return merge_outputs_with_report(nougat_text, figures_text, figure_placement, normalize_math)[0]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract Markdown from a single PDF using Nougat-OCR + Docling and merge outputs."
    )
    parser.add_argument("pdf", type=Path, help="Path to a single PDF")
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILE_DEFAULTS),
        required=True,
        help="Required extraction profile",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"), help="Root output directory")
    parser.add_argument("--nougat-cmd", help="Path to nougat executable")
    parser.add_argument("--docling-cmd", help="Path to docling executable")
    parser.add_argument(
        "--nougat-arg",
        action="append",
        default=[],
        help="One additional Nougat argument; repeat for multiple arguments.",
    )
    parser.add_argument(
        "--docling-arg",
        action="append",
        default=[],
        help="One additional Docling argument; repeat for multiple arguments.",
    )
    parser.add_argument("--resume", action="store_true", help="Reuse valid cached OCR/Docling stages.")
    parser.add_argument("--force", action="store_true", help="Recompute external stages even when cached.")
    parser.add_argument(
        "--parallel-stages",
        action="store_true",
        help="Run Nougat and Docling concurrently; use only when resources allow.",
    )
    parser.add_argument(
        "--figure-placement",
        choices=("appended", "inline"),
        default="appended",
        help="Place figures at the end or align them into the source document.",
    )
    parser.add_argument(
        "--inline-figures",
        action="store_const",
        const="inline",
        dest="figure_placement",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--image-mode",
        choices=("referenced", "embedded"),
        default="referenced",
        help="Docling image export mode; referenced avoids a base64 round trip.",
    )
    parser.add_argument(
        "--keep-base64",
        action="store_true",
        help="Keep embedded Docling images in docling.md instead of decoding them.",
    )
    parser.add_argument("--require-complete", action="store_true", help="Exit 2 after writing reports if completeness is incomplete or unknown.")
    parser.add_argument("--recovery-ledger", type=Path, help="Verified, hash-bound replacements for the merged body.")
    parser.add_argument("--merge-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--skip-ocr-audit",
        action="store_true",
        help="Skip the post-extraction OCR fidelity audit.",
    )
    parser.add_argument("--normalize-math", action="store_true", help="Normalize bracketed math delimiters.")
    parser.add_argument("--to-latex", action="store_true", help="Generate LaTeX via Pandoc.")
    parser.add_argument("--title", help="Title metadata for Pandoc/YAML front matter.")
    parser.add_argument("--author", help="Author metadata for Pandoc/YAML front matter.")
    parser.add_argument("--date", help="Date metadata for Pandoc/YAML front matter.")
    parser.add_argument("--pandoc-cmd", help="Path to pandoc executable")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.keep_base64:
        args.image_mode = "embedded"
    if args.resume and args.force:
        print("--resume and --force cannot be used together.", file=sys.stderr)
        return 2

    pdf_path = args.pdf.expanduser().resolve()
    if not pdf_path.is_file():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 1

    out_root = args.output_dir.expanduser().resolve()
    input_hash = sha256_file(pdf_path)
    artifact_stem = safe_stem(pdf_path)
    job_dir = out_root / f"{artifact_stem}-{input_hash[:12]}"
    job_dir.mkdir(parents=True, exist_ok=True)

    nougat_md = job_dir / "nougat.mmd"
    docling_md = job_dir / "docling.md"
    docling_code = job_dir / "docling_code.json"
    combined_md = job_dir / f"{artifact_stem}.md"
    combined_for_tex = job_dir / f"{artifact_stem}_for_tex.md"
    combined_tex = job_dir / f"{artifact_stem}.tex"
    manifest_path = job_dir / "manifest.json"
    timings_path = job_dir / "timings.json"
    manifest = load_manifest(manifest_path)

    nougat_args = apply_profile_args(args.profile, args.nougat_arg, "nougat_args")
    docling_args = apply_profile_args(args.profile, args.docling_arg, "docling_args")

    nougat_cmd = None
    docling_cmd = None
    audit_nougat_cmd: str | None = None
    tools: dict[str, dict[str, str]] = {}
    if not args.merge_only:
        nougat_cmd = require_command("nougat", args.nougat_cmd)
        docling_cmd = require_command("docling", args.docling_cmd)
        audit_nougat_cmd = nougat_cmd
        tools = {
            "nougat": {"command": nougat_cmd, "version": command_version(nougat_cmd, "nougat-ocr")},
            "docling": {"command": docling_cmd, "version": command_version(docling_cmd, "docling")},
        }
    elif args.nougat_cmd:
        resolved_nougat = resolve_command_path(args.nougat_cmd)
        if resolved_nougat is not None:
            audit_nougat_cmd = str(resolved_nougat)

    cache_common = {
        "schema": V2_VERSION,
        "input_sha256": input_hash,
        "profile": args.profile,
    }
    nougat_config = {
        **cache_common,
        "nougat_args": nougat_args,
        "tool": tools.get("nougat", {}),
    }
    docling_config = {
        **cache_common,
        "docling_args": docling_args,
        "image_mode": args.image_mode,
        "exports": ["md", "json"],
        "code_sidecar_schema": DOCLING_CODE_SCHEMA_VERSION,
        "tool": tools.get("docling", {}),
    }
    cache_config = {"nougat": nougat_config, "docling": docling_config}
    nougat_key = cache_digest(nougat_config)
    docling_key = cache_digest(docling_config)
    nougat_cached = bool(args.resume) and stage_is_cached(manifest, "nougat", nougat_key, [nougat_md])
    docling_source_candidate: Path | None = None
    if (args.resume or args.merge_only) and ((job_dir / "docling.md").exists() or (job_dir / "docling_assets").exists()):
        try:
            docling_source_candidate = repair_cached_docling_markdown(job_dir, pdf_path.stem)
        except (RuntimeError, ValueError, OSError):
            docling_source_candidate = None
    docling_cached = bool(args.resume) and docling_source_candidate is not None and stage_is_cached(
        manifest,
        "docling",
        docling_key,
        [docling_md, docling_source_candidate, docling_code],
    ) and docling_assets_complete(docling_source_candidate)
    stage_timings: dict[str, dict[str, Any]] = {}
    actual_image_mode = args.image_mode
    docling_source: Path | None = None

    if args.merge_only:
        if not nonempty_file(nougat_md) or not nonempty_file(docling_md):
            print(f"Missing v2 stage outputs in {job_dir}; run extraction first.", file=sys.stderr)
            return 1
        if not (job_dir / "docling_assets").exists() and args.image_mode == "referenced":
            print(f"Missing Docling asset directory in {job_dir}.", file=sys.stderr)
            return 1
        nougat_cached = docling_cached = True
    else:
        assert nougat_cmd is not None and docling_cmd is not None
        manifest = {
            **manifest,
            "schema": V2_VERSION,
            "merge_schema": MERGE_SCHEMA_VERSION,
            "input": {"path": str(pdf_path), "sha256": input_hash},
            "profile": args.profile,
            "config": cache_config,
            "tools": tools,
            "runtime": {
                "python": sys.executable,
                "python_version": platform.python_version(),
                "platform": platform.platform(),
                "machine": platform.machine(),
            },
            "updated_at": now_utc(),
        }
        if args.force:
            nougat_cached = docling_cached = False

        if not nougat_cached or not docling_cached:
            # Do not leave a previously merged artifact looking current when
            # either source stage is about to be replaced (or fails midway).
            for path in (combined_md, combined_for_tex, combined_tex):
                path.unlink(missing_ok=True)

        for stage, cached, cache_key in (
            ("nougat", nougat_cached, nougat_key),
            ("docling", docling_cached, docling_key),
        ):
            if not cached:
                invalidate_stage_outputs(job_dir, stage)
                stage_pending(manifest, stage, cache_key)
        write_manifest(manifest_path, manifest)

        def run_stage(name: str, callback: Any) -> Any:
            started = time.perf_counter()
            result = callback()
            stage_timings[name] = {"seconds": round(time.perf_counter() - started, 3), "cached": False}
            return result

        if args.parallel_stages and not nougat_cached and not docling_cached:
            print("Running Nougat-OCR and Docling concurrently...", file=sys.stderr)
            with ThreadPoolExecutor(max_workers=2) as executor:
                nougat_future = executor.submit(
                    run_stage,
                    "nougat",
                    lambda: run_nougat(nougat_cmd, pdf_path, nougat_md, job_dir, nougat_args),
                )
                docling_future = executor.submit(
                    run_stage,
                    "docling",
                    lambda: run_docling(
                        docling_cmd,
                        pdf_path,
                        docling_md,
                        job_dir,
                        docling_args,
                        args.image_mode,
                    ),
                )
                nougat_future.result()
                docling_source, actual_image_mode = docling_future.result()
        else:
            if nougat_cached:
                print("Reusing cached Nougat output.", file=sys.stderr)
                stage_timings["nougat"] = {"seconds": 0.0, "cached": True}
            else:
                print("Running Nougat-OCR...", file=sys.stderr)
                run_stage(
                    "nougat",
                    lambda: run_nougat(nougat_cmd, pdf_path, nougat_md, job_dir, nougat_args),
                )
            if docling_cached:
                print("Reusing cached Docling output.", file=sys.stderr)
                docling_source = docling_source_candidate
                actual_image_mode = manifest_stage(manifest, "docling").get(
                    "image_mode", args.image_mode
                )
                stage_timings["docling"] = {"seconds": 0.0, "cached": True}
            else:
                print("Running Docling...", file=sys.stderr)
                docling_source, actual_image_mode = run_stage(
                    "docling",
                    lambda: run_docling(
                        docling_cmd,
                        pdf_path,
                        docling_md,
                        job_dir,
                        docling_args,
                        args.image_mode,
                    ),
                )

        if nougat_cached:
            stage_timings.setdefault("nougat", {"seconds": 0.0, "cached": True})
        else:
            stage_record(
                manifest,
                "nougat",
                nougat_key,
                stage_timings["nougat"]["seconds"],
                [nougat_md],
            )
        if docling_cached:
            stage_timings.setdefault("docling", {"seconds": 0.0, "cached": True})
        else:
            assert docling_source is not None
            stage_record(
                manifest,
                "docling",
                docling_key,
                stage_timings["docling"]["seconds"],
                [docling_md, docling_code],
                image_mode=actual_image_mode,
            )
        manifest["updated_at"] = now_utc()
        write_manifest(manifest_path, manifest)
    if args.merge_only:
        actual_image_mode = manifest_stage(manifest, "docling").get(
            "image_mode", args.image_mode
        )

    decode_duration = 0.0
    figures_dir = job_dir / "figures"
    figures_decoded = job_dir / "docling_decoded.md"
    source_path = docling_source or find_docling_source(job_dir, pdf_path.stem)
    decode_started = time.perf_counter()
    reset_directory(figures_dir)
    decode_docling_markdown(source_path, figures_decoded, figures_dir)
    base_path = figures_decoded
    decode_duration = time.perf_counter() - decode_started

    merge_started = time.perf_counter()
    nougat_text = nougat_md.read_text(encoding="utf-8")
    figures_text = base_path.read_text(encoding="utf-8")
    code_groups = load_docling_code_groups(docling_code)
    combined_text, merge_report = merge_outputs_with_report(
        nougat_text,
        figures_text,
        args.figure_placement,
        args.normalize_math,
        code_groups,
    )

    metadata_block = build_yaml_metadata(args.title, args.author, args.date)
    if metadata_block:
        combined_text = metadata_block + combined_text.lstrip()
    combined_text = combined_text.rstrip() + "\n"
    recovery_base = combined_text
    recovery_ledger = load_manifest(args.recovery_ledger) if args.recovery_ledger else None
    if args.recovery_ledger and not recovery_ledger:
        raise ValueError("Recovery ledger is missing or invalid")
    combined_text, recoveries = apply_recoveries(combined_text, sha256_file(pdf_path), recovery_ledger)
    page_map = fallback(combined_text, sha256_file(pdf_path), "No PDF text-layer interpreter available")
    interpreter = find_audit_interpreter(audit_nougat_cmd, manifest)
    page_map_path = job_dir / "page_map.json"
    with tempfile.TemporaryDirectory(prefix="pdf-md-recovery-") as staging:
        candidate_md = Path(staging) / "candidate.md"
        candidate_map = Path(staging) / "page_map.json"
        candidate_md.write_text(combined_text, encoding="utf-8")
        if interpreter:
            try:
                result = subprocess.run([interpreter, str(Path(__file__).with_name("completeness.py")),
                                         str(pdf_path), str(candidate_md), str(candidate_map)],
                                        check=False, capture_output=True, text=True)
                candidate = load_manifest(candidate_map) if result.returncode == 0 else {}
                if candidate.get("status") in {"complete", "incomplete", "unknown"} and isinstance(candidate.get("pages"), list):
                    page_map = candidate
                else:
                    page_map["error"] = "Page reconciliation produced no valid report"
            except OSError as exc:
                page_map["error"] = f"Page reconciliation could not execute: {exc}"
    if any(r["page"] not in {p["page"] for p in page_map["pages"]} for r in recoveries):
        raise ValueError("Recovery names a page outside the source PDF or page membership cannot be verified")
    if recoveries:
        recovery_errors = markdown_reference_errors(combined_text, job_dir)
        if recovery_errors:
            raise ValueError("Recovery failed durable-asset validation: " + "; ".join(recovery_errors))
    (job_dir / "recovery").mkdir(exist_ok=True)
    atomic_write_text(job_dir / "recovery" / "base.md", recovery_base)
    page_map["recoveries"] = recoveries
    for page in page_map["pages"]:
        page["recovery"] = [r for r in recoveries if r["page"] == page["page"]]
    if page_map["status"] != "complete":
        warning = WARNING.format(status=page_map["status"])
        # Keep YAML front matter first; insert the warning after metadata.
        offset = len(metadata_block) if metadata_block else 0
        combined_text = combined_text[:offset] + warning + combined_text[offset:]
        for page in page_map["pages"]:
            for span in page["final_spans"]:
                span[:] = [value + len(warning) if value >= offset else value for value in span]
        seen_markers = set()
        for marker in page_map["missing_markers"] + [m for page in page_map["pages"] for m in page["missing_markers"]]:
            if id(marker) in seen_markers:
                continue
            seen_markers.add(id(marker))
            marker["span"] = [value + len(warning) if value >= offset else value for value in marker["span"]]
        for diagnostic in page_map["numbering_diagnostics"]:
            if "span" in diagnostic:
                diagnostic["span"] = [value + len(warning) if value >= offset else value for value in diagnostic["span"]]
        merge_report["warnings"].append("Content completeness is " + page_map["status"] + "; see page_map.json")
    atomic_write_text(combined_md, combined_text)
    page_map["final_md_sha256"] = digest(combined_text)
    write_json(page_map_path, page_map)
    merge_report["completeness"] = {"status": page_map["status"], "page_map": "page_map.json",
                                    "missing_pages": sorted({m["page"] for m in page_map["missing_markers"] if m["page"] is not None}),
                                    "unattributed_missing_markers": sum(m["page"] is None for m in page_map["missing_markers"])}
    merge_report["numbering_diagnostics"] = page_map["numbering_diagnostics"]
    merge_report["execution"] = {"ok": True}
    artifact_paths = [docling_md, combined_md]
    decoded_path = job_dir / "docling_decoded.md"
    if decoded_path.exists():
        artifact_paths.append(decoded_path)
    validation_errors: dict[str, list[str]] = {
        path.name: markdown_reference_errors(path.read_text(encoding="utf-8"), job_dir)
        for path in artifact_paths
    }
    merge_report["validation"] = {
        "ok": not any(validation_errors.values()),
        "artifacts": validation_errors,
    }
    write_json(job_dir / "merge_report.json", merge_report)
    if not merge_report["validation"]["ok"]:
        merge_report["status"] = "review_required"
        merge_report["execution"] = {"ok": False, "stage": "structural_validation"}
        write_json(job_dir / "merge_report.json", merge_report)
        scrub_report_files(job_dir, out_root, pdf_path)
        details = "; ".join(
            f"{name}: {', '.join(errors)}" for name, errors in validation_errors.items() if errors
        )
        raise RuntimeError(f"Merged Markdown failed durable-asset validation: {details}")

    # Keep merge and audit timings additive: the audit is a separate, optional
    # post-validation stage and should not be counted twice in timings.json.
    merge_duration = time.perf_counter() - merge_started
    audit_duration = 0.0
    if args.skip_ocr_audit:
        (job_dir / "ocr_audit.json").unlink(missing_ok=True)
        (job_dir / "ocr_audit.md").unlink(missing_ok=True)
        merge_report["ocr_audit"] = {
            "status": "skipped",
            "execution_ok": None,
            "detected_findings": 0,
            "active_findings": 0,
            "suppressed_findings": 0,
            "coverage": {},
        }
        print("Skipping OCR fidelity audit (--skip-ocr-audit).", file=sys.stderr)
    else:
        audit_started = time.perf_counter()
        audit_report = run_ocr_audit(pdf_path, job_dir, combined_md, audit_nougat_cmd, manifest)
        audit_duration = time.perf_counter() - audit_started
        merge_report["ocr_audit"] = audit_report_summary(audit_report)
    write_json(job_dir / "merge_report.json", merge_report)
    merge_report["fidelity"] = {"status": merge_report["ocr_audit"]["status"],
                                "coverage": merge_report["ocr_audit"].get("coverage", {}),
                                "scope": page_map["coverage_scope"]}
    merge_report["status"] = "clear" if (page_map["status"] == "complete"
        and merge_report["ocr_audit"]["status"] == "clear"
        and not page_map["numbering_diagnostics"]) else "review_required"
    write_json(job_dir / "merge_report.json", merge_report)
    manifest["merge_schema"] = MERGE_SCHEMA_VERSION
    manifest["updated_at"] = now_utc()
    write_manifest(manifest_path, manifest)

    timings = load_manifest(timings_path)
    if not isinstance(timings, dict):
        timings = {}
    timings.update(
        {
            "updated_at": now_utc(),
            "stages": stage_timings or timings.get("stages", {}),
            "merge_seconds": round(merge_duration, 3),
            "figure_decode_seconds": round(decode_duration, 3),
            "ocr_audit_seconds": round(audit_duration, 3),
            "figure_placement": args.figure_placement,
            "image_mode": actual_image_mode,
        }
    )
    write_json(timings_path, timings)

    if args.to_latex:
        cleaned = fix_fences(combined_text)
        latex_input_errors = validate_latex_input(cleaned, job_dir)
        if latex_input_errors:
            raise RuntimeError("Cannot generate pdfLaTeX input: " + "; ".join(latex_input_errors))
        atomic_write_text(combined_for_tex, cleaned)
        pandoc_cmd = require_command("pandoc", args.pandoc_cmd)
        pandoc_command = [
            pandoc_cmd,
            combined_for_tex.name,
            "-s",
            "--from",
            "markdown+tex_math_dollars+tex_math_single_backslash",
            f"--resource-path=.{os.pathsep}figures{os.pathsep}docling_assets",
        ]
        for declaration in pdftex_header_includes(cleaned):
            pandoc_command.extend(["--variable", f"header-includes={declaration}"])
        pandoc_command.extend(["-o", combined_tex.name])
        subprocess.run(
            pandoc_command,
            check=True,
            cwd=job_dir,
        )
        latex_text = combined_tex.read_text(encoding="utf-8", errors="replace")
        combined_tex.write_text(postprocess_latex(latex_text), encoding="utf-8")
    else:
        combined_for_tex.unlink(missing_ok=True)
        combined_tex.unlink(missing_ok=True)

    active_outputs = {combined_md.resolve(), combined_for_tex.resolve(), combined_tex.resolve()}
    for legacy_name in ("combined.md", "combined_for_tex.md", "combined.tex"):
        legacy = job_dir / legacy_name
        if legacy.resolve() not in active_outputs:
            legacy.unlink(missing_ok=True)
    scrub_report_files(job_dir, out_root, pdf_path)
    print(f"Wrote merged Markdown to {combined_md} (completeness: {page_map['status']})")
    return 2 if args.require_complete and page_map["status"] != "complete" else 0


if __name__ == "__main__":
    raise SystemExit(main())
