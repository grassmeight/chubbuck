# Hugging Face Space (Docker SDK) image for the Chubbuck scene analyzer.
#
# Bundles:
#   - Python 3.12 (stable wheels for all our deps)
#   - LibreOffice (headless xlsx -> pdf conversion)
#   - Noto Hebrew font (LibreOffice's default Hebrew fallback otherwise looks bad)
#   - Carlito (metric-compatible Calibri replacement so the template's Calibri
#     references render at the same widths as on the dev machine; the page-break
#     math in app/filler.py was calibrated against Calibri metrics)
#
# HF Spaces require the container to (1) run as a non-root user with uid 1000
# and (2) listen on the port declared as `app_port` in README.md frontmatter.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice \
        fonts-noto-hebrew \
        fonts-crosextra-carlito \
        fontconfig \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR /home/user/app

COPY --chown=user:user requirements.txt ./
RUN pip install --user --no-cache-dir -r requirements.txt

COPY --chown=user:user . ./

EXPOSE 7860

# Single worker is correct here: each request spawns its own LibreOffice
# subprocess with an isolated user-profile (see app/pdf_export.py), so request
# concurrency is bounded by LibreOffice startup cost, not by uvicorn workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
