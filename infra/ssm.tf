# Job-API secrets, replacing the local .env.
#
# The secret VALUES are deliberately NOT in Terraform. Terraform state stores values in
# PLAINTEXT — even for a SecureString — so putting the real Adzuna key here would copy it into
# terraform.tfstate on this box and into CI's state in Phase 5. Terraform is not a secret store.
#
# So: Terraform owns the parameter's EXISTENCE, name, type and IAM surface. The value is written
# once, out of band:
#
#   aws ssm put-parameter --name /aws-job-streamer/adzuna/app_key \
#     --value "<key>" --type SecureString --overwrite --region us-east-2
#
# `ignore_changes = [value]` then stops Terraform reverting that real value back to the
# placeholder on the next apply.

locals {
  # NOT "/${var.project}". SSM reserves every parameter name beginning with "aws" or "ssm"
  # (case-insensitive) and this project is literally called aws-job-streamer, so the obvious
  # path is rejected with AccessDeniedException: "No access to reserved parameter name".
  # The prefix therefore drops the "aws-"; every other resource still uses var.project.
  ssm_prefix = "/job-streamer"
}

resource "aws_ssm_parameter" "adzuna_app_id" {
  name        = "${local.ssm_prefix}/adzuna/app_id"
  description = "Adzuna app_id. Not secret — Adzuna publishes it in every redirect url it returns."
  type        = "String"
  value       = "PLACEHOLDER — set with: aws ssm put-parameter --overwrite"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "adzuna_app_key" {
  name        = "${local.ssm_prefix}/adzuna/app_key"
  description = "Adzuna app_key. THE SECRET — never in git, never in terraform state."
  type        = "SecureString" # encrypted at rest with the free AWS-managed key
  value       = "PLACEHOLDER — set with: aws ssm put-parameter --overwrite"

  lifecycle {
    ignore_changes = [value]
  }
}
