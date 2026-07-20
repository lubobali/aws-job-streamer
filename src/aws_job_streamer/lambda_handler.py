"""AWS Lambda entry point — EventBridge invokes this, it calls `runner.run()`.

Deliberately thin: the scheduled digest and a hand-run digest are the SAME code path (`runner.run`),
so nothing about "it's in Lambda now" can change behaviour. This module only does the two things
that are genuinely Lambda-specific:

  1. Pull the OpenRouter key from SSM at runtime (not a plaintext Lambda env var — a secret in the
     function config is visible to anyone with `lambda:GetFunctionConfiguration`). The rest of the
     config is non-secret and comes from env vars set by Terraform.
  2. Make sure our INFO heartbeat actually reaches CloudWatch, then return a small JSON summary.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import boto3

from aws_job_streamer import runner
from aws_job_streamer.daily_summary import parse_heartbeat, render_daily, summarize

_OPENROUTER_SSM_NAME = os.environ.get("OPENROUTER_SSM_NAME", "/job-streamer/openrouter/api_key")

# Per-source credentials pulled from SSM at runtime. Names match ssm.tf; values set out of band.
_SSM_SECRETS = {
    "ADZUNA_APP_ID": os.environ.get("ADZUNA_APP_ID_SSM_NAME", "/job-streamer/adzuna/app_id"),
    "ADZUNA_APP_KEY": os.environ.get("ADZUNA_APP_KEY_SSM_NAME", "/job-streamer/adzuna/app_key"),
    "USAJOBS_API_KEY": os.environ.get("USAJOBS_API_KEY_SSM_NAME", "/job-streamer/usajobs/api_key"),
    "USAJOBS_EMAIL": os.environ.get("USAJOBS_EMAIL_SSM_NAME", "/job-streamer/usajobs/email"),
}


def _load_secret_into_env(ssm_name: str, env_key: str, region: str) -> None:
    """Fetch a SecureString from SSM into the environment, unless it is already set.

    Already-set wins so a local run (with a real `.env`) never hits SSM — the same override rule
    as `load_dotenv`. In Lambda the env var is absent, so this reads and decrypts it once.
    """
    if os.environ.get(env_key):
        return
    ssm = boto3.client("ssm", region_name=region)
    value = ssm.get_parameter(Name=ssm_name, WithDecryption=True)["Parameter"]["Value"]
    os.environ[env_key] = value


def handler(event: Any, context: Any) -> dict[str, Any]:  # noqa: ANN401 — Lambda event/context
    """Run one full cycle and email any new strong matches. Returns a summary for the logs/console.

    Raising propagates to Lambda as an invocation error, which the CloudWatch Errors alarm catches
    — so a hard failure is never silent. A degraded-but-completed run is caught instead by the
    heartbeat's WARN/ERROR line (see `runner.assess_run`).
    """
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("aws_job_streamer").setLevel(logging.INFO)

    region = os.environ.get("AWS_REGION", "us-east-2")

    # A second EventBridge rule fires once a day with {"mode": "heartbeat"}: no pipeline, no LLM —
    # just email a 24h summary so silence never reads as "is it broken?".
    if isinstance(event, dict) and event.get("mode") == "heartbeat":
        return _daily_heartbeat(region)

    _load_secret_into_env(_OPENROUTER_SSM_NAME, "OPENROUTER_API_KEY", region)
    for env_key, ssm_name in _SSM_SECRETS.items():
        _load_secret_into_env(ssm_name, env_key, region)

    result, digest_result = runner.run()
    c = result.counts
    return {
        "fetched": c.fetched,
        "eligible": c.eligible,
        "new": c.new,
        "scored": c.scored,
        "skipped": c.skipped,
        "digest": c.digest,
        "deferred": c.deferred,
        "source_failures": c.source_failures,
        "emailed": digest_result.count if digest_result and digest_result.sent else 0,
        "message_id": digest_result.message_id if digest_result else None,
    }


def _daily_heartbeat(region: str) -> dict[str, Any]:
    """Read the last 24h of heartbeat lines from CloudWatch, summarize, and email it. No LLM."""
    logs = boto3.client("logs", region_name=region)
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "aws-job-streamer-poll")
    end = int(time.time())
    query = logs.start_query(
        logGroupName=f"/aws/lambda/{function_name}",
        startTime=end - 86_400,
        endTime=end,
        queryString="fields @message | filter @message like /job-streamer run/ | limit 2000",
    )
    query_id = query["queryId"]
    results: list[list[dict[str, str]]] = []
    for _ in range(30):  # Logs Insights is async; a day of a small log group completes in seconds.
        response = logs.get_query_results(queryId=query_id)
        if response["status"] in ("Complete", "Failed", "Cancelled"):
            results = response.get("results", [])
            break
        time.sleep(1)

    rows = []
    for row in results:
        message = next((f["value"] for f in row if f["field"] == "@message"), "")
        parsed = parse_heartbeat(message)
        if parsed is not None:
            rows.append(parsed)

    model = os.environ.get("SCORER_MODEL") or "haiku"
    per_score = 0.004 if "haiku" in model.lower() else 0.012
    summary = summarize(rows, cost_per_score=per_score)
    subject, html, text = render_daily(summary)

    boto3.client("ses", region_name=region).send_email(
        Source=os.environ["DIGEST_SENDER"],
        Destination={"ToAddresses": [os.environ["DIGEST_RECIPIENT"]]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Html": {"Data": html}, "Text": {"Data": text}},
        },
    )
    return {"mode": "heartbeat", "runs": summary.runs, "emailed": summary.emailed}
