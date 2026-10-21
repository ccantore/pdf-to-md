#!/usr/bin/env python3
"""Audit extracted Markdown against the PDF text layer.

The audit is deliberately conservative.  It reports small substitutions only
when exact context agrees on both sides of the candidate; it never edits an
OCR artifact.  The runner invokes this file with the Nougat interpreter so
that the already-installed ``pypdfium2`` dependency can be reused without
adding a dependency to the lightweight merge environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from completeness import reconcile, digest
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Sequence

try:  # pypdfium2 lives in the isolated Nougat environment.
    import pypdfium2 as pdfium  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised through the runner fallback.
    pdfium = None  # type: ignore[assignment]


AUDIT_SCHEMA_VERSION = "2"
REVIEW_SCHEMA_VERSION = "1"
RESOLUTION_STATUSES = (
    "confirmed_extraction_error",
    "source_matches",
    "false_positive",
)

BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class MappedChar:
    char: str
    start: int
    end: int


@dataclass(frozen=True)
class NormalizedText:
    text: str
    spans: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class CharPosition:
    left: float
    bottom: float
    right: float
    top: float


@dataclass
class Token:
    value: str
    start: int
    end: int
    kind: str = "word"
    bbox: BBox | None = None


@dataclass
class PdfPage:
    number: int
    text: str
    chars: list[CharPosition | None]
    size: tuple[float, float] | None
    tokens: list[Token]
    ignored_ranges: list[tuple[int, int]]
    extraction_error: str | None = None


@dataclass(frozen=True)
class PageAnchor:
    page_token_index: int
    nougat_token_index: int
    width: int


@dataclass(frozen=True)
class PageAlignment:
    page_number: int
    anchor: PageAnchor
    nougat_start: int
    nougat_end: int


@dataclass(frozen=True)
class DoclingEvidence:
    origin: str
    text: str
    page_hint: int | None = None


LIGATURES = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "ft",
    "ﬆ": "st",
}

GREEK_UNICODE = {
    "α": "alpha",
    "Α": "alpha",
    "β": "beta",
    "Β": "beta",
    "γ": "gamma",
    "Γ": "gamma",
    "δ": "delta",
    "Δ": "delta",
    "ε": "epsilon",
    "Ε": "epsilon",
    "ϵ": "epsilon",
    "϶": "epsilon",
    "ζ": "zeta",
    "Ζ": "zeta",
    "η": "eta",
    "Η": "eta",
    "θ": "theta",
    "Θ": "theta",
    "ϑ": "theta",
    "ι": "iota",
    "Ι": "iota",
    "κ": "kappa",
    "Κ": "kappa",
    "λ": "lambda",
    "Λ": "lambda",
    "μ": "mu",
    "Μ": "mu",
    "ν": "nu",
    "Ν": "nu",
    "ξ": "xi",
    "Ξ": "xi",
    "ο": "omicron",
    "Ο": "omicron",
    "π": "pi",
    "Π": "pi",
    "ϖ": "pi",
    "ρ": "rho",
    "Ρ": "rho",
    "ϱ": "rho",
    "σ": "sigma",
    "Σ": "sigma",
    "ς": "sigma",
    "τ": "tau",
    "Τ": "tau",
    "υ": "upsilon",
    "Υ": "upsilon",
    "φ": "phi",
    "Φ": "phi",
    "ϕ": "phi",
    "χ": "chi",
    "Χ": "chi",
    "ψ": "psi",
    "Ψ": "psi",
    "ω": "omega",
    "Ω": "omega",
}

GREEK_LATEX = {
    "alpha": "alpha",
    "beta": "beta",
    "gamma": "gamma",
    "delta": "delta",
    "epsilon": "epsilon",
    "varepsilon": "epsilon",
    "zeta": "zeta",
    "eta": "eta",
    "theta": "theta",
    "vartheta": "theta",
    "iota": "iota",
    "kappa": "kappa",
    "lambda": "lambda",
    "mu": "mu",
    "nu": "nu",
    "xi": "xi",
    "omicron": "omicron",
    "pi": "pi",
    "varpi": "pi",
    "rho": "rho",
    "varrho": "rho",
    "sigma": "sigma",
    "varsigma": "sigma",
    "tau": "tau",
    "upsilon": "upsilon",
    "phi": "phi",
    "varphi": "phi",
    "chi": "chi",
    "psi": "psi",
    "omega": "omega",
}

GREEK_NAMES = set(GREEK_LATEX.values())

SUPERSCRIPT_MAP = {
    "⁰": "0",
    "¹": "1",
    "²": "2",
    "³": "3",
    "⁴": "4",
    "⁵": "5",
    "⁶": "6",
    "⁷": "7",
    "⁸": "8",
    "⁹": "9",
    "⁺": "+",
    "⁻": "-",
    "⁼": "=",
    "⁽": "(",
    "⁾": ")",
    "ⁿ": "n",
    "ⁱ": "i",
    "ᵃ": "a",
    "ᵇ": "b",
    "ᶜ": "c",
    "ᵈ": "d",
    "ᵉ": "e",
    "ᶠ": "f",
    "ᵍ": "g",
    "ʰ": "h",
    "ʲ": "j",
    "ᵏ": "k",
    "ˡ": "l",
    "ᵐ": "m",
    "ⁿ": "n",
    "ᵒ": "o",
    "ᵖ": "p",
    "ʳ": "r",
    "ˢ": "s",
    "ᵗ": "t",
    "ᵘ": "u",
    "ᵛ": "v",
    "ʷ": "w",
    "ˣ": "x",
    "ʸ": "y",
    "ᶻ": "z",
}

SUBSCRIPT_MAP = {
    "₀": "0",
    "₁": "1",
    "₂": "2",
    "₃": "3",
    "₄": "4",
    "₅": "5",
    "₆": "6",
    "₇": "7",
    "₈": "8",
    "₉": "9",
    "₊": "+",
    "₋": "-",
    "₌": "=",
    "₍": "(",
    "₎": ")",
    "ₐ": "a",
    "ₑ": "e",
    "ₕ": "h",
    "ᵢ": "i",
    "ⱼ": "j",
    "ₖ": "k",
    "ₗ": "l",
    "ₘ": "m",
    "ₙ": "n",
    "ₒ": "o",
    "ₚ": "p",
    "ᵣ": "r",
    "ₛ": "s",
    "ₜ": "t",
    "ᵤ": "u",
    "ᵥ": "v",
    "ₓ": "x",
}

MINUS_CHARS = "−﹣－‒–—"

LATEX_COMMAND_RE = re.compile(r"\\([A-Za-z]+|[,;:!])")
LATEX_ENV_RE = re.compile(r"\\(?:begin|end)\{[^}\n]+\}")
LATEX_DELIMITER_RE = re.compile(r"\\(?:\(|\)|\[|\])")
LATEX_BREAK_RE = re.compile(r"\\\\+")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
MARKDOWN_EMPHASIS_RE = re.compile(r"\*\*(?P<body>[^*\n]+)\*\*|(?<!\w)_(?P<body2>[^_\n]+)_(?!\w)")
LINE_BREAK_HYPHEN_RE = re.compile(r"([A-Za-z])[-\u2010\u2011\u00ad][ \t]*\r?\n[ \t]*([A-Za-z])")
WHITESPACE_RE = re.compile(r"\s+")
BRACE_RE = re.compile(r"[{}]")
SUB_SUP_RE = re.compile(r"(?P<marker>[_^])")

# Semantic tokens intentionally omit ordinary Markdown punctuation.  Operators
# and sub/sup markers remain because they are useful evidence in equations.
SEMANTIC_TOKEN_RE = re.compile(r"[A-Za-z]+|\d+(?:[.,]\d+)?|[+\-=<>*/%]")
NUMBER_RE = re.compile(r"^\d+(?:[.,]\d+)?$")
OPERATOR_VALUES = {"+", "-", "=", "<", ">", "*", "/", "%"}
MATH_MARKER_VALUES = {"sqrt", "frac", "alpha", "beta", "gamma", "delta", "epsilon", "theta", "lambda", "mu", "pi", "rho", "sigma", "phi", "omega", "xi", "eta", "tau", "psi", "chi", "nu", "iota", "kappa", "zeta", "upsilon", "omicron"}
SHORT_MATH_IDENTIFIERS = {"ct", "et", "kt", "nt", "ut", "yt"}

MATH_SPAN_RE = re.compile(
    r"\$\$.*?\$\$|\\\[.*?\\\]|\\\(.*?\\\)|"
    r"\\begin\{[^}]+\}.*?\\end\{[^}]+\}|(?<!\$)\$[^\n]+?(?<!\$)\$",
    re.DOTALL,
)
FIGURE_CAPTION_RE = re.compile(r"^(?:fig(?:ure)?\.?\s*)?(?P<number>\d+)\s*[:.\-–]\s*(?P<body>.+)$", re.IGNORECASE)
FIGURE_LINE_RE = re.compile(r"^fig(?:ure)?\.?\s*(?P<number>\d+)\s*[:.\-–]\s*(?P<body>.+)$", re.IGNORECASE)
FOOTNOTE_DEF_RE = re.compile(r"^\[\^(?P<number>\d+)\]:\s*(?P<body>.+)$")
NUMBERED_FOOTNOTE_RE = re.compile(r"^(?P<number>\d+)[).]\s]+(?P<body>.+)$")
PAGE_MARKER_RE = re.compile(
    r"(?:<!--\s*(?:page(?:\s+number)?|page-number)\s*[:=]?\s*(\d+)\s*-->|"
    r"\[\s*page\s+(\d+)\s*\])",
    re.IGNORECASE,
)
CODE_LIKE_LINE_RE = re.compile(
    r"^(?:\d+\s*)?(?:var\b|varexo\b|parameters?\b|model\s*;|end\s*;|"
    r"steady\s*;|set[_ ]param|load[_ ]param|stoch\s+simul|stock[_ ]simul|%\s*\(?\d+)",
    re.IGNORECASE,
)

ANCHOR_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "were",
    "with",
}


class AuditUnavailable(RuntimeError):
    """Raised when the PDF text-layer reader cannot be used."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _replacement_span(chars: Sequence[MappedChar], start: int, end: int) -> tuple[int, int]:
    if start >= end:
        return (0, 0)
    relevant = chars[start:end]
    return min(item.start for item in relevant), max(item.end for item in relevant)


def _replace_mapped(
    chars: Sequence[MappedChar],
    pattern: re.Pattern[str],
    replacement: str | Callable[[re.Match[str]], str],
) -> list[MappedChar]:
    source = "".join(item.char for item in chars)
    out: list[MappedChar] = []
    cursor = 0
    for match in pattern.finditer(source):
        out.extend(chars[cursor:match.start()])
        span_start, span_end = _replacement_span(chars, match.start(), match.end())
        value = replacement(match) if callable(replacement) else replacement
        out.extend(MappedChar(char, span_start, span_end) for char in value)
        cursor = match.end()
    out.extend(chars[cursor:])
    return out


def _expand_direct_chars(text: str) -> list[MappedChar]:
    chars: list[MappedChar] = []
    previous_script: str | None = None
    for index, char in enumerate(text):
        if char in LIGATURES:
            replacement = LIGATURES[char]
            previous_script = None
        elif char in GREEK_UNICODE:
            # Keep a Unicode Greek glyph separate from adjacent flattened
            # identifiers (PDFium may return ``σz`` for a typeset σ_z).
            replacement = f" {GREEK_UNICODE[char]} "
            previous_script = None
        elif char in SUPERSCRIPT_MAP:
            replacement = ("^" if previous_script != "sup" else "") + SUPERSCRIPT_MAP[char]
            previous_script = "sup"
        elif char in SUBSCRIPT_MAP:
            replacement = ("_" if previous_script != "sub" else "") + SUBSCRIPT_MAP[char]
            previous_script = "sub"
        elif char in MINUS_CHARS:
            replacement = "-"
            previous_script = None
        elif char == "√":
            replacement = "sqrt"
            previous_script = None
        elif char == "×":
            replacement = "*"
            previous_script = None
        elif char == "÷":
            replacement = "/"
            previous_script = None
        elif char == "≤":
            replacement = "<="
            previous_script = None
        elif char == "≥":
            replacement = ">="
            previous_script = None
        elif char == "≠":
            replacement = "!="
            previous_script = None
        elif char == "≈":
            replacement = "~"
            previous_script = None
        elif char == "±":
            replacement = "+-"
            previous_script = None
        elif char == "∓":
            replacement = "-+"
            previous_script = None
        elif char == "∞":
            replacement = "infinity"
            previous_script = None
        elif char == "∂":
            replacement = "partial"
            previous_script = None
        elif char == "∇":
            replacement = "nabla"
            previous_script = None
        elif char == "·":
            replacement = "*"
            previous_script = None
        elif char == "\u00ad":
            replacement = ""
            previous_script = None
        elif char in {"\ufffe", "\uffff"}:
            # A few PDF producers use noncharacters as soft line-break
            # artifacts.  They carry no document content.
            replacement = ""
            previous_script = None
        else:
            replacement = char
            if not char.isspace():
                previous_script = None
        chars.extend(MappedChar(value, index, index + 1) for value in replacement)
    return chars


def _latex_command_replacement(match: re.Match[str]) -> str:
    command = match.group(1)
    if command in GREEK_LATEX:
        return f" {GREEK_LATEX[command]} "
    if command in {"cdot", "times"}:
        return " * "
    if command in {"le", "leq"}:
        return " <= "
    if command in {"ge", "geq"}:
        return " >= "
    if command in {"ne", "neq"}:
        return " != "
    if command in {"approx", "sim"}:
        return " ~ "
    if command in {"pm"}:
        return " +- "
    if command in {"mp"}:
        return " -+ "
    if command == "infty":
        return " infinity "
    if command in {"sqrt"}:
        return " sqrt "
    if command in {
        "bar",
        "mathbf",
        "mathbb",
        "mathcal",
        "mathrm",
        "mathit",
        "operatorname",
        "overline",
        "overbrace",
        "text",
        "textbf",
        "textit",
        "tilde",
        "underbrace",
        "underline",
        "vec",
        "hat",
        "widetilde",
        "widehat",
        "boldsymbol",
        "pmb",
        "ensuremath",
        "overrightarrow",
        "left",
        "right",
        "displaystyle",
        "dfrac",
        "frac",
        "tfrac",
        "binom",
        "tag",
        "label",
    }:
        return " "
    if command in {",", ";", ":", "!"}:
        return " "
    # Unknown commands are retained as a word.  This is safer than silently
    # turning an unknown semantic symbol into whitespace.
    return f" {command} "


def normalize_with_mapping(text: str) -> NormalizedText:
    """Normalize equivalent PDF/LaTeX forms while retaining source offsets."""

    chars: list[MappedChar] = _expand_direct_chars(text)
    chars = _replace_mapped(chars, HTML_COMMENT_RE, " ")
    chars = _replace_mapped(chars, LINE_BREAK_HYPHEN_RE, lambda match: match.group(1) + match.group(2))
    chars = _replace_mapped(chars, MARKDOWN_EMPHASIS_RE, lambda match: match.group("body") or match.group("body2") or "")
    chars = _replace_mapped(chars, LATEX_ENV_RE, " ")
    chars = _replace_mapped(chars, LATEX_DELIMITER_RE, " ")
    chars = _replace_mapped(chars, LATEX_BREAK_RE, " ")
    chars = _replace_mapped(chars, LATEX_COMMAND_RE, _latex_command_replacement)
    # Subscript/superscript delimiters describe layout, not content.  The PDF
    # text layer often flattens them (``A_t`` becomes ``At``), so compare the
    # underlying symbols while retaining the original source for math-context
    # classification and visual review.
    chars = _replace_mapped(chars, SUB_SUP_RE, " ")
    chars = _replace_mapped(chars, BRACE_RE, " ")
    chars = _replace_mapped(chars, WHITESPACE_RE, " ")

    # Strip only leading/trailing normalization whitespace.  Keep the mapping
    # correct so a token at either edge can still receive a bounding box.
    while chars and chars[0].char == " ":
        chars.pop(0)
    while chars and chars[-1].char == " ":
        chars.pop()
    return NormalizedText(
        "".join(item.char for item in chars),
        tuple((item.start, item.end) for item in chars),
    )


def normalize_text(text: str) -> str:
    """Return the public, offset-free form used for exact comparison."""

    return normalize_with_mapping(text).text


def _bbox_for_span(chars: Sequence[CharPosition | None], start: int, end: int) -> BBox | None:
    boxes: list[CharPosition] = []
    for char in chars[max(0, start):min(len(chars), end)]:
        if char is not None:
            boxes.append(char)
    if not boxes:
        return None
    return (
        round(min(box.left for box in boxes), 3),
        round(min(box.bottom for box in boxes), 3),
        round(max(box.right for box in boxes), 3),
        round(max(box.top for box in boxes), 3),
    )


def semantic_tokens(normalized: NormalizedText, chars: Sequence[CharPosition | None] | None = None) -> list[Token]:
    def identifier_pieces(raw: str, start: int, end: int) -> list[tuple[str, int, int]]:
        if not raw.isalpha():
            return [(raw, start, end)]
        split_at = [index for index in range(1, len(raw)) if raw[index].isupper() and raw[index - 1].islower()]
        if split_at:
            boundaries = [0, *split_at, len(raw)]
            pieces: list[tuple[str, int, int]] = []
            for piece_start, piece_end in zip(boundaries, boundaries[1:]):
                pieces.extend(identifier_pieces(raw[piece_start:piece_end], start + piece_start, start + piece_end))
            return pieces
        if (
            len(raw) <= 4
            and len(raw) >= 3
            and raw[0].isupper()
            and raw[-1].lower() == "t"
        ) or (len(raw) == 2 and raw[0].isupper() and raw[1].islower()):
            return [
                (char, start + index, start + index + 1)
                for index, char in enumerate(raw)
            ]
        return [(raw, start, end)]

    tokens: list[Token] = []
    for match in SEMANTIC_TOKEN_RE.finditer(normalized.text):
        raw_match = match.group(0)
        pieces = identifier_pieces(raw_match, match.start(), match.end())
        for piece, piece_start, piece_end in pieces:
            value = piece.casefold()
            kind = "number" if NUMBER_RE.match(value) else "operator" if value in OPERATOR_VALUES else "word"
            spans = normalized.spans[piece_start:piece_end]
            raw_start = min(span[0] for span in spans)
            raw_end = max(span[1] for span in spans)
            bbox = _bbox_for_span(chars or [], raw_start, raw_end) if chars is not None else None
            tokens.append(Token(value=value, start=raw_start, end=raw_end, kind=kind, bbox=bbox))
        if (
            len(raw_match) == 2
            and raw_match.isalpha()
            and raw_match.islower()
            and raw_match in SHORT_MATH_IDENTIFIERS
        ):
            context = normalized.text[max(0, match.start() - 14):min(len(normalized.text), match.end() + 14)]
            if re.search(r"[=+\-*/<>]", context):
                # PDFium commonly flattens ``u_t``/``e_t`` into ``ut``/``et``.
                # Split only short lowercase identifiers in an operator-rich
                # neighborhood so ordinary prose remains word-level.
                tokens[-1].value = raw_match[0].casefold()
                tokens[-1].end = min(tokens[-1].end, raw_start + 1)
                second_start = raw_start + max(1, raw_end - raw_start) // 2
                tokens.append(Token(
                    value=raw_match[1].casefold(),
                    start=second_start,
                    end=raw_end,
                    kind="word",
                    bbox=_bbox_for_span(chars or [], second_start, raw_end) if chars is not None else None,
                ))
    return tokens


def formula_spans(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in MATH_SPAN_RE.finditer(text)]


def _overlaps(start: int, end: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(start < other_end and end > other_start for other_start, other_end in spans)


def _page_line_ranges(text: str) -> list[tuple[int, int, str]]:
    ranges: list[tuple[int, int, str]] = []
    cursor = 0
    for line in text.splitlines(keepends=True):
        end = cursor + len(line)
        ranges.append((cursor, end, line.rstrip("\r\n")))
        cursor = end
    if cursor < len(text) or not ranges:
        ranges.append((cursor, len(text), text[cursor:]))
    return ranges


def _header_key(line: str) -> str | None:
    values = [token.value for token in semantic_tokens(normalize_with_mapping(line))]
    if len(values) < 3 or len(values) > 20:
        return None
    if values and NUMBER_RE.match(values[-1]):
        values = values[:-1]
    if len(values) < 3:
        return None
    if all(value in ANCHOR_STOPWORDS or NUMBER_RE.match(value) for value in values):
        return None
    return " ".join(values)


def repeated_header_ranges(pages: Sequence[PdfPage]) -> dict[int, list[tuple[int, int]]]:
    """Find identical top/bottom lines that should not anchor page alignment."""

    candidates: dict[str, set[int]] = defaultdict(set)
    page_lines: dict[int, list[tuple[int, int, str, str]]] = {}
    for page in pages:
        lines = _page_line_ranges(page.text)
        nonempty = [index for index, (_, _, line) in enumerate(lines) if line.strip()]
        selected = set(nonempty[:3] + nonempty[-3:])
        page_lines[page.number] = []
        for index in sorted(selected):
            start, end, line = lines[index]
            key = _header_key(line)
            if key is not None:
                candidates[key].add(page.number)
                page_lines[page.number].append((start, end, line, key))

    repeated = {key for key, page_numbers in candidates.items() if len(page_numbers) >= 2}
    return {
        number: [(start, end) for start, end, _, key in lines if key in repeated]
        for number, lines in page_lines.items()
    }


def code_like_page(page: PdfPage) -> bool:
    """Return true for pages dominated by source code rather than prose/math."""

    matches = sum(1 for _, _, line in _page_line_ranges(page.text) if CODE_LIKE_LINE_RE.match(line.strip()))
    return matches >= 3


def reorder_positioned_scripts(tokens: Sequence[Token]) -> list[Token]:
    """Undo PDFium's common ``base, superscript, subscript`` text order."""

    reordered = list(tokens)
    for index in range(1, len(reordered)):
        current = reordered[index]
        if current.bbox is None:
            continue
        current_center_x = (current.bbox[0] + current.bbox[2]) / 2
        current_center_y = (current.bbox[1] + current.bbox[3]) / 2
        current_height = current.bbox[3] - current.bbox[1]
        insertion = index
        cursor = index - 1
        while cursor >= 0:
            previous = reordered[cursor]
            if previous.bbox is None:
                break
            previous_center_x = (previous.bbox[0] + previous.bbox[2]) / 2
            previous_center_y = (previous.bbox[1] + previous.bbox[3]) / 2
            previous_height = previous.bbox[3] - previous.bbox[1]
            if (
                previous_height > current_height + 1.5
                or previous_center_y <= current_center_y + 1.5
                or previous_center_y - current_center_y > 8.0
                or abs(previous_center_x - current_center_x) > 32.0
            ):
                break
            insertion = cursor
            cursor -= 1
        if insertion < index:
            reordered.insert(insertion, reordered.pop(index))
    return reordered


def _read_page_chars(textpage: Any, count: int) -> tuple[str, list[CharPosition | None]]:
    text = textpage.get_text_range(0, count) if count else ""
    chars: list[CharPosition | None] = []
    for index in range(len(text)):
        try:
            box = textpage.get_charbox(index, loose=True)
            if box is None or len(box) != 4 or not all(math.isfinite(float(value)) for value in box):
                chars.append(None)
            else:
                chars.append(CharPosition(*(float(value) for value in box)))
        except (IndexError, RuntimeError, TypeError, ValueError):
            chars.append(None)
    return text, chars


def extract_pdf_pages(pdf_path: Path) -> list[PdfPage]:
    """Extract page text and character boxes using the Nougat-side PDFium."""

    if pdfium is None:
        raise AuditUnavailable("pypdfium2 is unavailable in the audit interpreter")
    try:
        document = pdfium.PdfDocument(str(pdf_path))
    except Exception as exc:  # pypdfium2 exposes several backend exceptions.
        raise AuditUnavailable(f"could not open PDF with pypdfium2: {exc}") from exc

    pages: list[PdfPage] = []
    try:
        page_count = len(document)
        for index in range(page_count):
            page_number = index + 1
            page = None
            textpage = None
            try:
                page = document.get_page(index)
                size = tuple(float(value) for value in page.get_size())
                textpage = page.get_textpage()
                count = int(textpage.count_chars())
                text, chars = _read_page_chars(textpage, count)
                normalized = normalize_with_mapping(text)
                tokens = semantic_tokens(normalized, chars)
                pages.append(PdfPage(page_number, text, chars, size, tokens, []))
            except Exception as exc:
                pages.append(PdfPage(page_number, "", [], None, [], [], str(exc)))
            finally:
                if textpage is not None:
                    try:
                        textpage.close()
                    except Exception:
                        pass
                if page is not None:
                    try:
                        page.close()
                    except Exception:
                        pass
    finally:
        try:
            document.close()
        except Exception:
            pass

    ignored_by_page = repeated_header_ranges(pages)
    for page in pages:
        page.ignored_ranges = ignored_by_page.get(page.number, [])
        if page.ignored_ranges:
            page.tokens = [
                token
                for token in page.tokens
                if not _overlaps(token.start, token.end, page.ignored_ranges)
            ]
        page.tokens = reorder_positioned_scripts(page.tokens)
    return pages


def build_ngram_index(tokens: Sequence[Token], widths: Sequence[int] = (8, 6, 5, 4)) -> dict[int, dict[tuple[str, ...], list[int]]]:
    indexes: dict[int, dict[tuple[str, ...], list[int]]] = {}
    values = [token.value for token in tokens]
    for width in widths:
        index: dict[tuple[str, ...], list[int]] = defaultdict(list)
        for start in range(0, max(0, len(values) - width + 1)):
            index[tuple(values[start:start + width])].append(start)
        indexes[width] = index
    return indexes


def _anchor_is_strong(values: Sequence[str]) -> bool:
    strong = [value for value in values if value not in ANCHOR_STOPWORDS and (len(value) >= 3 or NUMBER_RE.match(value))]
    return len(strong) >= max(3, len(values) // 2)


def find_exact_anchor(
    page_tokens: Sequence[Token],
    nougat_tokens: Sequence[Token],
    nougat_index: dict[int, dict[tuple[str, ...], list[int]]],
    minimum_nougat_index: int = 0,
) -> PageAnchor | None:
    """Find a unique, exact, monotonic context anchor for one page."""

    values = [token.value for token in page_tokens]
    for width in (8, 6, 5, 4):
        if len(values) < width:
            continue
        for page_start in range(0, len(values) - width + 1):
            window = tuple(values[page_start:page_start + width])
            if not _anchor_is_strong(window):
                continue
            hits = [hit for hit in nougat_index.get(width, {}).get(window, []) if hit >= minimum_nougat_index]
            if len(hits) == 1:
                return PageAnchor(page_start, hits[0], width)
    return None


def align_pages(pages: Sequence[PdfPage], nougat_tokens: Sequence[Token]) -> tuple[list[PageAlignment], dict[int, str]]:
    index = build_ngram_index(nougat_tokens)
    anchors: dict[int, PageAnchor] = {}
    cursor = 0
    coverage: dict[int, str] = {}
    for page in pages:
        if len(page.tokens) < 5:
            coverage[page.number] = "unavailable"
            continue
        anchor = find_exact_anchor(page.tokens, nougat_tokens, index, cursor)
        if anchor is None:
            coverage[page.number] = "partial"
            continue
        anchors[page.number] = anchor
        cursor = anchor.nougat_token_index + anchor.width
        coverage[page.number] = "partial"  # An anchor is alignment evidence only.

    ordered = [(page.number, anchors[page.number]) for page in pages if page.number in anchors]
    alignments: list[PageAlignment] = []
    for position, (page_number, anchor) in enumerate(ordered):
        # Estimate the beginning from the anchor's relative offset.  The
        # overlap is intentional: it preserves prose before a late page anchor
        # without relying on a fragile page-break marker in Nougat.
        start = max(0, anchor.nougat_token_index - anchor.page_token_index - 12)
        if position + 1 < len(ordered):
            end = ordered[position + 1][1].nougat_token_index
        else:
            end = len(nougat_tokens)
        if end <= start:
            end = min(len(nougat_tokens), anchor.nougat_token_index + max(anchor.width, 1))
        alignments.append(PageAlignment(page_number, anchor, start, end))
    return alignments, coverage


def _source_snippet(text: str, tokens: Sequence[Token], start: int, end: int) -> str:
    selected = list(tokens[max(0, start):min(len(tokens), end)])
    if not selected:
        return ""
    raw_start = min(token.start for token in selected)
    raw_end = max(token.end for token in selected)
    return re.sub(r"\s+", " ", text[raw_start:raw_end]).strip()


def _contiguous_context(
    reference: Sequence[Token],
    extracted: Sequence[Token],
    reference_start: int,
    reference_end: int,
    extracted_start: int,
    extracted_end: int,
    direction: int,
    maximum: int = 8,
) -> int:
    count = 0
    while count < maximum:
        if direction < 0:
            reference_index = reference_start - count - 1
            extracted_index = extracted_start - count - 1
        else:
            reference_index = reference_end + count
            extracted_index = extracted_end + count
        if reference_index < 0 or extracted_index < 0:
            break
        if reference_index >= len(reference) or extracted_index >= len(extracted):
            break
        if reference[reference_index].value != extracted[extracted_index].value:
            break
        count += 1
    return count


def _is_math_token(token: Token, source: str, spans: Sequence[tuple[int, int]]) -> bool:
    if token.kind == "operator" or token.value in MATH_MARKER_VALUES:
        return True
    if _overlaps(token.start, token.end, spans):
        return True
    nearby = source[max(0, token.start - 48):min(len(source), token.end + 48)]
    if re.search(r"[\\$_^{}]|[α-ωΑ-Ωϵϑϕϖ]", nearby):
        return True
    # Plain-text PDF layers often lose the math delimiters.  A short
    # identifier adjacent to an equation-like operator is still useful math
    # evidence, while requiring an operand on both sides avoids treating an
    # ordinary prose hyphen as a mathematical context.
    return len(token.value) <= 2 and bool(
        re.search(r"[A-Za-z0-9]+\s*[=+*/<>]\s*[A-Za-z0-9]+", nearby)
    )


def _candidate_type(
    reference: Sequence[Token],
    extracted: Sequence[Token],
    reference_start: int,
    reference_end: int,
    extracted_start: int,
    extracted_end: int,
    reference_text: str,
    extracted_text: str,
    reference_math_spans: Sequence[tuple[int, int]],
    extracted_math_spans: Sequence[tuple[int, int]],
) -> str:
    reference_tokens = reference[reference_start:reference_end]
    extracted_tokens = extracted[extracted_start:extracted_end]
    if reference_tokens and extracted_tokens and all(token.kind == "number" for token in reference_tokens + extracted_tokens):
        return "digit"
    if any(_is_math_token(token, reference_text, reference_math_spans) for token in reference_tokens) or any(
        _is_math_token(token, extracted_text, extracted_math_spans) for token in extracted_tokens
    ):
        return "mathematical_symbol"
    return "prose"


def _confidence(kind: str, before: int, after: int, anchor_width: int | None = None) -> float:
    score = 0.68
    score += min(before, 6) * 0.025
    score += min(after, 6) * 0.025
    if kind == "mathematical_symbol":
        score += 0.08
    elif kind == "digit":
        score += 0.04
    if anchor_width:
        score += min(anchor_width, 8) * 0.01
    return round(min(0.99, score), 3)


def _bbox_for_tokens(tokens: Sequence[Token], start: int, end: int) -> BBox | None:
    boxes = [token.bbox for token in tokens[max(0, start):min(len(tokens), end)] if token.bbox is not None]
    if not boxes:
        return None
    return (
        round(min(box[0] for box in boxes), 3),
        round(min(box[1] for box in boxes), 3),
        round(max(box[2] for box in boxes), 3),
        round(max(box[3] for box in boxes), 3),
    )


def compare_token_sequences(
    reference_tokens: Sequence[Token],
    extracted_tokens: Sequence[Token],
    reference_text: str,
    extracted_text: str,
    page_number: int,
    origin: str,
    reference_math_spans: Sequence[tuple[int, int]] = (),
    extracted_math_spans: Sequence[tuple[int, int]] = (),
    anchor_width: int | None = None,
    include_bbox: bool = True,
    content_origin: str | None = None,
) -> list[dict[str, Any]]:
    """Return only bounded substitutions with exact context on both sides."""

    if not reference_tokens or not extracted_tokens:
        return []
    reference_values = [token.value for token in reference_tokens]
    extracted_values = [token.value for token in extracted_tokens]
    matcher = SequenceMatcher(None, reference_values, extracted_values, autojunk=False)
    opcodes = matcher.get_opcodes()
    findings: list[dict[str, Any]] = []
    for opcode_index, (tag, ref_start, ref_end, ext_start, ext_end) in enumerate(opcodes):
        if tag != "replace":
            continue
        ref_length = ref_end - ref_start
        ext_length = ext_end - ext_start
        if not (1 <= ref_length <= 4 and 1 <= ext_length <= 4 and ref_length == ext_length):
            continue
        before = _contiguous_context(
            reference_tokens, extracted_tokens, ref_start, ref_end, ext_start, ext_end, -1
        )
        after = _contiguous_context(
            reference_tokens, extracted_tokens, ref_start, ref_end, ext_start, ext_end, 1
        )
        # A substitution is actionable only with substantial agreement on both
        # sides.  This prevents page-order ambiguity and large table shifts from
        # turning into dozens of speculative findings.
        if before < 2 or after < 2:
            continue
        if opcode_index and opcodes[opcode_index - 1][0] == "replace":
            continue
        if opcode_index + 1 < len(opcodes) and opcodes[opcode_index + 1][0] == "replace":
            continue

        kind = _candidate_type(
            reference_tokens,
            extracted_tokens,
            ref_start,
            ref_end,
            ext_start,
            ext_end,
            reference_text,
            extracted_text,
            reference_math_spans,
            extracted_math_spans,
        )
        reference_snippet = _source_snippet(reference_text, reference_tokens, ref_start, ref_end)
        extracted_snippet = _source_snippet(extracted_text, extracted_tokens, ext_start, ext_end)
        context_before = _source_snippet(reference_text, reference_tokens, max(0, ref_start - before), ref_start)
        context_after = _source_snippet(reference_text, reference_tokens, ref_end, min(len(reference_tokens), ref_end + after))
        evidence = {
            "before": context_before,
            "after": context_after,
            "matching_tokens_before": before,
            "matching_tokens_after": after,
            "alignment": "exact-context",
        }
        finding: dict[str, Any] = {
            "page": page_number,
            "origin": origin,
            "content_origin": content_origin or origin,
            "type": kind,
            "confidence": _confidence(kind, before, after, anchor_width),
            "reference": reference_snippet,
            "reference_snippet": reference_snippet,
            "extracted": extracted_snippet,
            "extracted_snippet": extracted_snippet,
            "contextual_evidence": evidence,
        }
        if include_bbox:
            bbox = _bbox_for_tokens(reference_tokens, ref_start, ref_end)
            if bbox is not None:
                finding["bbox"] = {
                    "left": bbox[0],
                    "bottom": bbox[1],
                    "right": bbox[2],
                    "top": bbox[3],
                }
        findings.append(finding)
    return findings


def _token_subsequence(values: Sequence[str], needle: Sequence[str]) -> list[int]:
    if not needle or len(needle) > len(values):
        return []
    return [
        index
        for index in range(len(values) - len(needle) + 1)
        if list(values[index:index + len(needle)]) == list(needle)
    ]


def _included_in_combined(text: str, combined_tokens: Sequence[Token], evidence: str) -> bool:
    evidence_tokens = semantic_tokens(normalize_with_mapping(evidence))
    if len(evidence_tokens) < 3:
        return False
    return bool(_token_subsequence([token.value for token in combined_tokens], [token.value for token in evidence_tokens]))


def _canonical_caption(line: str) -> str | None:
    match = FIGURE_LINE_RE.match(line.strip())
    return match.group("body").strip() if match else None


def extract_docling_evidence(docling_text: str, combined_text: str) -> list[DoclingEvidence]:
    """Keep only captions/footnotes that the merge actually emitted."""

    combined_tokens = semantic_tokens(normalize_with_mapping(combined_text))
    evidence: list[DoclingEvidence] = []
    lines = docling_text.splitlines()
    for index, line in enumerate(lines):
        caption = _canonical_caption(line)
        if caption and _included_in_combined(caption, combined_tokens, caption):
            evidence.append(DoclingEvidence("docling_caption", caption))
            continue
        footnote_match = FOOTNOTE_DEF_RE.match(line.strip())
        if footnote_match:
            body = footnote_match.group("body").strip()
            if _included_in_combined(body, combined_tokens, body):
                evidence.append(DoclingEvidence("docling_footnote", body))
            continue
        # A raw numbered Docling footnote is accepted only when the merged
        # document contains the corresponding Markdown marker.
        numbered = NUMBERED_FOOTNOTE_RE.match(line.strip())
        if numbered and f"[^{numbered.group('number')}]" in combined_text:
            body = numbered.group("body").strip()
            if _included_in_combined(body, combined_tokens, body):
                evidence.append(DoclingEvidence("docling_footnote", body))

    unique: list[DoclingEvidence] = []
    seen: set[tuple[str, str]] = set()
    for item in evidence:
        key = (item.origin, normalize_text(item.text).casefold())
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _best_evidence_page(evidence_tokens: Sequence[Token], pages: Sequence[PdfPage]) -> tuple[PdfPage, int, int] | None:
    values = [token.value for token in evidence_tokens]
    for width in (min(6, len(values)), min(4, len(values)), min(3, len(values))):
        if width < 3:
            continue
        needle = values[:width]
        matches: list[tuple[PdfPage, int]] = []
        for page in pages:
            positions = _token_subsequence([token.value for token in page.tokens], needle)
            matches.extend((page, position) for position in positions)
        if len(matches) == 1:
            page, position = matches[0]
            return page, position, width
    return None


def audit_docling_evidence(
    evidence: Sequence[DoclingEvidence],
    pages: Sequence[PdfPage],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for item in evidence:
        normalized = normalize_with_mapping(item.text)
        evidence_tokens = semantic_tokens(normalized)
        match = _best_evidence_page(evidence_tokens, pages)
        if match is None:
            continue
        page, page_start, anchor_width = match
        # Align the complete evidence block around its exact anchor.  Leading
        # and trailing page context lets the same conservative comparator catch
        # one wrong word in a caption or footnote.
        evidence_anchor = next(
            index for index, token in enumerate(evidence_tokens) if token.value == page.tokens[page_start].value
        )
        ref_start = max(0, page_start - evidence_anchor - 4)
        ref_end = min(len(page.tokens), ref_start + len(evidence_tokens) + 8)
        subset = page.tokens[ref_start:ref_end]
        extracted = [
            Token(token.value, token.start, token.end, token.kind)
            for token in evidence_tokens
        ]
        findings.extend(
            compare_token_sequences(
                subset,
                extracted,
                page.text,
                item.text,
                page.number,
                item.origin,
                formula_spans(page.text),
                formula_spans(item.text),
                anchor_width,
                include_bbox=True,
            )
        )
    return findings


def split_docling_page_chunks(text: str, page_count: int) -> list[tuple[int | None, str]]:
    markers = list(PAGE_MARKER_RE.finditer(text))
    if not markers:
        return [(1, text)] if page_count == 1 else []
    chunks: list[tuple[int | None, str]] = []
    cursor = 0
    current_page: int | None = None
    for marker in markers:
        chunk = text[cursor:marker.start()]
        if chunk.strip():
            chunks.append((current_page, chunk))
        current_page = int(marker.group(1) or marker.group(2))
        cursor = marker.end()
    if text[cursor:].strip():
        chunks.append((current_page, text[cursor:]))
    return chunks


def prose_only(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        if stripped.startswith("!") or stripped.startswith("|") or stripped.startswith("#"):
            continue
        if FIGURE_LINE_RE.match(stripped) or stripped.lower().startswith("table "):
            continue
        if MATH_SPAN_RE.fullmatch(stripped):
            continue
        lines.append(stripped)
    return "\n".join(lines)


def audit_docling_fallback(
    docling_text: str,
    nougat_text: str,
    pages: Sequence[PdfPage],
    unavailable_pages: Sequence[int],
) -> list[dict[str, Any]]:
    """Use page-marked Docling prose as a lower-confidence reference only."""

    if not pages:
        return []
    nougat_normalized = normalize_with_mapping(nougat_text)
    nougat_tokens = semantic_tokens(nougat_normalized)
    unavailable = set(unavailable_pages)
    findings: list[dict[str, Any]] = []
    for page_hint, chunk in split_docling_page_chunks(docling_text, len(pages)):
        if page_hint not in unavailable:
            continue
        reference_text = prose_only(chunk)
        if len(semantic_tokens(normalize_with_mapping(reference_text))) < 8:
            continue
        reference_tokens = semantic_tokens(normalize_with_mapping(reference_text))
        # Look for a unique strong anchor in Nougat.  Without one, a document
        # level comparison would silently assign prose to the wrong page.
        anchor = find_exact_anchor(
            reference_tokens,
            nougat_tokens,
            build_ngram_index(nougat_tokens),
            0,
        )
        if anchor is None:
            continue
        start = max(0, anchor.nougat_token_index - anchor.page_token_index - 4)
        end = min(len(nougat_tokens), anchor.nougat_token_index + len(reference_tokens) + 8)
        fallback_findings = compare_token_sequences(
                reference_tokens,
                nougat_tokens[start:end],
                reference_text,
                nougat_text,
                page_hint or 1,
                "docling_prose_fallback",
                (),
                formula_spans(nougat_text),
                anchor.width,
                include_bbox=False,
                content_origin="nougat_body",
            )
        for finding in fallback_findings:
            # The fallback is explicitly prose-only; mathematical content is
            # not source evidence when the PDF text layer is unavailable.
            if finding.get("type") == "prose":
                finding["confidence"] = min(float(finding.get("confidence", 0.0)), 0.74)
                finding["confidence_basis"] = "lower-confidence-docling-fallback"
                findings.append(finding)
    return findings


def _finding_fingerprint(pdf_hash: str, finding: dict[str, Any]) -> str:
    payload = {
        "pdf_sha256": pdf_hash,
        "page": finding.get("page"),
        "bbox": finding.get("bbox"),
        "origin": finding.get("origin"),
        "type": finding.get("type"),
        "reference": finding.get("reference"),
        "extracted": finding.get("extracted"),
        "before": finding.get("contextual_evidence", {}).get("before"),
        "after": finding.get("contextual_evidence", {}).get("after"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finding_location_key(finding: dict[str, Any]) -> tuple[str, ...]:
    bbox = finding.get("bbox")
    if isinstance(bbox, dict):
        return ("bbox", json.dumps(bbox, sort_keys=True, separators=(",", ":")))
    evidence = finding.get("contextual_evidence", {})
    return (
        "context",
        normalize_text(str(evidence.get("before", ""))).casefold(),
        normalize_text(str(evidence.get("after", ""))).casefold(),
    )


def _finding_id(finding: dict[str, Any]) -> str:
    evidence = finding.get("contextual_evidence", {})
    payload = "|".join(
        str(value)
        for value in (
            finding.get("page"),
            finding.get("origin"),
            finding.get("type"),
            evidence.get("before", ""),
            finding.get("reference", ""),
            finding.get("extracted", ""),
            evidence.get("after", ""),
        )
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"p{int(finding.get('page', 0)):03d}-{finding.get('type', 'finding')}-{digest}"


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return value if isinstance(value, dict) else default


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def review_resolutions(ledger: dict[str, Any]) -> dict[str, dict[str, Any]]:
    resolutions = ledger.get("resolutions")
    if not isinstance(resolutions, dict):
        return {}
    return {key: value for key, value in resolutions.items() if isinstance(value, dict)}


def apply_review_ledger(findings: list[dict[str, Any]], ledger: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    resolutions = review_resolutions(ledger)
    suppressed = 0
    for finding in findings:
        record = resolutions.get(finding["id"])
        if not record or record.get("fingerprint") != finding.get("fingerprint"):
            finding["active"] = True
            continue
        status = record.get("status")
        finding["resolution"] = status
        finding["active"] = status not in {"source_matches", "false_positive"}
        if not finding["active"]:
            suppressed += 1
    return findings, suppressed


def coverage_summary(coverage: dict[int, str]) -> dict[str, int]:
    return {
        status: sum(1 for value in coverage.values() if value == status)
        for status in ("full", "partial", "unavailable")
    }


def human_report(report: dict[str, Any]) -> str:
    lines = ["# OCR fidelity audit", "", f"Status: {str(report.get('status', 'review_required')).replace('_', ' ').upper()}", ""]
    execution = report.get("execution", {})
    if isinstance(execution, dict) and not execution.get("ok", True):
        lines.extend([f"Audit execution: failed — {execution.get('error', 'unknown error')}", ""])
    lines.extend([f"Content completeness: {report.get('completeness', 'unknown')}",
                  "Coverage describes comparison with the PDF text layer, not visual fidelity.", ""])
    coverage = report.get("coverage", {})
    if isinstance(coverage, dict):
        lines.append("Coverage by PDF page:")
        for page, status in sorted(coverage.items(), key=lambda item: int(item[0])):
            lines.append(f"- Page {page}: {status}")
        lines.append("")
    limitations = report.get("limitations", [])
    if limitations:
        lines.append("Coverage limitations:")
        lines.extend(f"- {item}" for item in limitations)
        lines.append("")
    findings = report.get("findings", [])
    active = [finding for finding in findings if finding.get("active", True)]
    if not findings:
        lines.append("No bounded substitutions were detected.")
    else:
        lines.append(f"Findings: {len(active)} active, {len(findings) - len(active)} resolved/suppressed")
        lines.append("")
        for finding in findings:
            state = "ACTIVE" if finding.get("active", True) else str(finding.get("resolution", "resolved")).upper()
            location = f"Page {finding.get('page')}"
            if finding.get("bbox"):
                box = finding["bbox"]
                location += f" [{box['left']}, {box['bottom']}, {box['right']}, {box['top']}]"
            lines.extend(
                [
                    f"- {state} `{finding.get('id')}` — {location}, {finding.get('origin')}, {finding.get('type')}, confidence {finding.get('confidence')}",
                    f"  PDF: `{str(finding.get('reference', '')).replace('`', '')}`",
                    f"  Extracted: `{str(finding.get('extracted', '')).replace('`', '')}`",
                    f"  Context: {finding.get('contextual_evidence', {}).get('before', '')} … {finding.get('contextual_evidence', {}).get('after', '')}",
                ]
            )
    lines.append("")
    lines.append("A finding is a review prompt, not an automatic correction.")
    return "\n".join(lines) + "\n"


def write_report(output_dir: Path, report: dict[str, Any]) -> None:
    atomic_write_json(output_dir / "ocr_audit.json", report)
    (output_dir / "ocr_audit.md").write_text(human_report(report), encoding="utf-8")


def _failure_report(pdf_path: Path, message: str) -> dict[str, Any]:
    try:
        pdf_hash = sha256_file(pdf_path)
    except OSError:
        pdf_hash = None
    return {
        "schema": AUDIT_SCHEMA_VERSION,
        "status": "review_required",
        "pdf_sha256": pdf_hash,
        "coverage": {},
        "coverage_by_page": {},
        "findings": [],
        "limitations": ["OCR fidelity audit could not execute; inspect the extracted document manually.", message],
        "execution": {"ok": False, "error": message},
        "summary": {"detected_findings": 0, "active_findings": 0, "suppressed_findings": 0},
    }


def audit_document(
    pdf_path: Path,
    nougat_path: Path,
    docling_path: Path | None,
    combined_path: Path,
    review_path: Path,
) -> dict[str, Any]:
    pdf_hash = sha256_file(pdf_path)
    combined_text = combined_path.read_text(encoding="utf-8")
    docling_text = docling_path.read_text(encoding="utf-8") if docling_path and docling_path.is_file() else ""
    pages = extract_pdf_pages(pdf_path)
    # Compare the delivered artifact; raw Nougat remains diagnostic input only.
    page_map = reconcile(pages, combined_text, pdf_hash)
    normalized_final = normalize_with_mapping(combined_text)
    final_tokens = semantic_tokens(normalized_final)
    alignments, _ = align_pages(pages, final_tokens)
    coverage = {p["page"]: p["fidelity_coverage"] for p in page_map["pages"]}

    findings: list[dict[str, Any]] = []
    page_by_number = {page.number: page for page in pages}
    for alignment in alignments:
        page = page_by_number[alignment.page_number]
        if any(m["page"] == page.number for m in page_map["missing_markers"]):
            continue
        if code_like_page(page):
            coverage[page.number] = "partial"
            continue
        findings.extend(
            compare_token_sequences(
                page.tokens,
                final_tokens[alignment.nougat_start:alignment.nougat_end],
                page.text,
                combined_text,
                page.number,
                "final_body",
                formula_spans(page.text),
                formula_spans(combined_text),
                alignment.anchor.width,
                include_bbox=True,
            )
        )

    evidence = extract_docling_evidence(docling_text, combined_text)
    if evidence:
        findings.extend(audit_docling_evidence(evidence, pages))

    limitations: list[str] = [page_map["coverage_scope"]]
    unavailable_pages = [page.number for page in pages if not page.tokens]
    unmapped_pages = [page.number for page in pages if page.tokens and coverage.get(page.number) == "unavailable"]
    if unmapped_pages:
        limitations.append("Pages without usable final-artifact alignment: " + ", ".join(map(str, unmapped_pages)) + ".")
    code_pages = [page.number for page in pages if code_like_page(page)]
    partial_pages = [
        page.number
        for page in pages
        if coverage.get(page.number) == "partial" and page.number not in code_pages
    ]
    if unavailable_pages:
        limitations.append(
            "PDF pages without a usable text layer: "
            + ", ".join(str(page) for page in unavailable_pages)
            + "."
        )
        limitations.append("Mathematical coverage is limited on pages without a usable PDF text layer.")
    if partial_pages:
        limitations.append(
            "Some pages had text but incomplete final-artifact text-layer coverage: "
            + ", ".join(str(page) for page in partial_pages)
            + "."
        )
    if code_pages:
        limitations.append(
            "Code-heavy pages were not compared as prose/math: "
            + ", ".join(str(page) for page in code_pages)
            + "."
        )
    if unavailable_pages and docling_text:
        fallback = audit_docling_fallback(docling_text, combined_text, pages, unavailable_pages)
        if fallback:
            for finding in fallback:
                finding["content_origin"] = "final_body"
            findings.extend(fallback)
            limitations.append("Docling was used as a lower-confidence prose reference where page markers allowed strong alignment.")
        else:
            limitations.append("Cached Docling prose had no strong page-marked anchor for fallback comparison.")
    if not any(_is_math_token(token, combined_text, formula_spans(combined_text)) for token in final_tokens):
        limitations.append("No mathematical content was safely aligned; mathematical coverage is limited.")

    # De-duplicate overlapping findings produced by a page comparison and a
    # Docling evidence comparison.  Prefer the final-body finding.
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for finding in findings:
        key = (
            finding.get("page"),
            finding.get("type"),
            normalize_text(str(finding.get("reference", ""))).casefold(),
            normalize_text(str(finding.get("extracted", ""))).casefold(),
            _finding_location_key(finding),
        )
        previous = unique.get(key)
        if previous is None or previous.get("origin") != "final_body" and finding.get("origin") == "final_body":
            unique[key] = finding
    findings = list(unique.values())
    for finding in findings:
        finding["id"] = _finding_id(finding)
        finding["fingerprint"] = _finding_fingerprint(pdf_hash, finding)
    findings.sort(key=lambda item: (int(item.get("page", 0)), str(item.get("id", ""))))

    ledger = load_json(review_path, {"schema": REVIEW_SCHEMA_VERSION, "resolutions": {}})
    ledger.setdefault("schema", REVIEW_SCHEMA_VERSION)
    if not isinstance(ledger.get("resolutions"), dict):
        ledger["resolutions"] = {}
    # Keep the ledger present even when the first audit has no findings.  It is
    # intentionally separate from the report so a merge-only rerun can reuse
    # human classifications without mutating the expensive OCR artifacts.
    atomic_write_json(review_path, ledger)
    findings, suppressed = apply_review_ledger(findings, ledger)
    active_count = sum(1 for finding in findings if finding.get("active", True))
    report_coverage = {str(page): status for page, status in sorted(coverage.items())}
    report: dict[str, Any] = {
        "schema": AUDIT_SCHEMA_VERSION,
        "status": "review_required" if active_count or page_map["status"] != "complete" or page_map["numbering_diagnostics"] or any(v != "full" for v in coverage.values()) else "clear",
        "final_md_sha256": digest(combined_text),
        "completeness": page_map["status"],
        "numbering_diagnostics": page_map["numbering_diagnostics"],
        "alignment_coverage": {str(p["page"]): p["mapping_confidence"] for p in page_map["pages"]},
        "coverage_scope": page_map["coverage_scope"],
        "pdf_sha256": pdf_hash,
        "pdf_pages": len(pages),
        "coverage": report_coverage,
        "coverage_by_page": report_coverage,
        "coverage_summary": coverage_summary(coverage),
        "findings": findings,
        "limitations": limitations,
        "execution": {"ok": True, "engine": "pypdfium2"},
        "summary": {
            "detected_findings": len(findings),
            "active_findings": active_count,
            "suppressed_findings": suppressed,
        },
    }
    return report


def run_audit(
    pdf_path: Path,
    nougat_path: Path,
    docling_path: Path | None,
    combined_path: Path,
    output_dir: Path,
    review_path: Path | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = review_path or output_dir / "ocr_audit_review.json"
    try:
        report = audit_document(pdf_path, nougat_path, docling_path, combined_path, ledger_path)
    except Exception as exc:
        report = _failure_report(pdf_path, str(exc))
        if not ledger_path.is_file():
            atomic_write_json(ledger_path, {"schema": REVIEW_SCHEMA_VERSION, "resolutions": {}})
    write_report(output_dir, report)
    return report


def resolve_finding(
    job_dir: Path,
    finding_id: str,
    status: str,
    note: str | None = None,
) -> int:
    if status not in RESOLUTION_STATUSES:
        print(f"Unknown resolution status: {status}", file=sys.stderr)
        return 2
    report_path = job_dir / "ocr_audit.json"
    report = load_json(report_path, {})
    findings = report.get("findings")
    if not isinstance(findings, list):
        print(f"No OCR audit findings found in {report_path}", file=sys.stderr)
        return 1
    finding = next((item for item in findings if isinstance(item, dict) and item.get("id") == finding_id), None)
    if finding is None:
        print(f"Finding not found: {finding_id}", file=sys.stderr)
        return 1
    ledger_path = job_dir / "ocr_audit_review.json"
    ledger = load_json(ledger_path, {"schema": REVIEW_SCHEMA_VERSION, "resolutions": {}})
    resolutions = ledger.setdefault("resolutions", {})
    if not isinstance(resolutions, dict):
        resolutions = {}
        ledger["resolutions"] = resolutions
    record: dict[str, Any] = {
        "status": status,
        "fingerprint": finding.get("fingerprint"),
    }
    if note:
        record["note"] = note
    resolutions[finding_id] = record
    ledger["schema"] = REVIEW_SCHEMA_VERSION
    atomic_write_json(ledger_path, ledger)

    # Refresh only the review state in the human/machine reports.  The next
    # normal or merge-only run performs a full audit and validates fingerprints.
    report_findings = [dict(item) for item in findings if isinstance(item, dict)]
    applied, suppressed = apply_review_ledger(report_findings, ledger)
    report["findings"] = applied
    active = sum(1 for item in report["findings"] if item.get("active", True))
    report["status"] = "review_required" if (active or not report.get("execution", {}).get("ok", True)
        or report.get("completeness", "unknown") != "complete" or report.get("numbering_diagnostics")
        or any(v != "full" for v in report.get("coverage", {}).values())) else "clear"
    summary = report.setdefault("summary", {})
    summary["active_findings"] = active
    summary["suppressed_findings"] = suppressed
    atomic_write_json(report_path, report)
    (job_dir / "ocr_audit.md").write_text(human_report(report), encoding="utf-8")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit OCR Markdown against a PDF text layer.")
    parser.add_argument("pdf", type=Path, help="PDF that was extracted")
    parser.add_argument("job_dir_positional", nargs="?", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--output-dir", "--job-dir", dest="output_dir", type=Path)
    parser.add_argument("--nougat-md", "--nougat", dest="nougat_md", type=Path)
    parser.add_argument("--docling-md", "--docling", dest="docling_md", type=Path)
    parser.add_argument("--combined-md", "--combined", dest="combined_md", type=Path)
    parser.add_argument("--review-ledger", type=Path)
    return parser


def safe_stem(path: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", path.stem).strip("._")
    return stem or "document"


def build_resolve_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record a human resolution for an OCR audit finding.")
    parser.add_argument("job_dir", nargs="?", type=Path)
    parser.add_argument("finding_id", nargs="?")
    parser.add_argument("status", nargs="?", choices=RESOLUTION_STATUSES)
    parser.add_argument("--job-dir", dest="job_dir_option", type=Path)
    parser.add_argument("--finding-id", dest="finding_id_option")
    parser.add_argument("--status", dest="status_option", choices=RESOLUTION_STATUSES)
    parser.add_argument("--note")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    if arguments and arguments[0] in {"resolve", "review"}:
        args = build_resolve_parser().parse_args(arguments[1:])
        job_dir = args.job_dir_option or args.job_dir
        finding_id = args.finding_id_option or args.finding_id
        status = args.status_option or args.status
        if job_dir is None or finding_id is None or status is None:
            print("resolve requires a job directory, finding ID, and resolution status.", file=sys.stderr)
            return 2
        return resolve_finding(job_dir.expanduser().resolve(), finding_id, status, args.note)

    args = build_parser().parse_args(arguments)
    pdf_path = args.pdf.expanduser().resolve()
    output_dir = (args.output_dir or args.job_dir_positional)
    if output_dir is None:
        print("An output/job directory is required.", file=sys.stderr)
        return 2
    output_dir = output_dir.expanduser().resolve()
    nougat_path = (args.nougat_md or output_dir / "nougat.mmd").expanduser().resolve()
    docling_path = args.docling_md.expanduser().resolve() if args.docling_md else output_dir / "docling_decoded.md"
    combined_path = (
        args.combined_md or output_dir / f"{safe_stem(pdf_path)}.md"
    ).expanduser().resolve()
    report = run_audit(pdf_path, nougat_path, docling_path, combined_path, output_dir, args.review_ledger)
    execution = report.get("execution", {})
    if isinstance(execution, dict) and not execution.get("ok", True):
        print(f"OCR audit warning: {execution.get('error', 'audit failed')}", file=sys.stderr)
    else:
        print(f"OCR audit: {report.get('status', 'review_required')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
