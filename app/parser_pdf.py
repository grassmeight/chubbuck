"""PDF -> classified items.

pdfplumber returns characters in visual order without reliable space chars, so
we reconstruct each line from its chars (inserting a space at any x-gap larger
than _WORD_GAP_PT), bidi-flip into logical order, then run the cleanup regexes.

Bold detection: per-line majority vote on font name (looking for 'bold',
'black', 'heavy', 'extrab').

Underline detection: scan page rects+lines for thin horizontal strokes whose
y-coordinate falls in a narrow window straddling the line's baseline. Only
checked for bold lines (names = bold + underline).
"""
from __future__ import annotations

from pathlib import Path

import pdfplumber
from bidi.algorithm import get_display

from app.parser_common import LogicalLine, clean, lines_to_items

# An underline rect must be thin and at most this many points tall.
UNDERLINE_MAX_HEIGHT = 2.0
# Underline can sit slightly above the bbox bottom (baseline) or just below.
UNDERLINE_Y_ABOVE = 8.0
UNDERLINE_Y_BELOW = 4.0
UNDERLINE_X_OVERLAP_RATIO = 0.5
# Fraction of chars that must be in a bold font for the line to count as bold.
BOLD_FRACTION_THRESHOLD = 0.6
# Centering: line midpoint must be within this fraction of the page width
# from the page midpoint, AND the line must have substantial left+right
# margins (not just a full-width line that happens to be centered).
_CENTER_MIDPOINT_TOLERANCE_RATIO = 0.05
_CENTER_MIN_MARGIN_RATIO = 0.10
# Minimum x-gap (in points) between adjacent characters that indicates a word
# boundary. Some PDFs encode actual space characters between words; others
# position each word by coordinates and rely on the gap alone.
_WORD_GAP_PT = 1.0


def _is_bold_font(fontname: str | None) -> bool:
    if not fontname:
        return False
    f = fontname.lower()
    return "bold" in f or "black" in f or "heavy" in f or "extrab" in f


def _collect_underlines(page) -> list[tuple[float, float, float]]:
    """Return list of (y, x0, x1) for thin horizontal rects/lines on the page."""
    underlines = []
    for r in page.rects:
        h = r["height"]
        w = r["width"]
        if 0 < h <= UNDERLINE_MAX_HEIGHT and w > 1:
            y = (r["top"] + r["bottom"]) / 2
            underlines.append((y, r["x0"], r["x1"]))
    for ln in page.lines:
        h = abs(ln["height"])
        w = abs(ln["width"])
        if h <= UNDERLINE_MAX_HEIGHT and w > 1:
            y = (ln["top"] + ln["bottom"]) / 2
            underlines.append((y, ln["x0"], ln["x1"]))
    return underlines


def _line_underlined(top: float, bottom: float, x0: float, x1: float,
                     underlines: list[tuple[float, float, float]]) -> bool:
    span_w = max(x1 - x0, 1.0)
    for uy, ux0, ux1 in underlines:
        if not (bottom - UNDERLINE_Y_ABOVE <= uy <= bottom + UNDERLINE_Y_BELOW):
            continue
        overlap = min(x1, ux1) - max(x0, ux0)
        if overlap / span_w >= UNDERLINE_X_OVERLAP_RATIO:
            return True
    return False


def _reconstruct_line_text(chars: list[dict]) -> str:
    """Build the raw (visual-order) text of a line from its chars, inserting
    a space at any x-gap larger than _WORD_GAP_PT.
    """
    if not chars:
        return ""
    chars_sorted = sorted(chars, key=lambda c: c["x0"])
    parts: list[str] = []
    prev_x1: float | None = None
    for c in chars_sorted:
        ch = c.get("text", "")
        if not ch:
            continue
        if prev_x1 is not None:
            gap = c["x0"] - prev_x1
            if gap > _WORD_GAP_PT:
                last = parts[-1] if parts else ""
                if not last.endswith(" ") and not ch.startswith(" "):
                    parts.append(" ")
        parts.append(ch)
        prev_x1 = c["x1"]
    return "".join(parts)


def _is_centered(x0: float, x1: float, page_width: float) -> bool:
    if page_width <= 0:
        return False
    line_mid = (x0 + x1) / 2
    page_mid = page_width / 2
    if abs(line_mid - page_mid) > page_width * _CENTER_MIDPOINT_TOLERANCE_RATIO:
        return False
    left_margin = x0
    right_margin = page_width - x1
    min_margin = page_width * _CENTER_MIN_MARGIN_RATIO
    return left_margin >= min_margin and right_margin >= min_margin


def _extract_logical_lines(pdf_path: Path) -> list[LogicalLine]:
    out: list[LogicalLine] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            underlines = _collect_underlines(page)
            page_width = float(page.width)
            text_lines = page.extract_text_lines(layout=False, return_chars=True)
            for tl in text_lines:
                chars = tl.get("chars", [])
                if not chars:
                    continue
                non_ws = [c for c in chars if c.get("text", "").strip()]
                if not non_ws:
                    continue
                raw_text = _reconstruct_line_text(chars)
                if not raw_text.strip():
                    continue
                bold_count = sum(1 for c in non_ws if _is_bold_font(c.get("fontname")))
                bold = (bold_count / len(non_ws)) >= BOLD_FRACTION_THRESHOLD
                top = tl["top"]
                bottom = tl["bottom"]
                x0 = tl["x0"]
                x1 = tl["x1"]
                underlined = _line_underlined(top, bottom, x0, x1, underlines) if bold else False
                centered = _is_centered(x0, x1, page_width) if bold else False
                logical_text = clean(get_display(raw_text, base_dir='R'),
                                     fix_pdf_split_hebrew=True)
                out.append(LogicalLine(
                    text=logical_text.strip(),
                    bold=bold,
                    underlined=underlined,
                    centered=centered,
                    top=top, bottom=bottom, x0=x0, x1=x1, page=page_num,
                ))
    return out


def parse_pdf(pdf_path: str | Path) -> list[dict]:
    return lines_to_items(_extract_logical_lines(Path(pdf_path)))
