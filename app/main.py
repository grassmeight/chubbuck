"""FastAPI app: accept a PDF/DOCX scene, return a filled-template PDF.

No persistent storage. Each request processes the upload in a TemporaryDirectory
that is removed after the response is returned.

Two upload paths exist on /api/*:

  POST /api/convert       — multipart upload + immediate convert.
                             Used by the HF and local-dev deploys.

  POST /api/upload-url    — return a presigned S3 PUT URL.
  POST /api/convert-s3    — convert a file already PUT to the uploads bucket.
                             Used by the AWS Lambda deploy because the
                             Function URL caps request bodies at 6 MB; the
                             two-step flow lets the browser PUT directly to
                             S3, bypassing the Function URL for the upload
                             body.

The S3 endpoints return HTTP 503 when UPLOADS_BUCKET is empty (i.e. on the
HF deploy), so a single frontend can detect-and-fall-back instead of needing
deploy-specific code paths.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
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

# Upload cap. Default 25 MB matches dev / HF / Lambda-with-presigned-S3.
# Lambda-without-presigned would set this to 6 to match the Function URL
# request-body ceiling, but our deploy uses presigned PUT so 25 holds.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024

APP_DIR = Path(__file__).parent
TEMPLATE_PATH = APP_DIR.parent / "טבלת ניתוחים ריקה.xlsx"
STATIC_DIR = APP_DIR / "static"
# When DEV_KEEP_OUTPUTS=1, mirror the filled .xlsx and the produced .pdf into
# tests/ next to the repo root before the temp dir is wiped, so we can inspect
# what LibreOffice was fed vs. what it produced.
DEV_OUTPUT_DIR = APP_DIR.parent / "tests"

app = FastAPI(title="Scene Analyzer", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _cleanup_tmpdir(tmpdir: Path) -> None:
    try:
        for p in tmpdir.iterdir():
            p.unlink(missing_ok=True)
        tmpdir.rmdir()
    except Exception:
        logger.exception("cleanup failed for %s", tmpdir)


def _make_pdf_response(
    *,
    input_path: Path,
    tmpdir: Path,
    original_filename: str,
) -> FileResponse:
    """Run parse → fill → export and return a FileResponse with cleanup wired.

    Caller owns: writing the input file at input_path, error handling for
    upload/fetch failures, and tmpdir cleanup if THIS function raises (the
    success path attaches cleanup to the response's BackgroundTask).
    """
    try:
        items = parse(input_path)
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
            stem = Path(original_filename).stem
            shutil.copy2(xlsx_path, DEV_OUTPUT_DIR / f"{stem}.xlsx")
            shutil.copy2(pdf_path, DEV_OUTPUT_DIR / f"{stem}.pdf")
            logger.info("dev: copied outputs to %s", DEV_OUTPUT_DIR)
        except Exception:
            logger.exception("dev: failed to copy outputs to %s", DEV_OUTPUT_DIR)

    download_name = f"{Path(original_filename).stem} - ניתוח.pdf"

    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=download_name,
        background=BackgroundTask(_cleanup_tmpdir, tmpdir),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/convert")
async def convert(file: UploadFile = File(...)) -> FileResponse:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type {suffix!r}. "
                                  f"Allowed: {sorted(ALLOWED_EXTENSIONS)}")

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

        return _make_pdf_response(
            input_path=upload_path,
            tmpdir=tmpdir,
            original_filename=file.filename or "scene",
        )
    except HTTPException:
        _cleanup_tmpdir(tmpdir)
        raise
    except Exception:
        _cleanup_tmpdir(tmpdir)
        logger.exception("unexpected failure")
        raise HTTPException(500, "Internal server error.")


# ---------------------------------------------------------------------------
# S3 presigned-PUT upload flow (Lambda deploy)
# ---------------------------------------------------------------------------

# Cache the boto3 client lazily so importing app.main on the HF deploy
# (no boto3 installed... actually boto3 IS in requirements via mangum's
# transitive deps on Lambda only — we still lazy-import to avoid pulling
# botocore into the HF process unnecessarily).
_s3_client: Any = None


def _get_s3():
    global _s3_client
    if _s3_client is None:
        import boto3
        _s3_client = boto3.client("s3")
    return _s3_client


def _uploads_bucket() -> str:
    bucket = os.environ.get("UPLOADS_BUCKET", "").strip()
    if not bucket:
        raise HTTPException(
            503,
            "S3 upload flow not configured on this deployment. "
            "Use POST /api/convert (multipart) instead.",
        )
    return bucket


@app.post("/api/upload-url")
def upload_url(payload: dict = Body(...)) -> dict:
    """Return a presigned PUT URL the browser can upload directly to.

    The browser then calls /api/convert-s3 with the returned key.
    """
    bucket = _uploads_bucket()

    filename = (payload.get("filename") or "").strip()
    if not filename:
        raise HTTPException(400, "filename is required")
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type {suffix!r}.")

    import uuid
    key = f"uploads/{uuid.uuid4().hex}{suffix}"

    s3 = _get_s3()
    url = s3.generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ContentType": "application/octet-stream",
        },
        ExpiresIn=300,  # 5 minutes — plenty for the upload, short enough
                       # that a leaked URL stops working quickly.
    )
    return {
        "key": key,
        "url": url,
        "max_bytes": MAX_UPLOAD_BYTES,
    }


@app.post("/api/convert-s3")
def convert_s3(payload: dict = Body(...)) -> FileResponse:
    """Fetch a previously-uploaded file from S3, convert, return PDF."""
    bucket = _uploads_bucket()

    key = (payload.get("key") or "").strip()
    # Tight allowlist on key shape — the only valid keys are ones we just
    # minted in /api/upload-url, so anything outside the uploads/ prefix is
    # either a misconfigured client or someone probing for object access.
    if not key.startswith("uploads/") or "../" in key or "\\" in key:
        raise HTTPException(400, "Invalid key.")

    suffix = Path(key).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type {suffix!r}.")

    # Original filename is purely for the response Content-Disposition; the
    # client passes it back so the downloaded PDF gets a meaningful name.
    original_name = (payload.get("filename") or "").strip() or f"scene{suffix}"

    s3 = _get_s3()

    tmpdir = Path(tempfile.mkdtemp(prefix="scene_"))
    try:
        # Defense-in-depth: HEAD before download to enforce the size cap on
        # what the browser actually PUT. The presigned URL doesn't carry a
        # content-length-range condition (using generate_presigned_url, not
        # generate_presigned_post), so this is the server-side check.
        try:
            head = s3.head_object(Bucket=bucket, Key=key)
        except Exception as exc:
            raise HTTPException(404, "Upload not found or expired.") from exc

        size = int(head.get("ContentLength", 0))
        if size == 0:
            raise HTTPException(400, "Empty upload.")
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "File too large.")

        upload_path = tmpdir / f"input{suffix}"
        s3.download_file(bucket, key, str(upload_path))
        logger.info("fetched s3 upload: key=%s bytes=%d", key, size)

        # Best-effort delete now that we have it locally. The 1-day bucket
        # lifecycle is the safety net; this just avoids waiting that long.
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except Exception:
            logger.warning("could not delete s3 upload %s "
                           "(lifecycle will clean up)", key)

        return _make_pdf_response(
            input_path=upload_path,
            tmpdir=tmpdir,
            original_filename=original_name,
        )
    except HTTPException:
        _cleanup_tmpdir(tmpdir)
        raise
    except Exception:
        _cleanup_tmpdir(tmpdir)
        logger.exception("unexpected failure")
        raise HTTPException(500, "Internal server error.")


# Mount the static UI last so /api/* and /health take precedence.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
