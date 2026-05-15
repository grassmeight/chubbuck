# =============================================================================
# GitHub Actions OIDC trust + deploy role
# =============================================================================
#
# Lets a GitHub Actions workflow in `var.github_repo` assume an IAM role with
# permission to push to this project's ECR repo and update the Lambda function
# — no long-lived access keys stored in GitHub Secrets.
#
# All resources are gated on `var.github_repo`: leave it empty (default) to
# skip OIDC provisioning entirely. Set it (e.g. "myorg/chubbuck") to enable.
#
# The matching workflow lives at .github/workflows/deploy.yml in the repo
# root. The workflow uses the role ARN from the `github_actions_role_arn`
# output (added to GitHub as a repository variable named AWS_ROLE_ARN).

locals {
  github_oidc_enabled = var.github_repo != ""
}

# AWS accepts only one OIDC provider per (account, URL). If you already have a
# `token.actions.githubusercontent.com` provider in this account (e.g. from
# another project), import it instead of creating a new one:
#   terraform import aws_iam_openid_connect_provider.github \
#     arn:aws:iam::<account>:oidc-provider/token.actions.githubusercontent.com
resource "aws_iam_openid_connect_provider" "github" {
  count = local.github_oidc_enabled ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]

  # AWS no longer validates this thumbprint for the GitHub OIDC issuer (it's
  # treated as a library-of-record formality since mid-2023), but the API
  # still requires a non-empty list. Use the well-known GitHub root cert
  # thumbprint for compatibility with older API behavior.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

data "aws_iam_policy_document" "github_actions_assume" {
  count = local.github_oidc_enabled ? 1 : 0

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github[0].arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Restrict to pushes on the `main` branch of the configured repo. Forked
    # PRs run with a different `sub` and so cannot assume the role — this is
    # intentional, since they'd otherwise be able to deploy arbitrary code.
    # Loosen to `repo:${var.github_repo}:*` if you also want feature-branch
    # builds, but understand the trade-off.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "github_actions" {
  count = local.github_oidc_enabled ? 1 : 0

  name               = "${var.project_name}-github-actions"
  description        = "CI/CD role assumed by GitHub Actions in ${var.github_repo} via OIDC. Allows ECR push + Lambda update for ${var.project_name} only."
  assume_role_policy = data.aws_iam_policy_document.github_actions_assume[0].json
}

data "aws_iam_policy_document" "github_actions_deploy" {
  count = local.github_oidc_enabled ? 1 : 0

  # ECR auth token is a global action (no resource scoping possible).
  statement {
    sid       = "ECRAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  # ECR push/pull, scoped to this project's repo only.
  statement {
    sid = "ECRPushPull"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:DescribeRepositories",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.app.arn]
  }

  # Lambda update + status polling, scoped to this project's function only.
  statement {
    sid = "LambdaUpdate"
    actions = [
      "lambda:UpdateFunctionCode",
      "lambda:GetFunction",
      "lambda:GetFunctionConfiguration",
    ]
    resources = [aws_lambda_function.app.arn]
  }
}

resource "aws_iam_role_policy" "github_actions_deploy" {
  count = local.github_oidc_enabled ? 1 : 0

  name   = "${var.project_name}-github-actions-deploy"
  role   = aws_iam_role.github_actions[0].id
  policy = data.aws_iam_policy_document.github_actions_deploy[0].json
}
