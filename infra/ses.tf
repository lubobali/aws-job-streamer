# The verified sender/recipient for the digest and instant alerts.
#
# Phase 0's checklist claimed this was verified; it was not — the account had zero SES identities
# when Terraform first ran. Creating it here means it is provisioned, not remembered.
#
# AWS emails a confirmation link on create. The identity stays UNVERIFIED until that link is
# clicked, and SES will refuse to send until then. Terraform cannot click it for you.
#
# The account is in the SES sandbox, which only permits sending TO verified addresses. That is
# fine and we will not request production access: Lubo only ever emails himself (GUARDRAILS).

resource "aws_ses_email_identity" "digest" {
  email = var.digest_email
}
