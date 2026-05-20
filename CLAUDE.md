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

The static UI (`app/static/index.html`) renders a full-width magenta `.hero` band (33vh tall) at the top, displaying `logo-chubbuck-wide.png` with `object-fit: cover; object-position: center`. The logo PNG was pre-padded to a 5.33:1 aspect ratio with the magenta background (`#C33552`) baked in, so `cover` crops only flat magenta on either side regardless of viewport width — both the wordmark and the Hebrew brand line stay centered and visible without white borders.

### Hebrew / RTL handling

- **PDF extraction (parser_pdf.py)**: pdfplumber gives chars in *visual* order without reliable space chars. We rebuild lines via `_reconstruct_line_text` (insert space wherever the x-gap > `_WORD_GAP_PT`), then run the text through `bidi.algorithm.get_display(..., base_dir='R')` to flip into logical order, then pass through `clean` regexes that fix punctuation spacing and stray-letter splits common to Hebrew PDFs.
- **DOCX extraction (parser_docx.py)**: bold/underline detection has three layers, in order: (1) **font name** — many real-world Hebrew screenplays don't set `<w:b>` at all and instead pick a heavy face directly (e.g. "Assistant ExtraBold"), so we check the run's `w:rFonts` for substrings like 'bold', 'black', 'heavy', 'extrab' (same heuristic as the PDF parser); (2) **direct toggles** read from raw XML — both `<w:b>` and the complex-script variant `<w:bCs>` that Hebrew runs use, since python-docx exposes only `w:b` (so a Hebrew-only doc with a real bold toggle reads as None across the board); (3) **inheritance** — walk `run.style` and `paragraph.style` chains via `base_style`. A line counts as bold/underlined only when *every* non-whitespace run carries the property.
- **Bracket repair**: some PDFs encode bracket pairs in visual order so they swap after bidi normalization; `_fix_reversed_brackets` detects this per-pair across the whole document.
- **Cell formatting (filler.py)**: every cell gets `Alignment(readingOrder=2, ...)` to force RTL reading order regardless of the cell's bidi base direction.
- **Classification**: `name = bold + underlined`, `stage_direction = bold only`, `dialogue = neither`. Underline detection scans `page.rects` and `page.lines` for thin horizontal strokes overlapping the line bbox; the search window straddles the baseline (`UNDERLINE_Y_ABOVE=8`, `UNDERLINE_Y_BELOW=4`) because PDFs place the underline above the bbox bottom.
- **Preamble trim** (`trim_preamble` in parser_common.py): scenes commonly start with a bold+underlined **scene title** (e.g. "כאב אמיתי") and one or more credit/metadata lines before the first character speaks. We must not render any of that. The trimmer anchors on the *first character name* — defined as the first `name` item that is followed (allowing intervening `stage_direction`s) by a `dialogue` item — and drops everything before it. A leading run of stage_directions immediately above that first character name is kept (those set the opening scene). Anchoring on "first dialogue, then walk back to a name" is wrong: the credits line under the title classifies as `dialogue`, so the title becomes the apparent first name and nothing gets trimmed.

### Page-layout tuning (filler.py — fragile, do not change casually)

The output must be landscape A4, narrow margins, with every page filled as densely as possible *and* names never separated from the dialogue line that follows them.

The strategy: **leave column widths alone, force an explicit print scale, and rely on the page-break math.** The template's columns A–M (including the empty merged zones G1:M1, G2:M3, E4:M4 that form the right side of the visible table) print at the explicit scale; manual page breaks handle vertical packing. Earlier iterations either widened E and force-hid G–M (broke the visible table layout) or kept the template's `fitToPage=True` (LibreOffice then auto-shrunk to ~42%, leaving a third of every page empty). Both approaches have been tried and rejected — see commit history.

Critical pieces:

- Print settings in `fill_template`: `orientation=landscape`, L/R margins `0.25"`, T/B margins `0.75"`, `pageSetUpPr.fitToPage=False`, `fitToWidth=None`, `fitToHeight=None`, `scale=_PRINT_SCALE_PCT` (currently 55).
- `_PRINT_SCALE_PCT = 55` was calibrated empirically. At narrow margins, the template's natural A–M width overflows the A4-landscape page width at scale 57+ on the current template — every page beyond that gets a phantom right-overflow page tacked on (just the M-column 'מכשולים' header band + a grid line). The header page in particular needs slightly more horizontal margin than data pages because the merged D2:E3 numbered-list cell and the E4:M4 label band have wider effective content. 55% leaves a 1pt safety margin below the empirical 56% threshold. **Symptom if this drifts:** page count exactly doubles — every content page comes paired with a near-empty right-stub page. Verify any scale change with a multi-page file and look for `pct < 1%` non-white pages in the output.
- **Do not modify column widths.** A–M stay exactly as the template defines them (A–G + M are explicit; H–L have no `column_dimensions` entry and inherit the default ~8.43, which is correct). Creating dim entries for H–L (even to set `hidden=True`) drifts the layout and shifts the safe scale.
- **`ws.print_area = f"A1:M{last_item_row}"`** is set in `fill_template`. Belt-and-suspenders with the scale fix: pins the right edge at column M and the bottom edge at the last filled row so LibreOffice has no excuse to emit a right-overflow or trailing-blank phantom page. Earlier iterations argued against setting this — that guidance is obsolete now that we're not hiding columns; leaving it set is harmless and defensive.
- **Do not re-enable `fitToPage`.** LibreOffice's xlsx fit-to-width with `fitToHeight=0` and our manual breaks ends up auto-scaling to ~42%, far smaller than the column-width math predicts — pages render with ~30% of vertical space empty. Forcing an explicit scale is the only way to get predictable vertical packing.
- `page_content_height_pt` (in `_layout_profile()`) is the per-page vertical budget. Theoretical max derivation: A4 landscape = 595pt tall, minus 0.75" T+B margins (108pt) = 487pt physical usable; at scale 55% that's 487 / 0.55 ≈ 886pt of logical row-height room per page. Carlito uses the full 886pt. **Noto uses 842pt** (~5% buffer) — even with the bumped Noto padding tiers, multi-line stage_directions were still rendering taller than the row-height estimate and clipping at the page bottom. The smaller budget triggers a manual page break earlier and absorbs the metric drift uniformly across all row kinds. If you change `_PRINT_SCALE_PCT`, recompute both profile values. `_set_page_breaks_keeping_name_with_dialogue` walks rows accumulating heights and inserts a manual `Break` *before* any row that would overflow, with a look-ahead that treats `name + dialogue` as one indivisible unit. **Drift symptom:** the last row of a page reappears alone at the top of a near-empty page — the walker is under-counting `HEADER_ROWS_HEIGHT_PT` or `page_content_height_pt`, letting one too many rows onto a page, and LO inserts its own break that pushes the orphan over. Verify `HEADER_ROWS_HEIGHT_PT = R1+R2+R3+R4` matches the actual template (R3 is post-`_spread_numbered_list_cell` 174, not the template's raw 111).
- Phantom-row cleanup: after filling, delete any `row_dimensions` entries beyond `last_item_row`; openpyxl can leave styled empty rows behind that LibreOffice renders as blank trailing pages. Note the template's actual `max_row` is **not** the visually obvious last row — use `ws.max_row` dynamically (see `_trim_or_extend_rows`).
- Row-height tiers (`_estimate_row_height`): height = `lines × _LINE_HEIGHT_PT` + per-(kind, length) padding. "Short" = ≤3 wrapped lines, "long" = ≥4 (the spec defines short as "≤2" and long as ">3"; the 3-line case falls into short by elimination). Padding tiers: name → 4pt; dialogue short → 4pt; dialogue long → 10pt; stage_direction short → 4pt; stage_direction long → 6pt. Wrap estimate is `len(text) / _CHARS_PER_LINE` per paragraph.
- Row-height/padding constants are **font-dependent** and split into two profiles, gated on `CELL_FONT_OVERRIDE` (see `_layout_profile` in `filler.py`):
  - **OFF (local dev, Carlito):** `_LINE_HEIGHT_PT=24`, `_CHARS_PER_LINE=50`, original small padding tiers. This is the tuning that produced the canonical reference output (`tests/הרומן שלי עם אנני - וודי אלן - עכביש - ניתוח.pdf`).
  - **ON (Lambda, Noto Sans Hebrew):** `_LINE_HEIGHT_PT=27`, `_CHARS_PER_LINE=46`, padding tiers bumped (name 4→6, dialogue short 4→6, dialogue long 10→14, stage short 4→6, stage long 6→10). Noto renders both taller AND wider than Carlito at the same point size; the OFF tuning undercounts both and caused (a) visibly cramped cells and (b) the last dialogue cell overflowing off the bottom margin of the page and getting clipped mid-sentence.
  - The `CELL_FONT_OVERRIDE` env override exists because LibreOffice on Debian was picking NotoSansDevanagari as the Hebrew fallback under Carlito and rendered tofu. Setting it to `Noto Sans Hebrew` fixes the glyph problem but introduces the metric drift that the second profile compensates for.
- **CELL_FONT_OVERRIDE must also pin `readingOrder=2` on every template cell.** The template's static labels (e.g. "נסיבות מקדימות:", "טקסט + אובייקטים פנימיים + סימון ביטים") rely on font-based bidi inference. Once we swap the font, that inference shifts and bidi-neutral characters drift to the wrong end — observed: the leading נ of "נסיבות" detaches and reads as "סיבות מקדימות"; the two `+` signs jump to the visual front, producing "+ + טקסט אובייקטים פנימיים סימון ביטים". The font-override loop in `fill_template` rewrites both `cell.font` and `cell.alignment` (preserving the existing horizontal/vertical/wrap/indent settings, only forcing `readingOrder=2`).
- Numbered-list header cell D2 (merged D2:E3) is patched by `_spread_numbered_list_cell`. The template uses `vertical=distributed` to spread `.1 / .2 / .3 / .4 / .5` evenly across the cell — Excel honors that, LibreOffice doesn't (lines cluster). The fix encodes the spacing in the text itself (blank line between each number), switches the cell to `vertical=top`, and bumps R3 to 174pt so the now-9-line content fits. `HEADER_ROWS_HEIGHT_PT = 324.75` (R1=52.5 + R2=42 + R3=174 + R4=56.25) matches the actual template heights — the page-break walker uses this as the page-1 starting offset. Drift symptom if this undercounts: orphan-last-row on a near-empty page.
- **Breathing blanks** (`_add_breathing_blanks` in filler.py, called at the very start of `fill_template`): inserts a `{"type": "blank"}` row at the top of every output (so content doesn't butt against the header), and one between the opening stage_direction block and the first character name when the scene opens on stage_directions. Blanks render as empty template-styled cells at `_MIN_ROW_HEIGHT_PT`. They count toward the page-break walker's row budget like any other row.

## Deployment

Live URL: https://d3jh9ygx0amvge.cloudfront.net/. Source of truth: `github.com/grassmeight/chubbuck`, branch `main`.

The CloudFront distribution (`ENJIWKRFZYLJD`) has **two origins**:
- Default behavior (`/`, `/index.html`, assets) → S3 bucket `chubbuck-static-344550433882`.
- Ordered behavior (`/api/*`, `/health`) → Lambda Function URL.

A push to `main` triggers `.github/workflows/deploy.yml`, which runs all four steps:
1. Build the Docker image (`Dockerfile.lambda`), push to ECR.
2. `aws lambda update-function-code` + wait for `Successful`.
3. `aws s3 sync app/static/` to the static bucket — `*.html` gets `Cache-Control: no-cache`, everything else gets `max-age=86400`. `--delete` cleans up renamed/removed assets.
4. `aws cloudfront create-invalidation --paths /*` (fire-and-forget; propagation 1–5 min).

Steps 3 and 4 were added 2026-05-19 (commit `c1e1d13`). Before that the workflow only updated Lambda, which meant any change under `app/static/` (or `index.html`) would deploy a fresh Lambda image but leave CloudFront serving the previous static origin — symptom: UI doesn't match what's in the repo even though the Actions run is green. **If you see this, the first place to look is the "Sync static assets to S3" / "Invalidate CloudFront cache" steps in the latest run** — Lambda update success ≠ user-visible UI update.

Auth is via OIDC: the IAM role `chubbuck-github-actions` (ARN in repo variable `AWS_ROLE_ARN`) trusts only `repo:grassmeight/chubbuck:ref:refs/heads/main`. The role has scoped permissions for ECR push, Lambda update on `chubbuck`, S3 PutObject/DeleteObject/ListBucket on the static bucket, and `cloudfront:CreateInvalidation` on this specific distribution — nothing else. Defined in `infra/github_oidc.tf`; trust target is set via `github_repo` in `infra/terraform.tfvars` (gitignored).

The S3 `uploads` bucket (`chubbuck-uploads-344550433882`) is separate and only reachable via the presigned-PUT flow in `app/main.py` (`/api/upload-url` + `/api/convert-s3`); the GitHub Actions role has **no** access to it.

## Conventions & Gotchas

- Development environment: Windows 11, PowerShell. Use PowerShell syntax in shell commands (`$env:VAR`, `;` for sequencing — `&&` is not available in Windows PowerShell 5.1).
- Python 3.14. Some packages need pinned older versions (notably `python-bidi<0.5`).
- Don't open the test xlsx (`טבלת ניתוחים ריקה.xlsx`, `_*.xlsx`) in Excel while running scripts — Excel's lock prevents openpyxl from saving.
- The classifier needs both bold *and* an underline rect to call something a name. If a PDF's underline is encoded as a rendered glyph rather than a rect/line, classification will mis-fire and every line will look like a stage direction. Verify with a single-page sample before debugging upstream.
- `parser.parse(path)` is the only entry point used by `main.py`; `parse_pdf` and `parse_docx` are re-exported from `parser.py` for tests but should not be wired into new callers — let the dispatcher handle suffix routing.
