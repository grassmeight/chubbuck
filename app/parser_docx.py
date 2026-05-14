"""DOCX -> classified items.

Bold/underline detection for Hebrew docs is more involved than it looks:

- The most surprising case: many real-world Hebrew screenplays don't set
  `<w:b>` *at all* — the writer picks a heavier face directly (e.g. "Assistant
  ExtraBold" instead of "Assistant" + bold toggle), so visually-bold text has
  no bold property in the XML. We therefore also check the run's font name
  for bold-indicating substrings ('bold', 'black', 'heavy', 'extrab'), the
  same heuristic the PDF parser uses.

- python-docx's `run.font.bold` reads `<w:b>` and returns True/False/None
  (None = inherit from style). For Hebrew runs that *do* use a bold toggle,
  Word marks the text as complex-script and stores bold under `<w:bCs>`
  instead of `<w:b>` — and python-docx does not expose `bCs` at all. So we
  read both directly from the XML.

- Toggles may live on the run style or paragraph style rather than the run,
  so we walk both style chains as a fallback.

Underline has the same complex-script wrinkle (`<w:u w:val="single">` is the
common case; `w:val="none"` explicitly turns it off) plus inheritance.
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn

from app.parser_common import LogicalLine, clean, lines_to_items


def _toggle(elem) -> bool | None:
    """Read a Word toggle element (e.g. <w:b/>, <w:bCs/>).

    Returns True/False/None. None means 'not present at this level' so the
    caller can keep walking the inheritance chain.
    """
    if elem is None:
        return None
    val = elem.get(qn("w:val"))
    if val is None:
        return True
    return val.lower() in ("1", "true", "on")


def _underline_toggle(elem) -> bool | None:
    if elem is None:
        return None
    val = elem.get(qn("w:val"))
    if val is None:
        return True
    return val.lower() != "none"


def _bold_from_rpr(rpr) -> bool | None:
    """Direct bold property from an <w:rPr> element. Checks <w:b> first,
    then the complex-script variant <w:bCs> that Hebrew runs use."""
    if rpr is None:
        return None
    direct = _toggle(rpr.find(qn("w:b")))
    if direct is not None:
        return direct
    return _toggle(rpr.find(qn("w:bCs")))


def _underline_from_rpr(rpr) -> bool | None:
    if rpr is None:
        return None
    return _underline_toggle(rpr.find(qn("w:u")))


def _style_property(style, getter):
    """Walk a python-docx style + its base_style chain, returning the first
    non-None result from `getter(style)`."""
    while style is not None:
        val = getter(style)
        if val is not None:
            return val
        style = style.base_style
    return None


_BOLD_FONT_TOKENS = ("bold", "black", "heavy", "extrab")


def _font_name_is_bold(rpr) -> bool:
    """Heuristic: does the run's font name itself indicate weight (e.g. 'Assistant
    ExtraBold')? Many Hebrew docs set the heavy face directly without a bold toggle."""
    if rpr is None:
        return False
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        return False
    for attr in ("w:cs", "w:ascii", "w:hAnsi"):
        name = fonts.get(qn(attr))
        if name and any(tok in name.lower() for tok in _BOLD_FONT_TOKENS):
            return True
    return False


def _run_bold(run, paragraph) -> bool:
    rpr = run._element.find(qn("w:rPr"))
    if _font_name_is_bold(rpr):
        return True
    direct = _bold_from_rpr(rpr)
    if direct is not None:
        return direct
    val = _style_property(run.style, lambda s: _bold_from_rpr(
        s.element.find(qn("w:rPr"))))
    if val is not None:
        return val
    val = _style_property(paragraph.style, lambda s: _bold_from_rpr(
        s.element.find(qn("w:pPr/w:rPr")) if s.element.find(qn("w:pPr")) is not None
        else s.element.find(qn("w:rPr"))))
    return bool(val)


def _run_underlined(run, paragraph) -> bool:
    rpr = run._element.find(qn("w:rPr"))
    direct = _underline_from_rpr(rpr)
    if direct is not None:
        return direct
    val = _style_property(run.style, lambda s: _underline_from_rpr(
        s.element.find(qn("w:rPr"))))
    if val is not None:
        return val
    val = _style_property(paragraph.style, lambda s: _underline_from_rpr(
        s.element.find(qn("w:pPr/w:rPr")) if s.element.find(qn("w:pPr")) is not None
        else s.element.find(qn("w:rPr"))))
    return bool(val)


def _paragraph_centered(paragraph) -> bool:
    """True if the paragraph's effective alignment is center. Walks the
    paragraph style chain since Word often sets alignment on a 'Character'
    or screenplay style rather than the paragraph itself."""
    align = paragraph.alignment
    if align is not None:
        return align == WD_ALIGN_PARAGRAPH.CENTER
    val = _style_property(
        paragraph.style,
        lambda s: getattr(getattr(s, "paragraph_format", None), "alignment", None))
    return val == WD_ALIGN_PARAGRAPH.CENTER


def _extract_logical_lines(docx_path: Path) -> list[LogicalLine]:
    doc = Document(str(docx_path))
    out: list[LogicalLine] = []
    for para in doc.paragraphs:
        text = para.text
        if not text.strip():
            continue
        runs = [r for r in para.runs if r.text.strip()]
        if not runs:
            continue
        # A line counts as bold/underlined when *every* non-whitespace run
        # carries that property (matches the visual rule: a name is solidly
        # bold + underlined; mixed-format paragraphs are dialogue).
        bold = all(_run_bold(r, para) for r in runs)
        underlined = all(_run_underlined(r, para) for r in runs) if bold else False
        out.append(LogicalLine(
            text=clean(text.strip()),
            bold=bold,
            underlined=underlined,
            centered=_paragraph_centered(para),
        ))
    return out


def parse_docx(docx_path: str | Path) -> list[dict]:
    return lines_to_items(_extract_logical_lines(Path(docx_path)))
