"""Fill the xlsx template with classified items in column B.

Item type -> formatting:
  - name             -> bold + underline, vertical 'center'
  - stage_direction  -> bold,             horizontal 'right',  vertical 'distributed'
  - dialogue         -> regular,          horizontal 'center', vertical 'distributed'

All cells get explicit RTL reading order so mixed Hebrew/English stays
right-to-left regardless of Unicode bidi context.

Row height is computed from text length (approximate wrap calculation) plus
a 10pt padding so the cell never crops.
"""
from __future__ import annotations

import math
from copy import copy
from pathlib import Path
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font
from openpyxl.worksheet.pagebreak import Break

TARGET_COLUMN = 2  # column B
DATA_START_ROW = 5
TEMPLATE_LAST_ROW = 125
# Header is rows 1-4. R3 is bumped from the template's 111pt to 174pt by
# _spread_numbered_list_cell (see there for why). Total header occupies the
# top of page 1 and is subtracted from page 1's data budget in the page-break
# walker; page 2+ get the full _PAGE_CONTENT_HEIGHT_PT.
# R1=27 + R2=42 + R3=174 + R4=56.25 = 299.25pt
HEADER_ROWS_HEIGHT_PT = 299.25
_NUMBERED_LIST_R3_HEIGHT_PT = 174.0

# Print scale (whole percent). Page setup forces this exact scale (rather
# than fit-to-width auto-scale) so the per-page vertical budget below is
# predictable. Calibrated empirically: at narrow margins (0.25" L/R), the
# template's natural column widths (A-M, no widening, no hiding) start
# overflowing the A4-landscape page width somewhere between scale 61 and 67;
# 61% leaves a small horizontal safety margin. If you change this, also
# update _PAGE_CONTENT_HEIGHT_PT (= 487 / scale).
_PRINT_SCALE_PCT = 61

# Per-page vertical budget for content rows. A4 landscape is 595pt tall;
# with 0.75" T+B margins (108pt total) the usable height is 487pt physical.
# At _PRINT_SCALE_PCT=61%, that's 487 / 0.61 ≈ 798pt of logical row-height
# room per page. The page-break walker uses this as the hard cap and inserts
# manual breaks before any row that would overflow.
_PAGE_CONTENT_HEIGHT_PT = 798.0

# Empirical layout numbers tuned to the template (Calibri 16, column B width
# 52.71). Hebrew text at this column width and font size wraps at roughly this
# many characters per visual line.
_CHARS_PER_LINE = 50
_LINE_HEIGHT_PT = 21.0
_HEIGHT_PADDING_PT = 10.0          # long rows (3+ wrapped lines)
_SHORT_HEIGHT_PADDING_PT = 8.0     # short non-name rows (<= 2 wrapped lines)
_NAME_HEIGHT_PADDING_PT = 5.0      # name rows
_MIN_ROW_HEIGHT_PT = 21.0
# Long dialogue blocks visually crowd the cell; bump padding for 5+ wrapped
# lines so the text breathes.
_LONG_DIALOGUE_LINE_THRESHOLD = 5
_LONG_DIALOGUE_EXTRA_PADDING_PT = 5.0


def _estimate_row_height(text: str, kind: str) -> float:
    """Estimate the visual row height in points, given the wrapped text.

    Counts explicit newlines plus an approximate wrap based on character count.
    Padding tiers:
      - name              ->  5pt (always single line)
      - short row         ->  8pt (<= 2 wrapped lines)
      - long row          -> 10pt (3+ wrapped lines)
      - long dialogue     -> +5pt extra (5+ wrapped lines of dialogue)
    """
    if not text:
        return _MIN_ROW_HEIGHT_PT
    paragraphs = text.split("\n")
    total_lines = 0
    for p in paragraphs:
        total_lines += max(1, math.ceil(len(p) / _CHARS_PER_LINE))
    if kind == "name":
        padding = _NAME_HEIGHT_PADDING_PT
    elif total_lines <= 2:
        padding = _SHORT_HEIGHT_PADDING_PT
    else:
        padding = _HEIGHT_PADDING_PT
    if kind == "dialogue" and total_lines >= _LONG_DIALOGUE_LINE_THRESHOLD:
        padding += _LONG_DIALOGUE_EXTRA_PADDING_PT
    height = total_lines * _LINE_HEIGHT_PT + padding
    return max(_MIN_ROW_HEIGHT_PT, height)


def _alignment_for(kind: str, base: Alignment) -> Alignment:
    if kind == "name":
        h, v = "center", "top"
    elif kind == "stage_direction":
        h, v = "right", "distributed"
    else:  # dialogue
        h, v = "center", "distributed"
    return Alignment(
        horizontal=h,
        vertical=v,
        wrap_text=True,
        readingOrder=2,  # explicit RTL, ignore Unicode bidi base direction
        indent=base.indent or 0,
    )


def _trim_or_extend_rows(ws, last_item_row: int) -> None:
    """Match the table size to the actual item count.

    Uses the worksheet's *actual* max_row rather than a hardcoded constant —
    Excel/openpyxl can leave styled-but-empty cells well past the visible
    data area, which LibreOffice then renders as ghost rows on a trailing
    blank page if we don't remove them.
    """
    actual_last_row = ws.max_row
    if last_item_row < actual_last_row:
        ws.delete_rows(last_item_row + 1, actual_last_row - last_item_row)
    elif last_item_row > actual_last_row:
        src_row = actual_last_row
        for new_row in range(actual_last_row + 1, last_item_row + 1):
            for col in range(1, 6):  # A..E
                src = ws.cell(row=src_row, column=col)
                dst = ws.cell(row=new_row, column=col)
                dst.border = copy(src.border)
                dst.alignment = copy(src.alignment)
                dst.font = copy(src.font)
                dst.fill = copy(src.fill)


def _set_page_breaks_keeping_name_with_dialogue(ws, items: list[dict]) -> None:
    """Walk the data rows accumulating heights and emit a manual page break
    before every row that would overflow the (conservative) page budget.

    Special case: a name + its following dialogue are treated as one
    indivisible unit, so we never put a manual break between them. If the unit
    doesn't fit on the current page we break *before* the name.

    Because we add manual breaks at every predicted overflow point and the
    page budget is intentionally smaller than LibreOffice's real budget,
    LibreOffice won't insert any natural page breaks of its own — so the page
    break pattern is fully deterministic from this calculation.
    """
    breaks: list[int] = []  # row numbers AFTER which to break
    current_y = HEADER_ROWS_HEIGHT_PT  # page 1 starts with the header

    i = 0
    while i < len(items):
        item = items[i]
        row = DATA_START_ROW + i
        h = ws.row_dimensions[row].height or _MIN_ROW_HEIGHT_PT

        # If this row is a name and the next is dialogue, plan them as a unit.
        unit_h = h
        unit_size = 1
        if (item["type"] == "name"
                and i + 1 < len(items)
                and items[i + 1]["type"] == "dialogue"):
            next_row = row + 1
            next_h = ws.row_dimensions[next_row].height or _MIN_ROW_HEIGHT_PT
            unit_h = h + next_h
            unit_size = 2

        if current_y + unit_h > _PAGE_CONTENT_HEIGHT_PT and current_y > HEADER_ROWS_HEIGHT_PT:
            # Doesn't fit on this page -> break before this row.
            breaks.append(row - 1)
            current_y = unit_h
        else:
            current_y += unit_h

        i += unit_size

    if breaks:
        ws.row_breaks.brk = [Break(id=b, man=True) for b in breaks]


def _spread_numbered_list_cell(ws) -> None:
    """The header cell D2 (merged D2:E3) holds an enumerated list rendered
    with vertical=distributed. Excel honors that and spreads the 5 lines
    evenly across the cell; LibreOffice does not, so the lines cluster
    together in the PDF export.

    Fix: insert a blank line between each numbered item so the spacing is
    encoded in the text itself (renderer-independent), switch to
    vertical=top so neither renderer tries to distribute, and bump R3 tall
    enough to fit the now-9-line content. R3 grows by ~63pt; that comes out
    of page 1's data budget (HEADER_ROWS_HEIGHT_PT subtracted from
    _PAGE_CONTENT_HEIGHT_PT in the page-break walker) — about 3 fewer rows
    on page 1. Subsequent pages are unaffected.
    """
    cell = ws.cell(row=2, column=4)  # D2 (merged across D2:E3)
    raw = cell.value
    if not raw or "\n" not in str(raw):
        return
    lines = [ln for ln in str(raw).split("\n") if ln.strip()]
    cell.value = "\n\n".join(lines)
    cell.alignment = Alignment(
        horizontal=cell.alignment.horizontal or "right",
        vertical="top",
        wrap_text=True,
        readingOrder=cell.alignment.readingOrder or 0,
        indent=cell.alignment.indent or 0,
    )
    ws.row_dimensions[3].height = _NUMBERED_LIST_R3_HEIGHT_PT


def _prepend_leading_blanks_if_no_intro(items: list[dict]) -> list[dict]:
    """Scenes that open straight on a name (no opening stage direction) look
    cramped against the header. Reserve the first two rows as empty padding
    in that case. Items prepended here are rendered as empty cells with the
    template's default formatting (borders, RTL) and minimum row height.
    """
    if items and items[0]["type"] == "name":
        return [{"type": "blank", "text": ""},
                {"type": "blank", "text": ""},
                *items]
    return items


def fill_template(items: list[dict], template_path: str | Path,
                  output_path: str | Path) -> None:
    items = _prepend_leading_blanks_if_no_intro(items)
    wb = load_workbook(template_path)
    ws = wb.active

    # Print settings: landscape A4, narrow margins, explicit scale.
    #
    # Why scale=PRINT_SCALE / fitToPage=False (NOT auto-fit-to-width):
    # With fitToPage=True + fitToHeight=0, LibreOffice picks an auto-scale
    # that's much smaller than expected (empirically ~42% in our test files,
    # not the ~66% the column-width math predicts). The result is pages with
    # huge bottom margins — see commit history / CLAUDE.md note about
    # "shrinking to oblivion". Forcing an explicit scale gives predictable
    # output where _PAGE_CONTENT_HEIGHT_PT actually matches the per-page
    # vertical capacity. The template's natural column widths fit the
    # narrow-margin A4-landscape page width at this scale.
    ws.page_setup.orientation = "landscape"
    ws.page_margins.left = 0.25
    ws.page_margins.right = 0.25
    ws.page_margins.top = 0.75
    ws.page_margins.bottom = 0.75
    ws.sheet_properties.pageSetUpPr.fitToPage = False
    ws.page_setup.fitToWidth = None
    ws.page_setup.fitToHeight = None
    ws.page_setup.scale = _PRINT_SCALE_PCT

    style_cell = ws.cell(row=DATA_START_ROW, column=TARGET_COLUMN)
    base_font = style_cell.font
    base_align = style_cell.alignment

    _spread_numbered_list_cell(ws)

    last_item_row = DATA_START_ROW + len(items) - 1
    _trim_or_extend_rows(ws, last_item_row)

    for i, item in enumerate(items):
        row = DATA_START_ROW + i
        kind = item["type"]
        if kind == "blank":
            # Leave the cell with the template's own formatting; just fix the
            # row height so these reserved rows don't balloon vertically.
            ws.row_dimensions[row].height = _MIN_ROW_HEIGHT_PT
            continue
        cell = ws.cell(row=row, column=TARGET_COLUMN)
        bold = kind in ("name", "stage_direction")
        underline = "single" if kind == "name" else None

        cell.value = item["text"]
        cell.font = Font(
            name=base_font.name or "Calibri",
            size=base_font.size or 16,
            bold=bold,
            underline=underline,
            color=base_font.color,
        )
        cell.alignment = _alignment_for(kind, base_align)
        ws.row_dimensions[row].height = _estimate_row_height(item["text"], kind)

    _set_page_breaks_keeping_name_with_dialogue(ws, items)

    # openpyxl can leave row_dimensions entries past the filled range
    # (internal bookkeeping during delete_rows / style copies). LibreOffice
    # renders those as ghost rows at default height -> trailing blank pages.
    for r in list(ws.row_dimensions.keys()):
        if r > last_item_row:
            del ws.row_dimensions[r]

    wb.save(output_path)
