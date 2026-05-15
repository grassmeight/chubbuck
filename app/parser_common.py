"""Shared types and pipeline for the PDF and DOCX parsers.

Both `parser_pdf` and `parser_docx` produce a `list[LogicalLine]` and feed it
through the same downstream pipeline: classify -> group -> trim preamble ->
repair reversed brackets.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ItemType = Literal["name", "dialogue", "stage_direction"]

# Hebrew letter range (no final-form-only logic needed; final forms are in the range).
_HEB = r"֐-׿"

# Always-safe cleanup: punctuation spacing. Applies to both PDF and DOCX text.
_PUNCT_RULES: list[tuple[re.Pattern, str]] = [
    # 1) Remove space before sentence punctuation.
    (re.compile(r"\s+([!?.,;:])"), r"\1"),
    # 2) Remove space before a closing quote that is itself followed by punctuation,
    #    whitespace, or end of string. Handles 'קיץ "?' -> 'קיץ"?'.
    (re.compile(r"\s+\"(?=[!?.,;:\s]|$)"), '"'),
]

# PDF-only cleanup: pdfplumber sometimes emits single Hebrew letters separated
# from their word by a stray space (the bidi/spacing reconstruction in
# parser_pdf gets close but not perfect). These rules re-attach prefix/suffix
# letters that lost their connection.
#
# DOCX text MUST NOT use these rules: they wrongly delete legitimate spaces
# whenever a Hebrew word ends in a non-Hebrew char like an ASCII apostrophe
# (which is how Hebrew names like בנג'י, ג'וינט are commonly written), making
# `י` or `ם` look like a "stranded" single letter that should be merged.
_PDF_HEBREW_REJOIN_RULES: list[tuple[re.Pattern, str]] = [
    # 3) Merge a single Hebrew letter that lost its connection to the next Hebrew word
    #    (Hebrew prefix like ל-, ב-, מ-, ש-, ה-, ו- attaches without a space).
    (re.compile(rf"(?<![{_HEB}])([{_HEB}])\s+([{_HEB}]+)"), r"\1\2"),
    # 4) Merge a single Hebrew letter that lost its connection to the previous Hebrew
    #    word (final letters like ם, ן, ה, ו attaching as suffix).
    (re.compile(rf"([{_HEB}]+)\s+([{_HEB}])(?![{_HEB}])"), r"\1\2"),
]


def clean(text: str, *, fix_pdf_split_hebrew: bool = False) -> str:
    """Fix extraction artifacts.

    Always: stray spaces before punctuation/closing quotes.
    With fix_pdf_split_hebrew=True: also re-attach single Hebrew letters
    that lost their word connection — pass True only for PDF-extracted text.
    """
    rules = _PUNCT_RULES + (_PDF_HEBREW_REJOIN_RULES if fix_pdf_split_hebrew else [])
    prev = None
    cur = text
    while cur != prev:
        prev = cur
        for pattern, repl in rules:
            cur = pattern.sub(repl, cur)
    return cur


@dataclass
class LogicalLine:
    text: str  # Unicode logical order
    bold: bool
    underlined: bool
    centered: bool = False
    # Geometry fields are PDF-only; DOCX leaves them at zero.
    top: float = 0.0
    bottom: float = 0.0
    x0: float = 0.0
    x1: float = 0.0
    page: int = 0


# Some scenes use bold + centered (no underline) for names. Cap on word count
# avoids misclassifying centered bold stage directions as names.
_NAME_CENTERED_WORD_LIMIT = 4


def _classify(line: LogicalLine) -> ItemType:
    if line.bold and line.underlined:
        return "name"
    if (line.bold and line.centered
            and len(line.text.split()) <= _NAME_CENTERED_WORD_LIMIT):
        return "name"
    if line.bold:
        return "stage_direction"
    return "dialogue"


# Watermark/footer the source documents carry (author contact line). Filter
# any logical line containing this token; safe because legitimate Hebrew
# screenplay content won't include the Latin word "chubbuck".
_WATERMARK_TOKEN = "chubbuck"


def _is_watermark(text: str) -> bool:
    return _WATERMARK_TOKEN in text.lower()


def group_into_items(lines: list[LogicalLine]) -> list[dict]:
    """Apply the chunking rule:
       - Each name -> own item
       - Consecutive dialogue lines -> merged item
       - Consecutive stage_direction lines -> merged item
    """
    items: list[dict] = []
    dialogue_buffer: list[str] = []
    stage_buffer: list[str] = []

    def flush(buffer: list[str], kind: str):
        if buffer:
            merged = " ".join(s for s in buffer if s.strip())
            if merged.strip():
                items.append({"type": kind, "text": merged})
            buffer.clear()

    for line in lines:
        if not line.text:
            continue
        kind = _classify(line)
        if kind == "name":
            flush(dialogue_buffer, "dialogue")
            flush(stage_buffer, "stage_direction")
            items.append({"type": "name", "text": line.text})
        elif kind == "stage_direction":
            flush(dialogue_buffer, "dialogue")
            stage_buffer.append(line.text)
        else:
            flush(stage_buffer, "stage_direction")
            dialogue_buffer.append(line.text)
    flush(dialogue_buffer, "dialogue")
    flush(stage_buffer, "stage_direction")
    return items


def trim_preamble(items: list[dict]) -> list[dict]:
    """Drop preamble items: scene titles, "by:" lines, cast descriptions, etc.

    Anchor on the *first character name that repeats* — real characters speak
    multiple times in a scene, scene titles appear exactly once. Walk every
    name and pick the first one whose text appears again as a name elsewhere
    in the document.

    Why this rule: the obvious "first name followed by dialogue" heuristic
    fails on a common shape — title (name) → credits (dialogue) → opening
    stage_direction → character (name) → speech (dialogue). The title matches
    the pattern at index 0 and nothing gets trimmed. Scenes that genuinely
    have one-line speakers are rare; even then, the conversation partner
    repeats and anchors correctly. Fall back to the old rule only when no
    name repeats at all.

    Stage_directions immediately preceding the chosen first character name
    are kept (they set the opening scene).
    """
    name_indices: list[int] = [i for i, it in enumerate(items) if it["type"] == "name"]
    name_counts: dict[str, int] = {}
    for i in name_indices:
        text = items[i]["text"].strip()
        name_counts[text] = name_counts.get(text, 0) + 1

    first_real_name: int | None = None
    for i in name_indices:
        if name_counts.get(items[i]["text"].strip(), 0) >= 2:
            first_real_name = i
            break

    if first_real_name is None:
        # No name repeats — fall back to the old "name followed by dialogue
        # (possibly via stage_directions)" heuristic.
        for i in name_indices:
            j = i + 1
            while j < len(items) and items[j]["type"] == "stage_direction":
                j += 1
            if j < len(items) and items[j]["type"] == "dialogue":
                first_real_name = i
                break
    if first_real_name is None:
        return items
    start = first_real_name
    while start > 0 and items[start - 1]["type"] == "stage_direction":
        start -= 1
    return items[start:]


_BRACKET_PAIRS = [("(", ")"), ("[", "]"), ("{", "}")]


def fix_reversed_brackets(items: list[dict]) -> list[dict]:
    """Detect and repair systematically-reversed bracket pairs.

    Some PDFs encode bracket pairs in visual order so that after bidi
    normalization the open/close characters end up swapped. Per-pair: if a
    closing bracket appears before any opening bracket, swap every occurrence.
    """
    full_text = "\n".join(item["text"] for item in items)
    for open_b, close_b in _BRACKET_PAIRS:
        open_pos = full_text.find(open_b)
        close_pos = full_text.find(close_b)
        if close_pos != -1 and (open_pos == -1 or close_pos < open_pos):
            for item in items:
                item["text"] = (item["text"]
                                .replace(open_b, "\x00")
                                .replace(close_b, open_b)
                                .replace("\x00", close_b))
    return items


def lines_to_items(lines: list[LogicalLine]) -> list[dict]:
    """Run the shared post-extraction pipeline."""
    lines = [ln for ln in lines if not _is_watermark(ln.text)]
    items = group_into_items(lines)
    items = trim_preamble(items)
    return fix_reversed_brackets(items)
