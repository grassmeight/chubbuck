variable "project_name" {
  description = "Used as a prefix for all resource names. Lowercase, kebab-case."
  type        = string
  default     = "chubbuck"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}$", var.project_name))
    error_message = "project_name must be 2-31 chars, start with a letter, and contain only lowercase letters, digits, and hyphens."
  }
}

variable "aws_region" {
  description = "AWS region for the regional resources (Lambda, ECR, S3, EventBridge). CloudFront is global."
  type        = string
  default     = "us-east-1"
}

variable "lambda_memory_mb" {
  description = "Lambda memory in MB. CPU scales with memory; 2048 is the sweet spot for LibreOffice (faster conversion → lower total GB-s billed)."
  type        = number
  default     = 2048
}

variable "lambda_timeout_seconds" {
  description = "Hard cutoff per request. A typical scene takes 5-15s on 2048MB on a warm env. The first request after a cold start also pays the LibreOffice pre-warm tax (~30-50s with the full libreoffice metapackage), so 120s leaves comfortable headroom."
  type        = number
  default     = 120
}

variable "lambda_ephemeral_storage_mb" {
  description = "/tmp size in MB. Default 512 is too small for LibreOffice's profile + a multi-MB input + output. 1024 is comfortable."
  type        = number
  default     = 1024

  validation {
    condition     = var.lambda_ephemeral_storage_mb >= 512 && var.lambda_ephemeral_storage_mb <= 10240
    error_message = "lambda_ephemeral_storage_mb must be between 512 and 10240."
  }
}

variable "warm_pool_size" {
  description = "Number of warm Lambda execution environments to keep alive. Also the reserved concurrency cap. Free-tier-safe up to ~3 at a 5-min keepalive cadence."
  type        = number
  default     = 2

  validation {
    condition     = var.warm_pool_size >= 1 && var.warm_pool_size <= 5
    error_message = "warm_pool_size should be between 1 and 5."
  }
}

variable "image_tag" {
  description = "ECR image tag the Lambda runs. Push this tag to ECR before the first apply. Subsequent image updates via 'aws lambda update-function-code' don't require terraform apply (image_uri is in lifecycle.ignore_changes)."
  type        = string
  default     = "latest"
}

variable "log_retention_days" {
  description = "CloudWatch log retention. 1 day keeps free-tier ingestion comfortable; bump to 7 or 14 while debugging."
  type        = number
  default     = 1
}

variable "enable_overflow_bucket" {
  description = "Create the optional S3 overflow bucket for >20 MB output files. Free for 12 months only — leave false unless you actually have large outputs."
  type        = bool
  default     = false
}

variable "github_repo" {
  description = "GitHub repository (\"owner/name\") allowed to assume the CI/CD role via OIDC. Empty string disables OIDC role creation entirely. Example: \"shira401/chubbuck\"."
  type        = string
  default     = ""

  validation {
    condition     = var.github_repo == "" || can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repo))
    error_message = "github_repo must be empty or in the form \"owner/name\"."
  }
}

variable "cloudfront_price_class" {
  description = "CloudFront price class. PriceClass_100 = US/Canada/Europe edges only (cheapest, covers your audience). PriceClass_All = global."
  type        = string
  default     = "PriceClass_100"

  validation {
    condition     = contains(["PriceClass_100", "PriceClass_200", "PriceClass_All"], var.cloudfront_price_class)
    error_message = "cloudfront_price_class must be one of PriceClass_100, PriceClass_200, PriceClass_All."
  }
}
