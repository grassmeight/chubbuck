# Pre-create the log group so we can pin retention. If we let Lambda auto-
# create it on first invocation, retention defaults to "Never" and free-tier
# log ingestion eventually drifts over the limit.
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.project_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "app" {
  function_name = var.project_name
  role          = aws_iam_role.lambda_exec.arn

  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
  # x86_64 (not arm64) because the Debian base image we use for LibreOffice
  # availability builds natively on x86 dev machines — switching to arm64
  # would require QEMU emulation that runs ~5x slower for the LibreOffice
  # apt install. The Graviton GB-s discount is ~$0.03/mo at this volume.
  architectures = ["x86_64"]

  memory_size = var.lambda_memory_mb
  timeout     = var.lambda_timeout_seconds

  # Caps and reserves the warm pool. A request beyond this count throttles
  # rather than spinning a cold environment — acceptable at this volume.
  reserved_concurrent_executions = var.warm_pool_size

  ephemeral_storage {
    size = var.lambda_ephemeral_storage_mb
  }

  environment {
    variables = {
      # LibreOffice writes its user profile + font cache to $HOME on first run.
      # In Lambda only /tmp is writable.
      HOME   = "/tmp"
      TMPDIR = "/tmp"

      # Read by the handler to know how many copies of itself to fan-out on
      # the keepalive tick.
      WARM_POOL_SIZE = tostring(var.warm_pool_size)

      # Server-side cap. Uploads come via presigned S3 PUT (Browser → S3
      # direct), so this is not the Function URL's 6 MB request limit; it's
      # purely the size we re-validate after fetching from the uploads
      # bucket. Matches the dev / HF default.
      MAX_UPLOAD_MB = "25"

      # Where browsers PUT their uploads. The handler signs the presigned
      # URL with this name and fetches the file back from here.
      UPLOADS_BUCKET = aws_s3_bucket.uploads.bucket

      # LibreOffice on Debian Bookworm picks NotoSansDevanagari as the
      # Hebrew fallback when the cell font is Calibri/Carlito — which has
      # no Hebrew glyphs, so the output renders as tofu boxes. Forcing the
      # cell font to "Noto Sans Hebrew" sidesteps the bad fallback. See
      # app/filler.py and the matching CELL_FONT_OVERRIDE handling.
      CELL_FONT_OVERRIDE = "Noto Sans Hebrew"

      # Empty when overflow disabled; the handler treats empty as "stream
      # everything inline, never spill to S3".
      OVERFLOW_BUCKET = var.enable_overflow_bucket ? aws_s3_bucket.overflow[0].bucket : ""
    }
  }

  # Image pushes happen out-of-band (CI / `aws lambda update-function-code`).
  # Without ignore_changes here, every TF run would try to revert the live
  # image back to whatever tag is in state.
  lifecycle {
    ignore_changes = [image_uri]
  }

  depends_on = [
    aws_iam_role_policy_attachment.lambda_basic,
    aws_cloudwatch_log_group.lambda,
  ]
}

resource "aws_lambda_function_url" "app" {
  function_name = aws_lambda_function.app.function_name

  # Public endpoint — CloudFront proxies to it. AUTH=NONE because at this
  # scale we accept the abuse risk in exchange for skipping signed-URL
  # complexity. Function URL is also rate-limited by the reserved concurrency.
  authorization_type = "NONE"

  # BUFFERED capped at 6 MB request + 6 MB response. The doc proposes
  # RESPONSE_STREAM (20 MB response cap) but that requires a streaming-
  # style handler with awslambdaric chunked-response wrapping, which is
  # significantly more code than the value adds for this workload —
  # typical Chubbuck output PDFs are a few hundred KB. If you start
  # producing >6 MB PDFs, switch to RESPONSE_STREAM and rewrite
  # app/handler.py around @stream_response.
  invoke_mode = "BUFFERED"

  cors {
    allow_credentials = false
    allow_origins     = ["*"]
    # Function URL CORS: each method string must be ≤6 chars, and OPTIONS
    # isn't allowed in the list — Lambda handles CORS preflight automatically
    # when allow_methods is configured.
    allow_methods = ["GET", "POST"]
    allow_headers = ["content-type"]
    max_age       = 86400
  }
}
