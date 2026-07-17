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

# ---- Phase 4: the scheduled Lambda ----

variable "schedule_expression" {
  description = <<-EOT
    EventBridge cadence for the poll. `rate(4 hours)` = 6 runs/day: prompt enough to be early to a
    new posting, drains a cold start (<=200 scored/run) in a day or two, and — because the dedup
    gate + 65 floor mean most runs email nothing — sends only when a genuinely new strong match
    appears, not on a timer. Change freely; it is one apply.
  EOT
  type        = string
  default     = "rate(4 hours)"
}

variable "schedule_enabled" {
  description = <<-EOT
    Whether the EventBridge rule is ENABLED. Starts false so the Lambda can be deployed and
    test-invoked by hand before it begins emailing on a schedule — flip to true once verified.
  EOT
  type        = bool
  default     = false
}

variable "lambda_timeout" {
  description = <<-EOT
    Lambda timeout (s). Scoring is sequential HTTP, ~2s/job; a capped cold-start run (200 jobs) is
    ~400s of scoring + fetch, so 900 (the Lambda max) leaves headroom. A timeout loses the whole
    run (nothing is stored mid-flight), so err high — cost is per-ms and trivial at this volume.
  EOT
  type        = number
  default     = 900
}

variable "lambda_memory" {
  description = "Lambda memory (MB). The work is I/O-bound (HTTP), not memory-bound; 512 is plenty."
  type        = number
  default     = 512
}

variable "max_score_per_run" {
  description = "Cold-start guard: max NEW jobs scored per run. 200 * ~$0.003 = ~$0.60 worst case."
  type        = number
  default     = 200
}

variable "max_age_days" {
  description = "Cold-start guard: skip postings older than this (unknown-date kept). Freshness cut."
  type        = number
  default     = 30
}

variable "log_retention_days" {
  description = "CloudWatch log retention for the Lambda. 14 days is enough to debug a bad run."
  type        = number
  default     = 14
}

variable "no_invocation_window_seconds" {
  description = <<-EOT
    How long with zero runs before the "schedule is broken" alarm fires. Should exceed one
    schedule interval so a single skipped tick is not an alert. 28800 (8h) covers two missed
    runs at the default rate(4 hours). Only used when schedule_enabled = true.
  EOT
  type        = number
  default     = 28800
}
