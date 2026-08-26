# Dark Factory — IAM-as-code for the REAL AWS Security Agent integration.
#
# The Security Agent code-review runs headlessly from a hub-side Argo step:
#   upload {source archive, unified diff} to S3 -> securityagent create-code-review
#   -> start-code-review-job -> list-findings. No GitHub App, no OAuth.
#
# This file codifies the three IAM/infra pieces that path needs (all validated
# live 2026-07-16). Applied standalone, not by ArgoCD — the repo keeps
# addon IAM as committed Terraform, and the agent SPACE/APPLICATION (which have no
# Terraform/ACK provider yet) are reconciled separately by the chart's PreSync Job.
#
#   1. aws_iam_role.securityagent_service  — the role the Security Agent SERVICE
#      assumes (trusts securityagent.amazonaws.com) to read the S3 diff bucket +
#      write its logs. Passed as create-code-review --service-role.
#   2. aws_iam_role.df_securityagent_irsa  — the IRSA role the hub Argo workflow
#      (+ bootstrap Job) SA assumes to call the securityagent API and stage diffs
#      in S3. Bound to the two ServiceAccounts in the argo namespace.
#   3. aws_s3_bucket.diff                   — the private bucket holding per-run
#      source archives + unified diffs the agent reads.
#
# GOTCHA (cost 30 min live): the hub cluster's OIDC issuer had NO IAM OIDC
# provider. IRSA tokens (correct issuer/aud/sub) fail AssumeRoleWithWebIdentity
# with InvalidIdentityToken until the provider exists. This file (re)creates it.

variable "region" {
  type    = string
  default = "us-west-2"
}

variable "cluster_name" {
  type    = string
  default = "hub"
}

# The argo namespace ServiceAccounts allowed to assume the IRSA role.
variable "workflow_service_accounts" {
  type = list(string)
  default = [
    "system:serviceaccount:argo:dark-factory-workflow",
    "system:serviceaccount:argo:dark-factory-bootstrap",
  ]
}

data "aws_caller_identity" "current" {}

data "aws_eks_cluster" "hub" {
  name = var.cluster_name
}

locals {
  account_id  = data.aws_caller_identity.current.account_id
  # https://oidc.eks.<region>.amazonaws.com/id/XXXX  ->  strip scheme for ARNs/conditions.
  oidc_issuer = replace(data.aws_eks_cluster.hub.identity[0].oidc[0].issuer, "https://", "")
  diff_bucket = "dark-factory-secagent-${local.account_id}-${var.region}"
}

# ── 0. IAM OIDC provider for the hub cluster (IRSA prerequisite) ──────────────
# thumbprint = the EKS/Amazon root CA; STS ignores the value for EKS-hosted OIDC
# but the API requires one. Import if it already exists:
#   terraform import aws_iam_openid_connect_provider.hub <provider-arn>
resource "aws_iam_openid_connect_provider" "hub" {
  url             = "https://${local.oidc_issuer}"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["9e99a48a9960b14926bb7f3b02e22da2b0ab7280"]

  tags = { platform = "open-agent-platform", capability = "dark-factory" }
}

# ── 1. Security Agent SERVICE role (trusts securityagent.amazonaws.com) ───────
data "aws_iam_policy_document" "securityagent_service_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["securityagent.amazonaws.com"]
    }
    # Confused-deputy guards: only this account + this account's agent spaces.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:securityagent:${var.region}:${local.account_id}:agent-space/*"]
    }
  }
}

data "aws_iam_policy_document" "securityagent_service_perms" {
  statement {
    sid       = "ReadDiffBucket"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = ["arn:aws:s3:::${local.diff_bucket}", "arn:aws:s3:::${local.diff_bucket}/*"]
  }
  statement {
    sid       = "CreateLogGroup"
    effect    = "Allow"
    actions   = ["logs:CreateLogGroup"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/securityagent/dark-factory*"]
  }
  statement {
    sid       = "WriteLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/securityagent/dark-factory*:log-stream:*"]
  }
}

resource "aws_iam_role" "securityagent_service" {
  name                 = "df-securityagent-service-role"
  path                 = "/service-role/"
  assume_role_policy   = data.aws_iam_policy_document.securityagent_service_trust.json
  description          = "Role the AWS Security Agent service assumes to read the Dark Factory diff bucket + write logs"
  tags                 = { platform = "open-agent-platform", capability = "dark-factory" }
}

resource "aws_iam_role_policy" "securityagent_service" {
  name   = "df-securityagent-policy"
  role   = aws_iam_role.securityagent_service.id
  policy = data.aws_iam_policy_document.securityagent_service_perms.json
}

# ── 2. IRSA role for the hub Argo workflow + bootstrap SAs ────────────────────
data "aws_iam_policy_document" "irsa_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = ["arn:aws:iam::${local.account_id}:oidc-provider/${local.oidc_issuer}"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer}:sub"
      values   = var.workflow_service_accounts
    }
  }
}

data "aws_iam_policy_document" "irsa_perms" {
  # Agent-space + application lifecycle (used by the idempotent PreSync bootstrap Job).
  statement {
    sid    = "SecurityAgentSpaceLifecycle"
    effect = "Allow"
    actions = [
      "securityagent:CreateAgentSpace", "securityagent:GetAgentSpace",
      "securityagent:ListAgentSpaces", "securityagent:UpdateAgentSpace",
      "securityagent:CreateApplication", "securityagent:GetApplication",
      "securityagent:ListApplications",
    ]
    resources = ["*"]
  }
  # Per-run code review (used by the df-run security step).
  statement {
    sid    = "SecurityAgentCodeReview"
    effect = "Allow"
    actions = [
      "securityagent:CreateCodeReview", "securityagent:StartCodeReviewJob",
      "securityagent:StopCodeReviewJob", "securityagent:BatchGetCodeReviewJobs",
      "securityagent:BatchGetCodeReviews", "securityagent:ListCodeReviews",
      "securityagent:ListFindings", "securityagent:BatchGetFindings",
    ]
    resources = ["*"]
  }
  statement {
    sid       = "StageDiffsInS3"
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
    resources = ["arn:aws:s3:::${local.diff_bucket}", "arn:aws:s3:::${local.diff_bucket}/*"]
  }
  # Hand the service role to the agent (scoped so it can ONLY be passed to it).
  statement {
    sid       = "PassServiceRoleToSecurityAgent"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.securityagent_service.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["securityagent.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "df_securityagent_irsa" {
  name               = "df-securityagent-irsa"
  assume_role_policy = data.aws_iam_policy_document.irsa_trust.json
  description        = "Dark Factory hub workflow+bootstrap IRSA: securityagent lifecycle + code review + diff bucket"
  tags               = { platform = "open-agent-platform", capability = "dark-factory" }
}

resource "aws_iam_role_policy" "df_securityagent_irsa" {
  name   = "df-securityagent-irsa-policy"
  role   = aws_iam_role.df_securityagent_irsa.id
  policy = data.aws_iam_policy_document.irsa_perms.json
}

# ── 3. Private S3 bucket for per-run source archives + diffs ──────────────────
resource "aws_s3_bucket" "diff" {
  bucket = local.diff_bucket
  tags   = { platform = "open-agent-platform", capability = "dark-factory" }
}

resource "aws_s3_bucket_public_access_block" "diff" {
  bucket                  = aws_s3_bucket.diff.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "diff" {
  bucket = aws_s3_bucket.diff.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Diffs are ephemeral per-run inputs; expire them so the bucket doesn't grow.
resource "aws_s3_bucket_lifecycle_configuration" "diff" {
  bucket = aws_s3_bucket.diff.id
  rule {
    id     = "expire-run-artifacts"
    status = "Enabled"
    filter { prefix = "runs/" }
    expiration { days = 7 }
  }
}

output "securityagent_service_role_arn" {
  value = aws_iam_role.securityagent_service.arn
}
output "df_securityagent_irsa_role_arn" {
  value = aws_iam_role.df_securityagent_irsa.arn
}
output "diff_bucket" {
  value = aws_s3_bucket.diff.id
}
