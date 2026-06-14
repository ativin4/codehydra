import base64
import subprocess
from pathlib import Path
from typing import Optional

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".heic"})
PDF_EXTS = frozenset({".pdf"})
VIDEO_EXTS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"})
MEDIA_EXTS = IMAGE_EXTS | PDF_EXTS | VIDEO_EXTS


def pdf_to_text(path: Path) -> Optional[str]:
    """Extract text from a PDF using pdftotext (poppler). Returns None if unavailable."""
    try:
        result = subprocess.run(
            ["pdftotext", str(path), "-"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def image_to_base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()
