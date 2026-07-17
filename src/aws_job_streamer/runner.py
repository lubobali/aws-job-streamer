"""Wire the pipeline to real AWS (DynamoDB, SES) and OpenRouter, then send the digest.

This is the composition root: `pipeline.run_pipeline` stays pure and injectable, and everything
that touches the outside world (the boto3 store, the SES mailer, the HTTP scorer, the API key and
addresses) is assembled here. Phase 4's Lambda handler calls `run()` — the same entrypoint used to
trigger a manual run from the terminal — so the scheduled digest and a hand-run digest are the
exact same code path.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aws_job_streamer.dedup import JobStore
from aws_job_streamer.digest import DigestMailer, DigestResult, send_digest
from aws_job_streamer.pipeline import PipelineResult, run_pipeline
from aws_job_streamer.scoring import Scorer
from aws_job_streamer.watchlist import Board, to_fetchers


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
    # Cold-start guard, ON by default so the scheduled Lambda can never blow the $10 OpenRouter cap.
    # 200 jobs/run at ~$0.003 = ~$0.60/run worst case; a big cold start drains over successive runs.
    max_age_days: int = 30
    max_score_per_run: int = 200

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
            max_age_days=int(os.environ.get("COLD_START_MAX_AGE_DAYS", "30")),
            max_score_per_run=int(os.environ.get("MAX_SCORE_PER_RUN", "200")),
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
    scorer = Scorer(api_key=settings.openrouter_key, profile=profile)

    result = run_pipeline(
        sources,
        store=store,
        scorer=scorer,
        profile=profile,
        min_score=min_score,
        max_age_days=settings.max_age_days,
        max_score_per_run=settings.max_score_per_run,
    )

    if not send:
        return result, None

    mailer = DigestMailer(
        sender=settings.sender, recipient=settings.recipient, region=settings.region
    )
    digest_result = send_digest(result.digest, mailer=mailer, store=store)
    return result, digest_result
