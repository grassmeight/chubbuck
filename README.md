---
title: Chubbuck Scene Analyzer
emoji: 🎭
colorFrom: pink
colorTo: red
sdk: docker
app_port: 7860
pinned: false
short_description: Hebrew screenplay scene -> filled analysis spreadsheet (PDF)
---

# צ'בק · ניתוח סצנות

Upload a Hebrew dialogue scene (PDF or DOCX) and get back a filled-in Chubbuck
analysis spreadsheet, exported as PDF.

The frontend is served at `/`, the conversion API is `POST /api/convert`.
Each request is processed in a per-request temp directory and wiped after the
response is sent — no persistent storage.

## Local development

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:DEV_KEEP_OUTPUTS=1; uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

LibreOffice (`soffice`) must be installed and discoverable on PATH.
