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
    Address the digest and instant alerts are sent TO. Verified as an SES identity because the
    SES sandbox only permits sending to verified addresses — fine, we only ever email ourselves,
    so we will never request production access.
  EOT
  type        = string
}

variable "sender_domain" {
  description = <<-EOT
    Domain the digest is sent FROM. Must be a domain we actually own, so Easy DKIM can sign the
    mail and the signature aligns with the From address.

    A free-mail domain (gmail.com) cannot work here: SES is not an authorised sender for it, so
    the mail fails SPF/DKIM and Gmail treats it as spoofing. Verified the hard way — SES accepted
    our first send with 0 bounces and it still never reached the inbox.
  EOT
  type        = string

  validation {
    condition = !contains(
      ["gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com"],
      lower(var.sender_domain)
    )
    error_message = "sender_domain must be a domain you own. SES cannot authenticate mail sent as a free-mail provider's domain, so the digest would land in spam."
  }
}

variable "sender_local_part" {
  description = "Mailbox part of the sender address (jobs -> jobs@lubobali.com). Needs no real mailbox; it only sends."
  type        = string
  default     = "jobs"
}
