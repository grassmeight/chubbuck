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


def _style_rpr(style):
    """Return the run-properties element that applies to runs in this style.
    Paragraph styles nest it at w:pPr/w:rPr; character styles expose w:rPr
    directly. qn() only handles single namespaced tags, so we walk by hand."""
    elem = style.element
    p_pr = elem.find(qn("w:pPr"))
    if p_pr is not None:
        nested = p_pr.find(qn("w:rPr"))
        if nested is not None:
            return nested
    return elem.find(qn("w:rPr"))


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
    val = _style_property(paragraph.style, lambda s: _bold_from_rpr(_style_rpr(s)))
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
    val = _style_property(paragraph.style, lambda s: _underline_from_rpr(_style_rpr(s)))
    return bool(val)


def _effective_alignment(paragraph):
    """Effective paragraph alignment (WD_ALIGN_PARAGRAPH.* or None). Walks the
    paragraph style chain since Word often sets alignment on a 'Character' or
    screenplay style rather than the paragraph itself. None means no alignment
    is set anywhere — which for an RTL Hebrew paragraph renders right-aligned."""
    align = paragraph.alignment
    if align is not None:
        return align
    return _style_property(
        paragraph.style,
        lambda s: getattr(getattr(s, "paragraph_format", None), "alignment", None))


def _paragraph_centered(paragraph) -> bool:
    """True if the paragraph's effective alignment is center."""
    return _effective_alignment(paragraph) == WD_ALIGN_PARAGRAPH.CENTER


def _paragraph_right_default(paragraph) -> bool:
    """True if the paragraph sits at the right text margin: alignment is
    explicitly RIGHT or is unset (None) — an unset RTL Hebrew paragraph
    renders right-aligned by default. LEFT/JUSTIFY are excluded; those aren't
    the right-margin signal we use to spot unbolded stage notes.

    In Hebrew screenplays that don't bold their stage notes, dialogue and
    character names are explicitly centered while stage directions carry no
    alignment (so they fall to the RTL default, the right margin). This is the
    DOCX analog of the PDF parser's right-edge geometry (see `right_aligned`
    on LogicalLine)."""
    return _effective_alignment(paragraph) in (None, WD_ALIGN_PARAGRAPH.RIGHT)


def _split_paragraph_into_segments(para):
    """Split a paragraph at soft line breaks into segments, each with its
    own per-run formatting.

    Real-world Hebrew screenplays from Word commonly encode each speaker
    turn as a single paragraph shaped like
        <bold+underlined name run>  <w:br/>  <regular dialogue run>
    The <w:br/> is exposed by python-docx as a literal '\n' inside the run
    text. Treating the whole paragraph as one logical line collapses the
    name into the dialogue (and loses its bold+underline classification),
    so we split on those soft breaks before classification.

    Returns a list of segments; each segment is a list of (text_chunk,
    bold, underlined) tuples.
    """
    segments: list[list[tuple[str, bool, bool]]] = []
    current: list[tuple[str, bool, bool]] = []

    def flush() -> None:
        if current:
            segments.append(list(current))
        current.clear()

    for run in para.runs:
        if not run.text:
            continue
        b = _run_bold(run, para)
        u = _run_underlined(run, para)
        chunks = run.text.split("\n")
        for i, chunk in enumerate(chunks):
            if i > 0:
                flush()
            if chunk:
                current.append((chunk, b, u))
    flush()
    return segments


# If more than this fraction of non-bold lines sit at the right margin, the
# document right-aligns its dialogue too (so right-alignment can no longer tell
# stage notes from dialogue) and geometric promotion is skipped entirely. Same
# threshold and rationale as parser_pdf's justified-doc guard.
_JUSTIFIED_DOC_RATIO_THRESHOLD = 0.25


def _annotate_right_aligned_lines(lines: list[LogicalLine]) -> None:
    """Promote unbolded right-margin lines to stage_direction candidates.

    A line was provisionally flagged `right_aligned` during extraction when it
    was not bold, not centered, and its paragraph sat at the right margin (see
    `_paragraph_right_default`). Here we apply the justified-document guard: if
    those flagged lines are more than `_JUSTIFIED_DOC_RATIO_THRESHOLD` of the
    non-bold body, the document right-aligns dialogue too and the signal is
    worthless — clear every flag so dialogue isn't scattered into stage notes.
    Otherwise the flags stand and `_classify` treats them as stage_direction."""
    candidates = [ln for ln in lines if ln.text and not ln.bold]
    if not candidates:
        return
    flagged = [ln for ln in candidates if ln.right_aligned]
    if len(flagged) / len(candidates) > _JUSTIFIED_DOC_RATIO_THRESHOLD:
        for ln in flagged:
            ln.right_aligned = False


def _extract_logical_lines(docx_path: Path) -> list[LogicalLine]:
    doc = Document(str(docx_path))
    out: list[LogicalLine] = []
    for para in doc.paragraphs:
        if not para.text.strip():
            continue
        centered = _paragraph_centered(para)
        right_default = not centered and _paragraph_right_default(para)
        for segment in _split_paragraph_into_segments(para):
            text = "".join(c for c, _, _ in segment).strip()
            if not text:
                continue
            # A segment counts as bold/underlined when every chunk that
            # contributed non-whitespace text carries that property.
            non_ws = [(c, b, u) for c, b, u in segment if c.strip()]
            bold = all(b for _, b, _ in non_ws) if non_ws else False
            underlined = all(u for _, _, u in non_ws) if (bold and non_ws) else False
            out.append(LogicalLine(
                text=clean(text),
                bold=bold,
                underlined=underlined,
                centered=centered,
                # Only unbolded right-margin lines are stage-note candidates;
                # bold lines already have their own (name/stage) classification.
                right_aligned=right_default and not bold,
            ))
    _annotate_right_aligned_lines(out)
    return out


def parse_docx(docx_path: str | Path) -> list[dict]:
    return lines_to_items(_extract_logical_lines(Path(docx_path)))
