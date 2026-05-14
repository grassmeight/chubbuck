"""Convert an xlsx file to PDF using LibreOffice headless mode.

LibreOffice handles Hebrew RTL rendering correctly and is reliable across
Windows / Linux (Render deployment) when used with --headless.

Search order for the soffice binary:
  1. The SOFFICE_BIN environment variable (explicit override).
  2. `soffice` on PATH.
  3. Common install locations on Windows / Linux / macOS.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

_CANDIDATES = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/usr/local/bin/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
]


def _find_soffice() -> str:
    env = os.environ.get("SOFFICE_BIN")
    if env and Path(env).exists():
        return env
    on_path = shutil.which("soffice") or shutil.which("libreoffice")
    if on_path:
        return on_path
    for candidate in _CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError(
        "LibreOffice (soffice) not found. Set SOFFICE_BIN, install LibreOffice, "
        "or add soffice to PATH."
    )


def xlsx_to_pdf(xlsx_path: str | Path, pdf_path: str | Path,
                timeout_seconds: int = 120) -> None:
    """Convert xlsx_path -> pdf_path.

    LibreOffice writes the PDF with the source file's stem name into --outdir,
    so we convert into a temp dir and then move the result to pdf_path.
    """
    soffice = _find_soffice()
    xlsx_path = Path(xlsx_path).resolve()
    pdf_path = Path(pdf_path).resolve()
    if not xlsx_path.exists():
        raise FileNotFoundError(xlsx_path)

    with tempfile.TemporaryDirectory(prefix="lo_pdf_") as tmpdir:
        # Use an isolated user profile so concurrent conversions don't clash.
        user_profile = Path(tmpdir) / "lo_user"
        user_profile.mkdir()
        profile_uri = user_profile.absolute().as_uri()

        cmd = [
            soffice,
            f"-env:UserInstallation={profile_uri}",
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--convert-to", "pdf",
            "--outdir", tmpdir,
            str(xlsx_path),
        ]
        result = subprocess.run(
            cmd,
            timeout=timeout_seconds,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"LibreOffice conversion failed (exit {result.returncode}):\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )

        produced = Path(tmpdir) / f"{xlsx_path.stem}.pdf"
        if not produced.exists():
            raise RuntimeError(
                f"LibreOffice did not produce expected output {produced}.\n"
                f"Files in outdir: {list(Path(tmpdir).iterdir())}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), str(pdf_path))
