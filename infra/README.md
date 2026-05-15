# `infra/` — AWS free-tier Terraform

Provisions the AWS resources described in
[../aws-freetier-pdf-converter.md](../aws-freetier-pdf-converter.md):

| Resource | Purpose |
|---|---|
| `aws_ecr_repository` | Stores the Lambda container image |
| `aws_lambda_function` | FastAPI + LibreOffice runtime (arm64, 2048 MB, 60s, reserved concurrency = warm pool) |
| `aws_lambda_function_url` | Public HTTPS endpoint with response streaming (≤20 MB responses) |
| `aws_cloudfront_distribution` | TLS + caches static UI + proxies `/api/*` to the Function URL |
| `aws_s3_bucket` (static) | Hosts `index.html` and assets, accessed only via CloudFront OAC |
| `aws_s3_bucket` (uploads) | Browser uploads land here via presigned PUT URLs (1-day lifecycle) |
| `aws_s3_bucket` (overflow, optional) | Output overflow for PDFs >20 MB. Disabled by default |
| `aws_scheduler_schedule` | EventBridge keepalive every 5 min |
| `aws_cloudwatch_log_group` | Function logs, retention defaulted to 1 day |
| `aws_iam_role` ×2 | Lambda execution + scheduler invoke |

## What's provided alongside this IaC

The Lambda-side app code already exists in this repo:

- [`../Dockerfile.lambda`](../Dockerfile.lambda) — Lambda base image
  (arm64, Amazon Linux 2023, `dnf install libreoffice-core libreoffice-calc`,
  Hebrew + Liberation fonts, soffice version-check sanity).
- [`../app/handler.py`](../app/handler.py) — Mangum-wrapped FastAPI handler
  with the `{"warm": true}` short-circuit, fan-out to
  `WARM_POOL_SIZE - 1` self-invocations (the `lambda_self_invoke` IAM
  policy already grants this), and a cold-start LibreOffice pre-warm at
  module init.
- [`../app/main.py`](../app/main.py) — original `/api/convert` (multipart)
  endpoint still works for HF and local dev. Two new endpoints exist for
  the Lambda deploy:
  - `POST /api/upload-url` — returns a presigned S3 PUT URL the browser
    can upload to directly. Returns 503 when `UPLOADS_BUCKET` env is
    empty (HF/local), so the frontend can detect-and-fall-back.
  - `POST /api/convert-s3` — fetches the just-uploaded file from S3 by
    key, runs it through the parse → fill → export pipeline, returns
    the PDF, deletes the S3 object.
- [`../app/static/index.html`](../app/static/index.html) — branches at
  runtime on `window.__UPLOAD_VIA_S3`. HF leaves it unset (falls back to
  multipart). The Lambda deploy injects a small `<script>` tag setting
  it to `true` (see [Bootstrap order](#bootstrap-order) step 4).
- [`../requirements.txt`](../requirements.txt) — gained `mangum`.

## What this still does NOT do

1. **Build & push the image.** `aws_lambda_function` validates that the
   `image_uri` resolves to an existing image, so the first `terraform apply`
   that includes the Lambda will fail until you've pushed something. See
   [Bootstrap order](#bootstrap-order) below.

2. **Upload `index.html`** to the static S3 bucket — the
   [Bootstrap order](#bootstrap-order) below has the deploy snippet that
   patches in `window.__UPLOAD_VIA_S3 = true` and uploads to the static
   bucket. Without that patch the page would still try the multipart
   endpoint, which works but caps uploads at 6 MB.

## Known gaps to verify after first deploy

- **Carlito font is not in the AL2023 dnf repos.** The HF deploy installs
  `fonts-crosextra-carlito` (a Calibri-metric replacement) and the
  page-break math in [`../app/filler.py`](../app/filler.py) is calibrated
  against Calibri. On Lambda LO will substitute another font; if PDFs
  spill onto extra horizontal pages, drop `_PRINT_SCALE_PCT` (currently
  61) by 1-2 points OR vendor Carlito ttf files into a
  `/usr/share/fonts/carlito/` `COPY` step in `Dockerfile.lambda`.
- **`invoke_mode = "BUFFERED"`**, not `RESPONSE_STREAM` as the design doc
  proposes. Streaming requires a chunked-response handler that's
  significantly more code; for Chubbuck-sized PDFs (a few hundred KB),
  the 6 MB buffered cap is plenty. Switch back to `RESPONSE_STREAM` and
  rewrite `app/handler.py` around `@stream_response` if you start
  producing >6 MB outputs.

## Prerequisites

- **AWS account** with billing alarms set up (this design is free-tier-safe,
  but it's still worth a $5/month alarm just in case).
- **AWS CLI ≥ 2.x** configured with credentials that can create IAM roles,
  Lambda functions, CloudFront distributions, S3 buckets, and EventBridge
  schedules. `aws configure` then `aws sts get-caller-identity` to verify.
- **Terraform ≥ 1.6** ([terraform.io/downloads](https://terraform.io/downloads)
  or `winget install Hashicorp.Terraform` on Windows).
- **Docker** (eventually, when you build the Lambda image).

## Bootstrap order

Because `aws_lambda_function` requires the image to exist in ECR before
apply, do this two-stage:

```powershell
cd infra

# 1. Apply just the ECR repo first.
terraform init
terraform apply -target=aws_ecr_repository.app

# 2. Build & push your Lambda image to the URL printed above.
$ecrUrl = terraform output -raw ecr_repository_url
$region = "us-east-1"
aws ecr get-login-password --region $region | `
    docker login --username AWS --password-stdin $ecrUrl
docker build --provenance=false --sbom=false `
    -t "${ecrUrl}:latest" -f ../Dockerfile.lambda ..
docker push "${ecrUrl}:latest"
# --provenance=false / --sbom=false: avoid the OCI attestation manifest
# Lambda rejects (see CI/CD footguns section below). The Lambda is x86_64
# (lambda.tf: architectures = ["x86_64"]) so no --platform flag is needed
# on an x86 dev machine.

# 3. Apply everything else.
terraform apply

# 4. Patch + upload the static index.html. The patch injects a one-line
#    <script> that sets window.__UPLOAD_VIA_S3 = true so the page uses the
#    presigned-PUT upload flow instead of multipart. HF/local-dev keep using
#    the unmodified file from app/static/.
#
#    NB: Get-Content -Raw + Set-Content -Encoding utf8 is NOT safe here on
#    Windows PowerShell 5.1: Get-Content reads as the system's ANSI default
#    (Windows-1252 on Hebrew/English locales), garbling UTF-8 multi-byte
#    sequences for Hebrew. Then Set-Content -Encoding utf8 writes with a BOM.
#    Use [System.IO.File] explicitly with UTF8Encoding(false) instead.
$utf8    = [System.Text.UTF8Encoding]::new($false)
$src     = [System.IO.File]::ReadAllText("..\app\static\index.html", $utf8)
$patched = $src -replace '</head>', '<script>window.__UPLOAD_VIA_S3=true;</script></head>'
New-Item -ItemType Directory -Force -Path ".\dist" | Out-Null
[System.IO.File]::WriteAllText("$PWD\dist\index.html", $patched, $utf8)

$bucket = terraform output -raw s3_static_bucket
aws s3 cp ".\dist\index.html" "s3://${bucket}/index.html" `
    --content-type "text/html; charset=utf-8"

# CloudFront caches index.html aggressively. Invalidate after re-upload.
$dist = (terraform state show aws_cloudfront_distribution.main `
         | Select-String -Pattern 'id\s+=\s+"([A-Z0-9]+)"').Matches.Groups[1].Value
aws cloudfront create-invalidation --distribution-id $dist --paths "/index.html" "/"

# 5. (Optional) Open the live site.
$cf = terraform output -raw cloudfront_domain
Start-Process $cf
```

**Re-deploying just the static page after a UI change**: re-run step 4 only.
`aws s3 cp` overwrites; CloudFront's CachingOptimized policy means
`index.html` may be cached at the edge for ~1 day. To force-flush use
`aws cloudfront create-invalidation --distribution-id <id> --paths "/*"`
(1000 free invalidation paths/month).

CloudFront takes ~5-10 minutes to deploy globally. Subsequent applies are
much faster.

## Updating the image without re-running terraform

The Lambda's `image_uri` is in `lifecycle.ignore_changes`, so direct image
pushes don't fight Terraform. After pushing a new image:

```powershell
$fnName = terraform output -raw lambda_function_name
$ecrUrl = terraform output -raw ecr_repository_url
aws lambda update-function-code --function-name $fnName --image-uri "${ecrUrl}:latest"
```

## CI/CD from GitHub Actions

A GitHub Actions workflow at [`../.github/workflows/deploy.yml`](../.github/workflows/deploy.yml)
builds the Lambda image and updates the function on every push to `main`. It
authenticates to AWS via OIDC — no access keys live in GitHub Secrets.

Provisioning the OIDC role (one-time):

1. Set `github_repo = "owner/name"` in `terraform.tfvars` (gitignored). The
   value must match the GitHub repo this code will live in. Example:
   ```hcl
   github_repo = "shira401/chubbuck"
   ```
2. `terraform apply`. This adds:
   - `aws_iam_openid_connect_provider.github` (one per AWS account; import
     it instead if you already have one)
   - `aws_iam_role.github_actions` — assumable only by the `main` branch of
     `var.github_repo`, scoped to ECR push + Lambda update on this project's
     resources only.
3. Grab the role ARN:
   ```powershell
   terraform output -raw github_actions_role_arn
   ```
4. Create the GitHub repo, then add a **repository variable** (not a
   secret — ARNs aren't secret): Settings → Secrets and variables → Actions
   → Variables → `AWS_ROLE_ARN` = the ARN from step 3.
5. Push to `main`. The workflow takes over.

The trust policy pins to `refs/heads/main` only. Forked PRs cannot assume
the role, which is intentional — they'd otherwise be able to deploy
arbitrary code. To also allow feature-branch builds, loosen the `sub`
condition in `github_oidc.tf` to `repo:${var.github_repo}:*`.

### Footguns encountered during the manual bootstrap

These don't affect the workflow (the Linux runner avoids them) but matter
if you're re-running the bootstrap by hand from PowerShell on Windows:

- `aws ecr get-login-password | docker login --password-stdin ...` returns
  HTTP 400 when piped natively in Windows PowerShell 5.1 — the pipe encoding
  mangles the JWT. Workaround: route through `cmd /c`:
  ```powershell
  $pw = aws ecr get-login-password --region us-east-1
  $env:_PW = $pw
  cmd /c "echo %_PW%| docker login --username AWS --password-stdin $ecrUrl"
  Remove-Item Env:\_PW
  ```
- `docker build` defaults to producing OCI manifests with attestation, and
  Lambda's `UpdateFunctionCode` rejects them (`The image manifest, config or
  layer media type for the source image ... is not supported`). Pass
  `--provenance=false --sbom=false` to fall back to the Docker v2 manifest
  Lambda accepts. The CI workflow already does this.
- The Dockerfile is `linux/amd64` (matches `architectures = ["x86_64"]` in
  `lambda.tf`). The earlier bootstrap snippet's `--platform linux/arm64`
  reference was stale — the Graviton variant was abandoned to skip QEMU
  build time. The build snippet above and the CI workflow both pin amd64.

## Tearing it all down

```powershell
terraform destroy
```

`force_destroy` is on for the S3 buckets and ECR repo so this works even
when they have content. CloudFront takes another 5-10 minutes to delete.

## Variables

See [variables.tf](variables.tf) for the full list with descriptions. Most
have sensible defaults. Common overrides go in `terraform.tfvars` (gitignored
— see [example.tfvars](example.tfvars) for the shape):

```hcl
project_name           = "chubbuck"
aws_region             = "us-east-1"
warm_pool_size         = 2
enable_overflow_bucket = false
log_retention_days     = 1
```

## Cost expectations

Per the design doc, **$0.03 - $0.10 per month** in steady state, dominated
by ECR storage rounding (free tier covers 500 MB; the LO image is ~800 MB).
Free tier covers Lambda invocations, scheduler runs, CloudFront traffic,
CloudWatch log ingestion, and the S3 static bucket comfortably for the
target volume (≤50 conversions/week).

The optional overflow bucket is **free for 12 months only** — leave
`enable_overflow_bucket = false` unless you actually have outputs >20 MB.

[Mangum]: https://mangum.io/
