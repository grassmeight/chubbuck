"""FastAPI app: accept a PDF/DOCX scene, return a filled-template PDF.

No persistent storage. Each request processes the upload in a TemporaryDirectory
that is removed after the response is returned. The frontend is served from
app/static/.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from app.parser import parse
from app.filler import fill_template
from app.pdf_export import xlsx_to_pdf

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB

APP_DIR = Path(__file__).parent
TEMPLATE_PATH = APP_DIR.parent / "טבלת ניתוחים ריקה.xlsx"
STATIC_DIR = APP_DIR / "static"
# When DEV_KEEP_OUTPUTS=1, mirror the filled .xlsx and the produced .pdf into
# tests/ next to the repo root before the temp dir is wiped, so we can inspect
# what LibreOffice was fed vs. what it produced.
DEV_OUTPUT_DIR = APP_DIR.parent / "tests"

app = FastAPI(title="Scene Analyzer", docs_url=None, redoc_url=None)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/convert")
async def convert(file: UploadFile = File(...)) -> FileResponse:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type {suffix!r}. "
                                  f"Allowed: {sorted(ALLOWED_EXTENSIONS)}")

    # Stream the upload to a temp file with a size cap.
    tmpdir = Path(tempfile.mkdtemp(prefix="scene_"))
    try:
        upload_path = tmpdir / f"input{suffix}"
        total = 0
        with upload_path.open("wb") as f:
            while True:
                chunk = await file.read(1 << 20)  # 1 MiB
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "File too large (limit 25 MB).")
                f.write(chunk)

        logger.info("received upload: name=%s bytes=%d", file.filename, total)

        try:
            items = parse(upload_path)
        except Exception as exc:
            logger.exception("parse failed")
            raise HTTPException(422, f"Could not parse file: {exc}") from exc

        if not items:
            raise HTTPException(422, "No dialogue items detected in the input.")

        xlsx_path = tmpdir / "output.xlsx"
        pdf_path = tmpdir / "output.pdf"
        try:
            fill_template(items, TEMPLATE_PATH, xlsx_path)
            xlsx_to_pdf(xlsx_path, pdf_path)
        except Exception as exc:
            logger.exception("fill/export failed")
            raise HTTPException(500, f"Conversion failed: {exc}") from exc

        if os.environ.get("DEV_KEEP_OUTPUTS") == "1":
            try:
                DEV_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                stem = Path(file.filename).stem
                shutil.copy2(xlsx_path, DEV_OUTPUT_DIR / f"{stem}.xlsx")
                shutil.copy2(pdf_path, DEV_OUTPUT_DIR / f"{stem}.pdf")
                logger.info("dev: copied outputs to %s", DEV_OUTPUT_DIR)
            except Exception:
                logger.exception("dev: failed to copy outputs to %s", DEV_OUTPUT_DIR)

        download_name = f"{Path(file.filename).stem} - ניתוח.pdf"

        def _cleanup():
            try:
                for p in tmpdir.iterdir():
                    p.unlink(missing_ok=True)
                tmpdir.rmdir()
            except Exception:
                logger.exception("cleanup failed for %s", tmpdir)

        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            filename=download_name,
            background=BackgroundTask(_cleanup),
        )
    except HTTPException:
        # cleanup on errors too
        try:
            for p in tmpdir.iterdir():
                p.unlink(missing_ok=True)
            tmpdir.rmdir()
        except Exception:
            pass
        raise
    except Exception:
        try:
            for p in tmpdir.iterdir():
                p.unlink(missing_ok=True)
            tmpdir.rmdir()
        except Exception:
            pass
        logger.exception("unexpected failure")
        raise HTTPException(500, "Internal server error.")


# Mount the static UI last so /api/* and /health take precedence.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
