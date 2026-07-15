variable "region" {
  description = "AWS region. Everything lives in one region — do not mix (PLAN.md Step 5)."
  type        = string
  default     = "us-east-2"
}

variable "project" {
  description = "Name prefix for every resource, so they are obvious in the console and in the bill."
  type        = string
  default     = "aws-job-streamer"
}

variable "digest_email" {
  description = <<-EOT
    Address the daily digest and instant alerts are sent to, and the verified SES sender.
    AWS emails a confirmation link when this identity is created; the identity stays unverified
    until that link is clicked. SES starts in the sandbox, which only allows sending TO verified
    addresses — fine, because we only ever email ourselves.
  EOT
  type        = string
}
