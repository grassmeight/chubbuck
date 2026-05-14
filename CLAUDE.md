# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Web app that turns a Hebrew dialogue scene (PDF or DOCX) into a filled-in analysis spreadsheet, exported as PDF. Drag-and-drop UI; no persistent storage. Each request is parsed, classified into name / stage-direction / dialogue items, written into the template `טבלת ניתוחים ריקה.xlsx` (column B), and converted to PDF via headless LibreOffice.

## Commands

Activate the venv first: `.\.venv\Scripts\Activate.ps1`

- Install deps: `pip install -r requirements.txt`
  - **Important:** `requirements.txt` is currently incomplete — `parser.py` also imports `pdfplumber` and `python-bidi` (use `python-bidi<0.5`; 0.6+ requires Rust toolchain on Python 3.14). Install them explicitly when setting up a fresh env, and add to `requirements.txt` before deploying.
- Run dev server (hot-reload): `$env:DEV_KEEP_OUTPUTS=1; uvicorn app.main:app --reload --host 127.0.0.1 --port 8000`
  - Open http://127.0.0.1:8000/ for the UI; POST to `/api/convert`; `/health` for liveness.
  - **Always run with `--reload`** during development. Without it the server keeps serving old code after edits, which has burned multiple debugging sessions.
  - `DEV_KEEP_OUTPUTS=1` makes every successful conversion drop a copy of the filled `.xlsx` and the produced `.pdf` into `tests/`, named after the input file's stem. Lets you inspect what LibreOffice was fed vs. what it produced when the PDF looks wrong but the xlsx looks fine.
- Quick parser+filler+export sanity check: `python debug_parser.py` (regenerates `_w7_*.xlsx` / `.pdf` / `.png` for the two sample files in the repo root).
- End-to-end HTTP test (server must be running): `python test_e2e.py`
- Per-stage tests: `test_parser.py`, `test_filler.py`, `test_pdf_export.py` — each is a plain script run via `python <name>.py`, not pytest.

LibreOffice (`soffice`) must be installed and discoverable. `app/pdf_export.py` searches `SOFFICE_BIN` env var → `PATH` → standard Win/Linux/macOS install paths, in that order.

## Architecture

Pipeline (one request flows top to bottom; no shared state):

1. **`app/main.py`** — FastAPI app. `/api/convert` streams the upload into a per-request `TemporaryDirectory` (25 MB cap, extension whitelist), invokes the pipeline, returns the PDF as a `FileResponse` with a `BackgroundTask` that wipes the tmpdir after the response is sent. Static UI is mounted at `/` *after* the API routes so `/api/*` and `/health` win.
2. **`app/parser.py`** — Thin dispatcher that routes by file suffix to:
   - **`app/parser_pdf.py`** — pdfplumber + bidi extraction.
   - **`app/parser_docx.py`** — python-docx extraction (with raw-XML fallbacks for Hebrew complex-script bold).
   - **`app/parser_common.py`** — shared types (`LogicalLine`) + post-extraction pipeline (`_classify`, `group_into_items`, `trim_preamble`, `fix_reversed_brackets`, `clean`).

   Both format parsers produce `list[LogicalLine]` and feed it through the same `lines_to_items` pipeline → `list[{"type": "name"|"stage_direction"|"dialogue", "text": str}]`.
3. **`app/filler.py`** — Items → filled `.xlsx` written to disk. Computes per-row heights, applies fonts/alignment, and emits manual page breaks.
4. **`app/pdf_export.py`** — `.xlsx` → `.pdf` via `soffice --headless --convert-to pdf`. Each invocation uses an isolated `UserInstallation` profile so concurrent requests don't collide.

The template (`טבלת ניתוחים ריקה.xlsx`) lives at the repo root and is the single source of truth for header rows, fonts, and column widths. Data starts at row 5, column B; columns A/C/D/F hold static labels and grid lines; E is widened at fill time to absorb leftover horizontal space.

### Hebrew / RTL handling

- **PDF extraction (parser_pdf.py)**: pdfplumber gives chars in *visual* order without reliable space chars. We rebuild lines via `_reconstruct_line_text` (insert space wherever the x-gap > `_WORD_GAP_PT`), then run the text through `bidi.algorithm.get_display(..., base_dir='R')` to flip into logical order, then pass through `clean` regexes that fix punctuation spacing and stray-letter splits common to Hebrew PDFs.
- **DOCX extraction (parser_docx.py)**: bold/underline detection has three layers, in order: (1) **font name** — many real-world Hebrew screenplays don't set `<w:b>` at all and instead pick a heavy face directly (e.g. "Assistant ExtraBold"), so we check the run's `w:rFonts` for substrings like 'bold', 'black', 'heavy', 'extrab' (same heuristic as the PDF parser); (2) **direct toggles** read from raw XML — both `<w:b>` and the complex-script variant `<w:bCs>` that Hebrew runs use, since python-docx exposes only `w:b` (so a Hebrew-only doc with a real bold toggle reads as None across the board); (3) **inheritance** — walk `run.style` and `paragraph.style` chains via `base_style`. A line counts as bold/underlined only when *every* non-whitespace run carries the property.
- **Bracket repair**: some PDFs encode bracket pairs in visual order so they swap after bidi normalization; `_fix_reversed_brackets` detects this per-pair across the whole document.
- **Cell formatting (filler.py)**: every cell gets `Alignment(readingOrder=2, ...)` to force RTL reading order regardless of the cell's bidi base direction.
- **Classification**: `name = bold + underlined`, `stage_direction = bold only`, `dialogue = neither`. Underline detection scans `page.rects` and `page.lines` for thin horizontal strokes overlapping the line bbox; the search window straddles the baseline (`UNDERLINE_Y_ABOVE=8`, `UNDERLINE_Y_BELOW=4`) because PDFs place the underline above the bbox bottom.
- **Preamble trim** (`_trim_preamble`): drops everything before the first dialogue item, except a leading run of stage_directions immediately above the first character name.

### Page-layout tuning (filler.py — fragile, do not change casually)

The output must be landscape A4, narrow margins, with every page filled as densely as possible *and* names never separated from the dialogue line that follows them.

The strategy: **leave column widths alone, force an explicit print scale, and rely on the page-break math.** The template's columns A–M (including the empty merged zones G1:M1, G2:M3, E4:M4 that form the right side of the visible table) print at the explicit scale; manual page breaks handle vertical packing. Earlier iterations either widened E and force-hid G–M (broke the visible table layout) or kept the template's `fitToPage=True` (LibreOffice then auto-shrunk to ~42%, leaving a third of every page empty). Both approaches have been tried and rejected — see commit history.

Critical pieces:

- Print settings in `fill_template`: `orientation=landscape`, L/R margins `0.25"`, T/B margins `0.75"`, `pageSetUpPr.fitToPage=False`, `fitToWidth=None`, `fitToHeight=None`, `scale=_PRINT_SCALE_PCT` (currently 61).
- `_PRINT_SCALE_PCT = 61` was calibrated empirically: at narrow margins, the template's natural A–M width starts overflowing the A4-landscape page somewhere between 61 and 67%. 61% leaves a small horizontal safety margin; raising it past ~62 causes LibreOffice to print the rightmost columns on a 2nd horizontal page (page count doubles).
- **Do not modify column widths.** A–M stay exactly as the template defines them (A–G + M are explicit; H–L have no `column_dimensions` entry and inherit the default ~8.43, which is correct). Creating dim entries for H–L (even to set `hidden=True`) drifts the layout and shifts the safe scale.
- **Do not set `ws.print_area`.** Not needed once we're not hiding columns.
- **Do not re-enable `fitToPage`.** LibreOffice's xlsx fit-to-width with `fitToHeight=0` and our manual breaks ends up auto-scaling to ~42%, far smaller than the column-width math predicts — pages render with ~30% of vertical space empty. Forcing an explicit scale is the only way to get predictable vertical packing.
- `_PAGE_CONTENT_HEIGHT_PT = 798.0` is the per-page vertical budget. Derivation: A4 landscape = 595pt tall, minus 0.75" T+B margins (108pt) = 487pt physical usable; at scale 61% that's 487 / 0.61 ≈ 798pt of logical row-height room per page. If you change `_PRINT_SCALE_PCT`, recompute this. `_set_page_breaks_keeping_name_with_dialogue` walks rows accumulating heights and inserts a manual `Break` *before* any row that would overflow, with a look-ahead that treats `name + dialogue` as one indivisible unit.
- Phantom-row cleanup: after filling, delete any `row_dimensions` entries beyond `last_item_row`; openpyxl can leave styled empty rows behind that LibreOffice renders as blank trailing pages. Note the template's actual `max_row` is **not** the visually obvious last row — use `ws.max_row` dynamically (see `_trim_or_extend_rows`).
- Row-height tiers (`_estimate_row_height`): names get 5pt padding, short rows (≤2 wrapped lines) get 8pt, long rows get 10pt. Wrap estimate is `len(text) / _CHARS_PER_LINE` per paragraph.
- Numbered-list header cell D2 (merged D2:E3) is patched by `_spread_numbered_list_cell`. The template uses `vertical=distributed` to spread `.1 / .2 / .3 / .4 / .5` evenly across the cell — Excel honors that, LibreOffice doesn't (lines cluster). The fix encodes the spacing in the text itself (blank line between each number), switches the cell to `vertical=top`, and bumps R3 to 174pt so the now-9-line content fits. `HEADER_ROWS_HEIGHT_PT` is updated to match (299.25pt) so the page-break math accounts for the taller header on page 1.

## Conventions & Gotchas

- Development environment: Windows 11, PowerShell. Use PowerShell syntax in shell commands (`$env:VAR`, `;` for sequencing — `&&` is not available in Windows PowerShell 5.1).
- Python 3.14. Some packages need pinned older versions (notably `python-bidi<0.5`).
- Don't open the test xlsx (`טבלת ניתוחים ריקה.xlsx`, `_*.xlsx`) in Excel while running scripts — Excel's lock prevents openpyxl from saving.
- The classifier needs both bold *and* an underline rect to call something a name. If a PDF's underline is encoded as a rendered glyph rather than a rect/line, classification will mis-fire and every line will look like a stage direction. Verify with a single-page sample before debugging upstream.
- `parser.parse(path)` is the only entry point used by `main.py`; `parse_pdf` and `parse_docx` are re-exported from `parser.py` for tests but should not be wired into new callers — let the dispatcher handle suffix routing.
