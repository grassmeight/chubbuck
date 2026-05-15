output "ecr_repository_url" {
  description = "Push the container image here. Use as the docker registry hostname."
  value       = aws_ecr_repository.app.repository_url
}

output "lambda_function_name" {
  description = "Function name. Use with `aws lambda update-function-code` to push a new image without re-running terraform."
  value       = aws_lambda_function.app.function_name
}

output "lambda_function_url" {
  description = "Direct Lambda Function URL (bypasses CloudFront — useful for debugging cold starts and CORS issues in isolation)."
  value       = aws_lambda_function_url.app.function_url
}

output "cloudfront_domain" {
  description = "Public site URL — the user-facing endpoint. Both the static UI and /api/* go through here."
  value       = "https://${aws_cloudfront_distribution.main.domain_name}/"
}

output "s3_static_bucket" {
  description = "Upload index.html and any other static assets here. CloudFront serves them at /."
  value       = aws_s3_bucket.static.bucket
}

output "s3_uploads_bucket" {
  description = "Browser uploads land here via presigned PUT URLs. Lambda fetches from here on /api/convert-s3."
  value       = aws_s3_bucket.uploads.bucket
}

output "overflow_bucket" {
  description = "Optional bucket for output PDFs >20 MB (only when enable_overflow_bucket = true)."
  value       = var.enable_overflow_bucket ? aws_s3_bucket.overflow[0].bucket : null
}

output "cloudwatch_log_group" {
  description = "CloudWatch Logs group for the Lambda function. Tail with `aws logs tail <name> --follow`."
  value       = aws_cloudwatch_log_group.lambda.name
}

output "github_actions_role_arn" {
  description = "IAM role ARN GitHub Actions assumes via OIDC. Add to the repo's variables as AWS_ROLE_ARN. Null when github_repo isn't set."
  value       = length(aws_iam_role.github_actions) > 0 ? aws_iam_role.github_actions[0].arn : null
}
