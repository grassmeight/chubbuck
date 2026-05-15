# Free-Tier AWS Architecture: FastAPI + LibreOffice PDF Converter

A perpetual-free-tier design for a low-volume (≤50 conversions/week, 25-35 users) document-to-PDF service. Optimized to avoid the cold-start UX problem of naive Lambda + LibreOffice deployments.

## Components

| Component | Purpose | Free Tier (Perpetual) | Used / Limit |
|---|---|---|---|
| **Lambda** (container image, ~800 MB) | FastAPI + LibreOffice runtime | 1M req + 400k GB-s/mo | ~8.7k req / ~14k GB-s |
| **Lambda Function URL** + response streaming | Public HTTPS endpoint (≤20 MB response) | Free | — |
| **EventBridge Scheduler** | Keepalive ping every 5 min | 1M invocations/mo | ~8.6k |
| **CloudFront** | TLS + static upload page caching | 1 TB out + 10M req/mo | negligible |
| **CloudWatch Logs** | Function logs, 1-day retention | 5 GB ingestion | ~MB/mo |
| **ECR** | Container image storage | 500 MB perpetual | ~800 MB → ~$0.03/mo |
| **S3** (optional, >20 MB outputs only) | Overflow file storage + presigned GET URLs | 5 GB free for 12 mo only | cents/mo after |

**Real recurring cost: $0.03 - $0.10/mo perpetually**, dominated by ECR rounding.

## Request Flow

```
User browser
   │  (HTTPS)
   ▼
CloudFront ──► S3 (static index.html upload form)
   │
   │  POST /convert  (multipart upload, ≤6 MB body)
   ▼
Lambda Function URL (streaming response, ≤20 MB)
   │
   ├─► /tmp: write input, soffice --headless --convert-to pdf
   │
   └─► Stream PDF back to client
        (or PUT to S3 + return presigned URL if >20 MB)
```

EventBridge Scheduler → Function URL `?warm=1` every 5 min, fan-out for N=2 warm envs.

## Cold-Start Mitigations (layered)

1. **EventBridge keepalive every 5 min** — handler short-circuits on `?warm=1` with HTTP 200 in ~50 ms. Keeps one execution environment hot.
2. **Fan-out to N warm envs** — keepalive handler self-invokes N-1 parallel copies once per cycle to keep multiple environments hot. Set N=2 or 3 to absorb rare concurrent requests.
3. **Pre-warm LibreOffice in module init** — run a dummy conversion at module load (outside the handler function), so the LO user-profile + font cache exists before the first real request lands.
4. **Reserved concurrency = N** — caps and reserves the warm pool, prevents one cold burst from spawning unwanted new envs.

Residual cold-start probability: ~1-2% of requests (AWS reaping the execution env). Invisible at this scale.

## Lambda Configuration

- **Memory**: 2048 MB. Counterintuitively cheaper — Lambda CPU scales with RAM, so a 30s conversion becomes ~10-15s. Net GB-s lower.
- **Timeout**: 60s
- **Ephemeral storage (/tmp)**: 1024 MB default is fine; up to 10 GB available
- **Architecture**: arm64 (Graviton) — ~20% cheaper GB-s rate, LibreOffice ARM builds work
- **Environment**: `HOME=/tmp`, `TMPDIR=/tmp` so LibreOffice can write its profile

## Known Limits & Gotchas

- **Sync invoke payload ceiling = 6 MB**. Function URL response streaming bumps response to 20 MB. Inputs >6 MB → presigned S3 PUT URL pattern.
- **No SnapStart for container images** (as of 2026). Python SnapStart requires zip packaging ≤250 MB — LibreOffice doesn't fit even with layers + EFS tricks. Keepalive ping is the substitute.
- **Image first-pull after deploy is slow** (5-15 s) regardless of warming. Deploy off-hours.
- **LibreOffice headless** writes to `~/.config/libreoffice` on first run. Must redirect `HOME` to `/tmp` or pre-bake the profile in the image.
- **Concurrent execution beyond reserved warm pool** → cold start. Acceptable at 50 conv/week; revisit if usage grows.

## Dockerfile Sketch

```dockerfile
FROM public.ecr.aws/lambda/python:3.12-arm64

RUN dnf install -y libreoffice-core libreoffice-writer libreoffice-calc \
    libreoffice-impress fontconfig && dnf clean all

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ${LAMBDA_TASK_ROOT}/app/
COPY warmup.docx /opt/

ENV HOME=/tmp TMPDIR=/tmp

# Pre-warm LO profile at module init (outside handler)
RUN soffice --headless --convert-to pdf --outdir /tmp /opt/warmup.docx || true

CMD ["app.handler.lambda_handler"]
```

Use **Mangum** to adapt FastAPI to the Lambda event interface, or write a plain handler if FastAPI's overhead isn't needed.

## Deployment

Recommend Terraform or AWS SAM. Minimum resources:

- `aws_ecr_repository` + image push
- `aws_lambda_function` (image package, arm64, 2048 MB, 60s, reserved_concurrency=2)
- `aws_lambda_function_url` (auth_type=NONE, invoke_mode=RESPONSE_STREAM)
- `aws_cloudfront_distribution` (origin = function URL, also serves S3 static)
- `aws_s3_bucket` (static site + optional output overflow)
- `aws_scheduler_schedule` (EventBridge Scheduler, rate(5 minutes), target = function URL via HTTPS invoke or direct Lambda invoke with `{"warm": true}` payload)
- `aws_cloudwatch_log_group` (retention_in_days = 1)

## When to Abandon This Design

- Sustained concurrency >3 → cheaper to run Lightsail/Fargate
- File sizes regularly >20 MB → S3 dependency grows, design value drops
- Need to add auth, rate limiting, multi-tenant — API Gateway re-enters the picture, free tier ends at 12 mo

For this workload, design holds indefinitely.
