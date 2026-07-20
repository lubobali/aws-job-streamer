# Phase 4 — the scheduled pipeline.
#
# EventBridge -> Lambda -> runner.run(). The Lambda IS the local runner, packaged: same code path,
# so nothing about "it runs in Lambda now" can change behaviour (lambda_handler.py is thin glue).
# The deployment zip is built by build_lambda.sh (app code + httpx + profile.example.json) BEFORE
# `terraform apply`; boto3 comes from the Lambda runtime.

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  function_name = "${var.project}-poll"
  lambda_zip    = "${path.module}/../dist/lambda.zip"
}

# ---- IAM: least privilege. The role can touch exactly the four things a run needs. ----

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.project}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# CloudWatch Logs — the managed policy is the standard, audited grant for exactly this.
resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "lambda_permissions" {
  # Dedup table: read to check "seen" (BatchGetItem), write scored jobs (save_new uses the boto3
  # batch writer = BatchWriteItem, not PutItem), update to mark emailed. Verified against dedup.py.
  statement {
    sid = "DedupTable"
    actions = [
      "dynamodb:BatchGetItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
    ]
    resources = [aws_dynamodb_table.jobs.arn]
  }

  # Send the digest. SES has no resource-level ARN for SendEmail that is worth scoping here; the
  # FromAddress condition pins it to our own verified sender so the role cannot send as anyone else.
  statement {
    sid       = "SendDigest"
    actions   = ["ses:SendEmail", "ses:SendRawEmail"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "ses:FromAddress"
      values   = [local.sender_address]
    }
  }

  # Read the job-API + LLM secrets. Scoped to this project's SSM prefix only.
  statement {
    sid       = "ReadSecrets"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:parameter${local.ssm_prefix}/*"]
  }

  # Decrypt those SecureStrings — but ONLY when SSM is the caller (the AWS-managed aws/ssm key).
  statement {
    sid       = "DecryptSecrets"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${data.aws_region.current.region}.amazonaws.com"]
    }
  }

  # The daily heartbeat reads its own last-24h heartbeat lines via Logs Insights. Read-only query
  # actions; GetQueryResults/StopQuery are resourceless so they cannot be scoped to the log group.
  statement {
    sid       = "HeartbeatLogInsights"
    actions   = ["logs:StartQuery", "logs:GetQueryResults", "logs:StopQuery"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${var.project}-lambda"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda_permissions.json
}

# ---- Log group owned by Terraform (not auto-created), so retention is set and it is destroyable. ----

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days
}

# ---- The function ----

resource "aws_lambda_function" "poll" {
  function_name = local.function_name
  description   = "Fetch -> prefilter -> dedup -> score -> rank -> email. One full run per invocation."
  role          = aws_iam_role.lambda.arn
  handler       = "aws_job_streamer.lambda_handler.handler"
  runtime       = "python3.13"
  timeout       = var.lambda_timeout
  memory_size   = var.lambda_memory

  filename         = local.lambda_zip
  source_code_hash = filebase64sha256(local.lambda_zip)

  environment {
    variables = {
      # AWS_REGION is reserved (Lambda sets it), so it is deliberately absent here — runner reads it.
      JOBS_TABLE              = aws_dynamodb_table.jobs.name
      DIGEST_SENDER           = local.sender_address
      DIGEST_RECIPIENT        = var.digest_email
      MAX_SCORE_PER_RUN       = tostring(var.max_score_per_run)
      COLD_START_MAX_AGE_DAYS = tostring(var.max_age_days)
      OPENROUTER_SSM_NAME     = aws_ssm_parameter.openrouter_api_key.name
      # Only set SCORER_MODEL when non-empty, so an unset var falls through to the code default.
      SCORER_MODEL = var.scorer_model
    }
  }

  depends_on = [
    aws_iam_role_policy.lambda,
    aws_cloudwatch_log_group.lambda,
  ]
}

# ---- The schedule (starts DISABLED — test-invoke by hand first, then flip schedule_enabled). ----

resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "${var.project}-schedule"
  description         = "Runs the job-streamer pipeline on a cadence."
  schedule_expression = var.schedule_expression
  state               = var.schedule_enabled ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "lambda" {
  rule = aws_cloudwatch_event_rule.schedule.name
  arn  = aws_lambda_function.poll.arn
}

resource "aws_lambda_permission" "events" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.poll.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}

# ---- Daily heartbeat: one "still alive, here's the 24h summary" email, so silence is never scary. ----

resource "aws_cloudwatch_event_rule" "daily_heartbeat" {
  name                = "${var.project}-daily-heartbeat"
  description         = "Fires once a day; the Lambda emails a 24h run summary instead of polling."
  schedule_expression = var.heartbeat_schedule
  state               = var.schedule_enabled ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "daily_heartbeat" {
  rule  = aws_cloudwatch_event_rule.daily_heartbeat.name
  arn   = aws_lambda_function.poll.arn
  input = jsonencode({ mode = "heartbeat" }) # tells the handler to summarize, not poll
}

resource "aws_lambda_permission" "daily_heartbeat" {
  statement_id  = "AllowDailyHeartbeat"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.poll.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily_heartbeat.arn
}
