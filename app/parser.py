"""Dispatch a scene file to the right format-specific parser.

Each parser returns a list of dicts:
    {"type": "name" | "dialogue" | "stage_direction", "text": str}
ordered as they appear in the document.

Format-specific logic lives in `parser_pdf` and `parser_docx`. Shared
post-extraction logic (classification, grouping, preamble trimming, bracket
repair) lives in `parser_common`.
"""
from __future__ import annotations

from pathlib import Path

from app.parser_pdf import parse_pdf
from app.parser_docx import parse_docx

__all__ = ["parse", "parse_pdf", "parse_docx"]


def parse(file_path: str | Path) -> list[dict]:
    p = Path(file_path)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(p)
    if suffix in (".docx", ".doc"):
        return parse_docx(p)
    raise ValueError(f"Unsupported file type: {suffix}")
