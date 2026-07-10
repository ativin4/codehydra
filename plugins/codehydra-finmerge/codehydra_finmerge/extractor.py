"""PDF text and table extraction for trade statements.

Uses pdfplumber for structured table extraction (preferred for broker
statements with tabular data), with pdftotext (poppler) as a fallback.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional, Tuple


def extract_text_pdfplumber(pdf_path: Path) -> Tuple[str, List[List[List[str]]]]:
    """Extract full text and structured tables from a PDF via pdfplumber.

    Returns:
        (full_text, tables) — tables is a flat list of tables across all pages;
        each table is a list of rows, each row a list of cell strings.
    """
    try:
        import pdfplumber
    except ImportError:
        raise ImportError(
            "pdfplumber is required for PDF table extraction. "
            "Install it with: pip install pdfplumber"
        )

    full_text_parts: List[str] = []
    all_tables: List[List[List[str]]] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            full_text_parts.append(text)

            page_tables = page.extract_tables() or []
            for table in page_tables:
                cleaned: List[List[str]] = []
                for row in table:
                    cleaned_row = [(cell.strip() if cell else "") for cell in row]
                    if any(cleaned_row):  # skip entirely empty rows
                        cleaned.append(cleaned_row)
                if cleaned:
                    all_tables.append(cleaned)

    return "\n\n".join(full_text_parts), all_tables


def extract_text_fallback(pdf_path: Path) -> Optional[str]:
    """Fallback: extract text using pdftotext (poppler) with layout preservation."""
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def extract_from_pdf(pdf_path: Path) -> dict:
    """Extract text and tables from a PDF.

    Returns:
        {"text": str, "tables": list[list[list[str]]], "file": str}
    """
    text = ""
    tables: List[List[List[str]]] = []

    try:
        text, tables = extract_text_pdfplumber(pdf_path)
    except ImportError:
        # pdfplumber not installed — fall back to pdftotext
        fallback = extract_text_fallback(pdf_path)
        if fallback:
            text = fallback
    except Exception:
        # pdfplumber choked on this particular PDF — try fallback
        fallback = extract_text_fallback(pdf_path)
        if fallback:
            text = fallback

    return {"text": text, "tables": tables, "file": str(pdf_path)}
