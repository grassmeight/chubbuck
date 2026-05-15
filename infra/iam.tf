# =============================================================================
# Lambda execution role
# =============================================================================

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_exec" {
  name               = "${var.project_name}-lambda-exec"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# AWSLambdaBasicExecutionRole grants CreateLogStream + PutLogEvents on the
# function's log group. Required for any Lambda that should produce logs.
resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Self-invoke permission for the warm-pool fan-out: on the keepalive tick
# the handler invokes (warm_pool_size - 1) parallel copies of itself so that
# multiple execution environments stay warm in parallel.
data "aws_iam_policy_document" "lambda_self_invoke" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.app.arn]
  }
}

resource "aws_iam_role_policy" "lambda_self_invoke" {
  name   = "${var.project_name}-lambda-self-invoke"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_self_invoke.json
}

# Uploads bucket access. The handler needs:
#   - PutObject: implicit, used to sign the presigned PUT URL handed to the
#     browser. The browser uses that URL; Lambda itself doesn't write.
#   - GetObject: fetch the uploaded file in /api/convert-s3.
#   - DeleteObject: clean up immediately after fetch (the 1-day lifecycle
#     is a fallback for orphans, but the happy path deletes synchronously).
data "aws_iam_policy_document" "lambda_uploads_s3" {
  statement {
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:DeleteObject",
    ]
    resources = ["${aws_s3_bucket.uploads.arn}/*"]
  }
}

resource "aws_iam_role_policy" "lambda_uploads_s3" {
  name   = "${var.project_name}-lambda-uploads-s3"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_uploads_s3.json
}

# Optional: overflow bucket access (presigned PUT/GET for >20 MB outputs).
data "aws_iam_policy_document" "lambda_overflow_s3" {
  count = var.enable_overflow_bucket ? 1 : 0

  statement {
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:DeleteObject",
    ]
    resources = ["${aws_s3_bucket.overflow[0].arn}/*"]
  }
}

resource "aws_iam_role_policy" "lambda_overflow_s3" {
  count  = var.enable_overflow_bucket ? 1 : 0
  name   = "${var.project_name}-lambda-overflow-s3"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_overflow_s3[0].json
}

# =============================================================================
# EventBridge Scheduler role
# =============================================================================

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    # Confused-deputy guard: only schedules in our account can assume this role.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.project_name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler_invoke_lambda" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.app.arn]
  }
}

resource "aws_iam_role_policy" "scheduler_invoke_lambda" {
  name   = "${var.project_name}-scheduler-invoke"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler_invoke_lambda.json
}
