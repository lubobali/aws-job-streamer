# Phase 4 monitoring — so a failure is never silent (the point of A2's heartbeat).
#
# Three ways a scheduled pipeline can fail, and an alarm for each:
#   1. The invocation itself errors (exception, timeout, OOM) -> AWS/Lambda Errors.
#   2. The run COMPLETES but is unhealthy (all sources down) -> the heartbeat logs "health=error",
#      caught by a log metric filter. This is the failure that would otherwise look like success.
#   3. The schedule stops firing entirely -> Invocations drops to zero. Only meaningful once the
#      schedule is live, so it is created only when schedule_enabled = true.
# All three notify one SNS topic; Lubo gets an email. (He must confirm the subscription once.)

resource "aws_sns_topic" "alerts" {
  name = "${var.project}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.digest_email # AWS emails a one-time confirmation link; click it to arm alerts.
}

# 1. Hard invocation failure.
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${var.project}-lambda-errors"
  alarm_description   = "The pipeline Lambda threw / timed out / ran out of memory."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.poll.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching" # no invocations != an error; alarm 3 owns that case
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

# 2. Completed-but-unhealthy run — the heartbeat said health=error.
resource "aws_cloudwatch_log_metric_filter" "run_unhealthy" {
  name           = "${var.project}-run-unhealthy"
  log_group_name = aws_cloudwatch_log_group.lambda.name
  pattern        = "\"health=error\"" # matches the assess_run ERROR heartbeat line

  metric_transformation {
    name          = "RunUnhealthy"
    namespace     = "JobStreamer"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "run_unhealthy" {
  alarm_name          = "${var.project}-run-unhealthy"
  alarm_description   = "A run completed but reported health=error (e.g. every source failed)."
  namespace           = "JobStreamer"
  metric_name         = aws_cloudwatch_log_metric_filter.run_unhealthy.metric_transformation[0].name
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# 3. The schedule stopped firing — only while the schedule is actually live.
resource "aws_cloudwatch_metric_alarm" "no_invocations" {
  count               = var.schedule_enabled ? 1 : 0
  alarm_name          = "${var.project}-no-invocations"
  alarm_description   = "No pipeline runs in the alarm window — the schedule may be broken."
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions          = { FunctionName = aws_lambda_function.poll.function_name }
  statistic           = "Sum"
  period              = var.no_invocation_window_seconds
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching" # no datapoints at all = nothing ran = alert
  alarm_actions       = [aws_sns_topic.alerts.arn]
}
