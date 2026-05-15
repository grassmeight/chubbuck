resource "aws_ecr_repository" "app" {
  name = var.project_name

  # We re-push :latest from local/CI so tag mutability needs to be on.
  image_tag_mutability = "MUTABLE"

  # Personal-project convenience: lets `terraform destroy` wipe images so the
  # repo deletes cleanly. Don't enable in production.
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

# ECR free tier covers 500 MB. The LibreOffice image is ~800 MB, so we round
# up to the smallest paid bucket ($0.10/GB-mo). Keeping only the live image +
# 2 rollbacks costs cents per month and avoids unbounded storage growth from
# CI rebuilds.
resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep only the 3 most recent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 3
      }
      action = { type = "expire" }
    }]
  })
}
