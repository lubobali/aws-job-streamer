# The dedup table — the thing that makes "never see the same job twice" true.
#
# It is also what keeps the bill at ~$0: the 15-minute poll checks this table BEFORE calling
# Bedrock, so a run that finds nothing new scores nothing and costs nothing.

resource "aws_dynamodb_table" "jobs" {
  name         = "${var.project}-jobs"
  billing_mode = "PAY_PER_REQUEST" # no capacity to guess at; a handful of jobs/day costs cents
  hash_key     = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }

  # job_id = sha256(source + source_id) — PLAN.md Decision Log #1. Keyed on the source's own id
  # rather than the url: Adzuna's url carries a per-request token and would mint a new id on
  # every poll, re-emailing the same job forever.
  #
  # No secondary index yet, deliberately. The digest needs "jobs not yet emailed", which at a few
  # hundred rows is a cheap scan. A GSI gets added when there is a query that needs it and real
  # data to shape it — not on a guess.

  point_in_time_recovery {
    # Off: PITR is billed, and this table is a rebuildable cache. Every row can be re-fetched
    # from the source APIs; losing it costs one poll cycle, not data.
    enabled = false
  }

  lifecycle {
    prevent_destroy = true # losing the dedup table means re-emailing every job ever seen
  }
}
