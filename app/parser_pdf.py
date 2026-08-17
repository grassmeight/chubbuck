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

from app.parser_common import LogicalLine, _is_watermark, clean, lines_to_items

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
# A line is "right-edge aligned" if its x1 is within this many points of the
# document's right text margin (computed per-doc). 20pt is generous enough
# to absorb pdfplumber bbox jitter (one Rain Man stage direction landed at
# x1=492.8 vs the 508.7 margin — 16pt short — while every dialogue line in
# the same doc sits at x1=418, well outside the window). For the 4 canonical
# reference files, no unbold uncentered line falls within this window, so
# the rule is a no-op for the standard "stage notes are bolded" convention.
_RIGHT_EDGE_TOLERANCE_PT = 20.0
# Disable the right-aligned stage-direction promotion when more than this
# fraction of unbold+uncentered lines reach the right margin. A justified
# document (where dialogue lines stretch to fill the full text width when
# wrapped) puts dialogue at the right margin too, so the geometric rule
# can no longer distinguish stage notes from dialogue. Threshold sits well
# above the observed 6–18% in screenplays with narrow-column dialogue and
# well below the ~38% measured in a justified doc.
_JUSTIFIED_DOC_RATIO_THRESHOLD = 0.25
# Disable the promotion when at least this share of the near-right-margin
# lines are also horizontally centered — the mark of a center-justified
# script whose long dialogue lines reach the margin, rather than a
# right-aligned stage note. Observed shares: 0% (Rain Man), 29% (Raging
# Bull), 100% (Gilmore Girls); 75% sits well clear of both sides.
_CENTERED_BLOCK_RATIO_THRESHOLD = 0.75
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


def _annotate_right_aligned_lines(lines: list[LogicalLine]) -> None:
    """Flag unbolded lines whose right edge meets the document's right margin.

    In Hebrew screenplays that don't bold stage notes, the stage notes are
    right-aligned to the page text margin while dialogue sits in a narrow
    centered column whose right edge is well inside the page. The rule:
    bold and centered lines are skipped (they have their own classifications);
    a remaining line whose x1 lands within `_RIGHT_EDGE_TOLERANCE_PT` of the
    document's max x1 gets promoted to stage_direction.

    Watermark lines are excluded from the margin computation so a stray
    watermark in the page corner can't shift the apparent right edge.

    Width is intentionally NOT required: short single-line stage directions
    ("ריימונד לא זז", "ריימונד מתבלבל") sit at the right margin too, and a
    width minimum would lose them.

    Justified-document guard: if a large fraction of unbolded lines reach
    the right margin, the dialogue itself is right-aligned (e.g. literary-
    script formatting that justifies long paragraphs) and the rule cannot
    distinguish stage notes from dialogue. Skip annotation so dialogue isn't
    misclassified — better to lose the rare opening stage note than to
    scatter half the dialogue into stage_direction.

    Centered-block guard: a *center-justified* script sets its dialogue with
    equal left and right margins, so the long lines of a speech reach the
    right margin too and the ratio guard alone doesn't catch them (observed
    22% in בנות גילמור — under the 25% threshold because the many short
    single-line replies dilute it). Centering separates the two cases
    cleanly, but only per-document: a genuinely right-aligned line has its
    right edge pinned to the margin and a ragged left edge (Rain Man: right
    gap 86.6-86.8pt across nine notes, left gap 235-453), while a centered
    line's edges move together (Gilmore: right gap 92-108). A single
    full-width line is inherently ambiguous — it looks centered *and*
    right-aligned — so we judge the block, not the line: when nearly every
    near-edge line is centered, the "right margin" we found is really the
    page's center-justified text block. Observed centered shares: 0% (Rain
    Man), 29% (Raging Bull — two full-width notes in a genuinely
    right-aligned block), 100% (Gilmore).
    """
    body = [ln for ln in lines if ln.text and not _is_watermark(ln.text)]
    if not body:
        return
    right_margin = max(ln.x1 for ln in body)
    # Centered lines are NOT excluded here: a full-width right-aligned stage
    # note reads as centered (equal margins by coincidence of spanning the
    # text width), and dropping those loses two real notes in Raging Bull.
    # The centered-block guard below handles them at document level instead.
    candidates = [ln for ln in body if not ln.bold]
    if not candidates:
        return
    near_edge = [ln for ln in candidates
                 if (right_margin - ln.x1) <= _RIGHT_EDGE_TOLERANCE_PT]
    if not near_edge:
        return
    if len(near_edge) / len(candidates) > _JUSTIFIED_DOC_RATIO_THRESHOLD:
        return
    centered_share = sum(1 for ln in near_edge if ln.centered) / len(near_edge)
    if centered_share >= _CENTERED_BLOCK_RATIO_THRESHOLD:
        return
    for ln in near_edge:
        ln.right_aligned = True


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
                # Computed for every line, not just bold ones: `_classify`
                # only consults it under `bold` (bold+centered = name), but
                # `_annotate_right_aligned_lines` needs it for unbold lines
                # to tell a center-justified paragraph from a right-aligned
                # one.
                centered = _is_centered(x0, x1, page_width)
                logical_text = clean(get_display(raw_text, base_dir='R'),
                                     fix_pdf_split_hebrew=True)
                out.append(LogicalLine(
                    text=logical_text.strip(),
                    bold=bold,
                    underlined=underlined,
                    centered=centered,
                    top=top, bottom=bottom, x0=x0, x1=x1, page=page_num,
                ))
    _annotate_right_aligned_lines(out)
    return out


def parse_pdf(pdf_path: str | Path) -> list[dict]:
    return lines_to_items(_extract_logical_lines(Path(pdf_path)))
