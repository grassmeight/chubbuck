# Copy to terraform.tfvars (gitignored) and edit. Defaults in variables.tf
# are sensible — most users only need to override project_name (if you want
# something other than "chubbuck") or aws_region.

# project_name             = "chubbuck"
# aws_region               = "us-east-1"
# warm_pool_size           = 2
# enable_overflow_bucket   = false
# log_retention_days       = 1
# image_tag                = "latest"

# Enables the GitHub Actions OIDC role for CI/CD. Leave empty to skip.
# After setting this and running `terraform apply`, copy the
# `github_actions_role_arn` output into the GitHub repo's variables as
# AWS_ROLE_ARN — the `.github/workflows/deploy.yml` workflow reads it.
# github_repo              = "owner/repo"
