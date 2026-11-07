# -*- coding: utf-8 -*-
"""Batch-convert scene files to analysis PDFs for manual review.

Usage:
    python batch_convert.py                    # every scene file in the repo root
    python batch_convert.py <dir-or-file>...   # specific sources
    python batch_convert.py --out "אישה וגבר" <dir>   # into a named subfolder

Output goes to "ניתוחים בדיקה/" next to this script (or a subfolder of it with
--out), one PDF per source named "<source stem> - ניתוח.pdf". A _report.txt in
the same folder lists, per file, the item counts and a per-page ink reading so
obviously-broken output (a near-blank page) is easy to spot before opening
anything.
"""
from __future__ import annotations

import io
import sys
import traceback
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).parent))

from app.parser import parse
from app.filler import (fill_template, _add_breathing_blanks, _layout_profile,
                        DATA_START_ROW, _MIN_ROW_HEIGHT_PT)
from app.pdf_export import xlsx_to_pdf

# A final page holding less than this share of the page budget is a widow the
# layout couldn't rescue. Tuned against the real defect (השור הזועם pre-fix:
# one row, 3% of budget) versus healthy short-dialogue endings (~25%).
# Raw ink% is NOT a usable signal here: a legitimate closing page of four
# two-word exchanges renders under 1% ink, same as a broken one.
_WIDOW_FILL_WARN = 0.15
# Ink this low means a genuinely empty page (grid lines only).
_BLANK_INK_PCT = 0.4

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "ניתוחים בדיקה"
TEMPLATE = ROOT / "טבלת ניתוחים ריקה.xlsx"
SUFFIXES = {".pdf", ".docx"}

# Repo-root files that aren't scene scripts: generated analyses, scratch output,
# the template itself, and screenshots.
def _is_source(p: Path) -> bool:
    return (p.suffix.lower() in SUFFIXES
            and not p.name.startswith("_")
            and "ניתוח" not in p.name
            and p.name != TEMPLATE.name)


def _page_ink(pdf_path: Path) -> list[float]:
    """Percent of non-white sampled pixels per page. A page under ~1% is blank."""
    try:
        import fitz
    except ImportError:
        return []
    out = []
    doc = fitz.open(pdf_path)
    for pn in range(doc.page_count):
        pix = doc[pn].get_pixmap(matrix=fitz.Matrix(0.5, 0.5))
        data, stride = pix.samples, pix.n
        sampled = max(1, (pix.width * pix.height) // 7)
        ink = sum(1 for i in range(0, len(data), stride * 7) if data[i] < 250)
        out.append(round(100.0 * ink / sampled, 2))
    doc.close()
    return out


def _page_fills(xlsx_path: Path, items: list[dict]) -> list[float]:
    """Each page's content height as a fraction of the per-page budget,
    read back from the manual breaks the filler wrote."""
    try:
        import openpyxl
    except ImportError:
        return []
    ws = openpyxl.load_workbook(xlsx_path).active
    rows = _add_breathing_blanks(items)
    heights = {DATA_START_ROW + i: (ws.row_dimensions[DATA_START_ROW + i].height
                                    or _MIN_ROW_HEIGHT_PT)
               for i in range(len(rows))}
    budget = _layout_profile()["page_content_height_pt"]
    brks = sorted(b.id for b in (ws.row_breaks.brk or []))
    bounds = [DATA_START_ROW - 1] + brks + [DATA_START_ROW + len(rows) - 1]
    return [round(sum(heights.get(r, 0.0) for r in range(a + 1, b + 1)) / budget, 3)
            for a, b in zip(bounds, bounds[1:])]


def collect(args: list[str]) -> list[Path]:
    if not args:
        return sorted((p for p in ROOT.iterdir() if _is_source(p)), key=lambda p: p.name)
    found: list[Path] = []
    for a in args:
        p = Path(a)
        if not p.is_absolute():
            p = ROOT / p
        if p.is_dir():
            found += sorted((q for q in p.rglob("*") if _is_source(q)), key=lambda q: q.name)
        elif p.is_file():
            found.append(p)
        else:
            print("  !! not found: %s" % p)
    return found


def main() -> int:
    args = sys.argv[1:]
    out_dir = OUT_DIR
    if args and args[0] == "--out":
        if len(args) < 2:
            print("--out needs a subfolder name")
            return 1
        out_dir = OUT_DIR / args[1]
        args = args[2:]
    out_dir.mkdir(parents=True, exist_ok=True)
    globals()["OUT_DIR"] = out_dir
    sources = collect(args)
    if not sources:
        print("No scene files found.")
        return 1

    lines: list[str] = []
    failures = 0
    suspect = 0
    print("Converting %d file(s) -> %s\n" % (len(sources), out_dir))
    for src in sources:
        out_pdf = out_dir / ("%s - ניתוח.pdf" % src.stem)
        tmp_xlsx = out_dir / ("%s - ניתוח.xlsx" % src.stem)
        try:
            items = parse(src)
            kinds: dict[str, int] = {}
            for it in items:
                kinds[it["type"]] = kinds.get(it["type"], 0) + 1
            fill_template(items, TEMPLATE, tmp_xlsx)
            xlsx_to_pdf(tmp_xlsx, out_pdf)
            fills = _page_fills(tmp_xlsx, items)
            tmp_xlsx.unlink(missing_ok=True)
            ink = _page_ink(out_pdf)
            warn = ""
            if not items:
                warn = "   <<< NO ITEMS PARSED"
            elif ink and min(ink) < _BLANK_INK_PCT:
                warn = "   <<< BLANK PAGE"
            elif len(fills) > 1 and fills[-1] < _WIDOW_FILL_WARN:
                warn = "   <<< WIDOW LAST PAGE (%.0f%% full)" % (100 * fills[-1])
            elif not kinds.get("name"):
                warn = "   <<< NO NAMES CLASSIFIED"
            if warn:
                suspect += 1
            row = ("%-52s %3d items  %-42s %d pages  fill=%s%s"
                   % (src.name[:52], len(items),
                      " ".join("%s=%d" % kv for kv in sorted(kinds.items())),
                      len(ink), [int(100 * f) for f in fills], warn))
        except Exception as exc:
            failures += 1
            row = "%-52s  FAILED: %s: %s" % (src.name[:52], type(exc).__name__, exc)
            traceback.print_exc()
        print("  " + row)
        lines.append(row)

    report = out_dir / "_report.txt"
    report.write_text(
        "Batch conversion report\n%s\n\n%s\n\n%d converted, %d failed, %d to check\n"
        % ("=" * 78, "\n".join(lines), len(sources) - failures, failures, suspect),
        encoding="utf-8")
    print("\n%d converted, %d failed, %d flagged for a closer look" %
          (len(sources) - failures, failures, suspect))
    print("report: %s" % report)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
