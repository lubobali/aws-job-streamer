"""Wire the pipeline to real AWS (DynamoDB, SES) and OpenRouter, then send the digest.

This is the composition root: `pipeline.run_pipeline` stays pure and injectable, and everything
that touches the outside world (the boto3 store, the SES mailer, the HTTP scorer, the API key and
addresses) is assembled here. Phase 4's Lambda handler calls `run()` — the same entrypoint used to
trigger a manual run from the terminal — so the scheduled digest and a hand-run digest are the
exact same code path.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from aws_job_streamer.dedup import JobStore
from aws_job_streamer.digest import DigestMailer, DigestResult, send_digest
from aws_job_streamer.pipeline import PipelineResult, run_pipeline
from aws_job_streamer.scoring import DEFAULT_MODEL, Scorer
from aws_job_streamer.watchlist import Board, to_fetchers

_log = logging.getLogger("aws_job_streamer")


class RunHealth(Enum):
    """How a run went. Logged at a matching level so a CloudWatch metric filter can alarm on it."""

    OK = "ok"
    WARN = "warn"
    ERROR = "error"

    @property
    def log_level(self) -> int:
        return {
            RunHealth.OK: logging.INFO,
            RunHealth.WARN: logging.WARNING,
            RunHealth.ERROR: logging.ERROR,
        }[self]


@dataclass(frozen=True, slots=True)
class RunSummary:
    """The heartbeat: one structured line per run so silence never hides a failure.

    Every scheduled run emits this. A dead pipeline (all sources failed, nothing fetched) logs
    ERROR; a degraded one (some sources down, or zero jobs came back) logs WARN; a healthy run —
    including the normal 'nothing new, cost nothing' run — logs INFO. The 'no runs at all' case
    (a broken schedule) is caught by a CloudWatch alarm on the Lambda in Phase 4, not here.
    """

    health: RunHealth
    headline: str
    result: PipelineResult
    emailed: int
    message_id: str | None
    source_count: int

    def line(self) -> str:
        c = self.result.counts
        return (
            f"job-streamer run health={self.health.value} ({self.headline}) | "
            f"fetched={c.fetched} eligible={c.eligible} new={c.new} scored={c.scored} "
            f"skipped={c.skipped} digest={c.digest} deferred={c.deferred} "
            f"sources_failed={c.source_failures}/{self.source_count} "
            f"emailed={self.emailed} msg={self.message_id or '-'}"
        )


def assess_run(
    result: PipelineResult, *, source_count: int, digest_result: DigestResult | None
) -> RunSummary:
    """Classify a finished run's health. Pure — no logging, no I/O — so it is trivially testable."""
    c = result.counts
    emailed = digest_result.count if digest_result and digest_result.sent else 0
    message_id = digest_result.message_id if digest_result else None

    attempted = c.new - c.deferred  # jobs actually handed to the scorer this run
    if source_count and c.source_failures >= source_count:
        health, headline = RunHealth.ERROR, "all sources failed — fetched nothing"
    elif attempted > 0 and c.scored == 0:
        # The graceful per-job skip (one bad JD must not lose a run) hides a TOTAL scoring outage:
        # if every attempted score failed, the LLM is down or out of credit (a live 402 did this).
        # That is a broken run, not a quiet one — flag it loudly.
        health, headline = RunHealth.ERROR, f"scored 0 of {attempted} — scorer down (API/credit?)"
    elif c.source_failures > 0:
        health, headline = RunHealth.WARN, f"{c.source_failures}/{source_count} sources failed"
    elif c.fetched == 0:
        health, headline = RunHealth.WARN, "0 jobs fetched with no failures — boards empty/changed?"
    else:
        health, headline = RunHealth.OK, "healthy"

    return RunSummary(health, headline, result, emailed, message_id, source_count)


def load_dotenv(path: str | Path = ".env") -> None:
    """Load `KEY=value` lines from a .env file into the environment, without overriding what is
    already set (the Lambda gets these as real env vars; local runs get them from the file).

    Absent file is fine — the Lambda has no .env. Blank lines and `#` comments are skipped.
    """
    env = Path(path)
    if not env.exists():
        return
    for raw in env.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def load_profile(explicit: str | Path | None = None) -> dict[str, Any]:
    """Load the matching profile — the real `profile.json` if present, else the example.

    `profile.json` is gitignored (it is his real profile); `profile.example.json` is the committed
    sanitized stand-in, complete enough to run against.
    """
    if explicit is not None:
        return json.loads(Path(explicit).read_text())
    for candidate in ("profile.json", "profile.example.json"):
        if Path(candidate).exists():
            return json.loads(Path(candidate).read_text())
    raise FileNotFoundError("no profile.json or profile.example.json found")


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the run needs from the environment. Fails loudly if the API key is missing —
    a silent empty run is indistinguishable from 'nothing was posted today'."""

    openrouter_key: str
    table_name: str = "aws-job-streamer-jobs"
    region: str = "us-east-2"
    sender: str = "jobs@lubobali.com"
    recipient: str = "lubobali23@gmail.com"
    # Cold-start guard, ON by default so the scheduled Lambda can never blow the OpenRouter budget.
    # Measured cost is ~$0.012/job on Sonnet (4x an earlier guess), so the levers below matter: a
    # 14-day freshness cut shrinks a cold start from ~2300 jobs to a few hundred, and scorer_model
    # (Haiku ~1/3 the price) cuts the per-job cost — validated on the gold set before it is trusted.
    max_age_days: int = 14
    max_score_per_run: int = 200
    scorer_model: str = DEFAULT_MODEL

    @classmethod
    def from_env(cls) -> Settings:
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is not set (see .env.example)")
        return cls(
            openrouter_key=key,
            table_name=os.environ.get("JOBS_TABLE", "aws-job-streamer-jobs"),
            region=os.environ.get("AWS_REGION", "us-east-2"),
            sender=os.environ.get("DIGEST_SENDER", "jobs@lubobali.com"),
            recipient=os.environ.get("DIGEST_RECIPIENT", "lubobali23@gmail.com"),
            max_age_days=int(os.environ.get("COLD_START_MAX_AGE_DAYS", "14")),
            max_score_per_run=int(os.environ.get("MAX_SCORE_PER_RUN", "200")),
            # `or` not the 2nd arg: an empty SCORER_MODEL (Terraform sets "" to mean "unset") must
            # fall through to the code default, not become a blank model id.
            scorer_model=os.environ.get("SCORER_MODEL") or DEFAULT_MODEL,
        )


def run(
    boards: Sequence[Board] | None = None,
    *,
    send: bool = True,
    min_score: int | None = None,
) -> tuple[PipelineResult, DigestResult | None]:
    """Run one real cycle end-to-end and (optionally) email the digest.

    `boards` defaults to the full watchlist; pass a subset for a scoped/manual run. `send=False`
    runs everything but the email — a dry run that still scores and persists. Returns the pipeline
    result and the digest result (None when `send=False`).
    """
    load_dotenv()
    settings = Settings.from_env()
    profile = load_profile()

    sources = to_fetchers(boards) if boards is not None else to_fetchers()
    store = JobStore(table_name=settings.table_name, region=settings.region)
    scorer = Scorer(api_key=settings.openrouter_key, profile=profile, model=settings.scorer_model)

    result = run_pipeline(
        sources,
        store=store,
        scorer=scorer,
        profile=profile,
        min_score=min_score,
        max_age_days=settings.max_age_days,
        max_score_per_run=settings.max_score_per_run,
    )

    digest_result: DigestResult | None = None
    if send:
        mailer = DigestMailer(
            sender=settings.sender, recipient=settings.recipient, region=settings.region
        )
        digest_result = send_digest(result.digest, mailer=mailer, store=store)

    # Heartbeat: one structured line per run, at a level a CloudWatch alarm can watch (A3).
    summary = assess_run(result, source_count=len(sources), digest_result=digest_result)
    _log.log(summary.health.log_level, summary.line())

    return result, digest_result
