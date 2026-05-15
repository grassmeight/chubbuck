# =============================================================================
# Static UI bucket — index.html + assets, served via CloudFront only.
# =============================================================================

# S3 bucket names are globally unique; suffix with account ID to avoid
# collisions across AWS accounts using this same module.
resource "aws_s3_bucket" "static" {
  bucket = "${var.project_name}-static-${data.aws_caller_identity.current.account_id}"

  # Personal-project: lets `terraform destroy` succeed even if you've
  # uploaded objects. Don't enable in production.
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "static" {
  bucket = aws_s3_bucket.static.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "static" {
  bucket = aws_s3_bucket.static.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Bucket policy: only this CloudFront distribution (via OAC) can read.
data "aws_iam_policy_document" "static_bucket" {
  statement {
    sid       = "AllowCloudFrontServicePrincipalReadOnly"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.static.arn}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.main.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "static" {
  bucket = aws_s3_bucket.static.id
  policy = data.aws_iam_policy_document.static_bucket.json
}

# =============================================================================
# Uploads bucket — every upload lands here first via a presigned PUT URL,
# then Lambda fetches it for processing. Required because the Function URL
# request body ceiling (6 MB sync invoke) is well below our 25 MB upload cap.
# Browser → S3 ingress is free; S3 → Lambda same-region transfer is free;
# objects auto-expire after 1 day, so steady-state storage stays in cents/yr.
# =============================================================================

resource "aws_s3_bucket" "uploads" {
  bucket        = "${var.project_name}-uploads-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Browsers PUT directly to S3 from the user-facing CloudFront origin, so the
# bucket needs CORS for PUT. allowed_origins=["*"] is safe here because the
# only way to PUT is with a presigned URL signed by our Lambda — open CORS
# doesn't grant any actual write capability.
resource "aws_s3_bucket_cors_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  cors_rule {
    allowed_methods = ["PUT"]
    allowed_origins = ["*"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

# Auto-expire uploads after 1 day. Each upload is consumed within seconds of
# the PUT; the lifecycle is a safety net for orphans (browser PUT succeeded
# but the user closed the tab before /api/convert-s3 fired).
resource "aws_s3_bucket_lifecycle_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  rule {
    id     = "expire-1d"
    status = "Enabled"

    filter {}

    expiration {
      days = 1
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# =============================================================================
# Optional overflow bucket — only created when enable_overflow_bucket = true.
# Used for output PDFs >20 MB that exceed Function URL response streaming.
# =============================================================================

resource "aws_s3_bucket" "overflow" {
  count         = var.enable_overflow_bucket ? 1 : 0
  bucket        = "${var.project_name}-overflow-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "overflow" {
  count  = var.enable_overflow_bucket ? 1 : 0
  bucket = aws_s3_bucket.overflow[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Auto-expire overflow objects after 1 day. Each conversion is per-request
# ephemeral; the presigned GET URL is delivered to the user immediately and
# never needs to outlive the session.
resource "aws_s3_bucket_lifecycle_configuration" "overflow" {
  count  = var.enable_overflow_bucket ? 1 : 0
  bucket = aws_s3_bucket.overflow[0].id

  rule {
    id     = "expire-1d"
    status = "Enabled"

    filter {}

    expiration {
      days = 1
    }
  }
}
