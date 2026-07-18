"""The pipeline: fetch -> prefilter -> dedup -> score -> rank -> store -> digest.

This is the one place the whole thing runs as a unit. Phase 4's Lambda will call `run_pipeline`
on a schedule; today it runs from the terminal (or the CLI). It orchestrates the stages that each
already have their own tests, so this module owns only the wiring and its three load-bearing
properties:

  1. **Order for cost.** Prefilter (free) then the dedup gate (cheap) run BEFORE scoring (the paid
     LLM stage), so a run that finds nothing new spends nothing. After dedup most 15-minute runs
     are exactly that — nothing new, zero cost.

  2. **Per-source isolation.** Each source is a network call that can fail many ways; a failure
     drops that source's jobs and is counted, but never loses the rest of the run.

  3. **Score once.** A job is stored only after it is scored, and stored jobs close the dedup gate,
     so the LLM is never paid twice for the same posting.

The store and scorer are injected (duck-typed), which keeps this module free of boto3/httpx and
makes the wiring testable with fakes — including asserting the scorer is *not* called on a
nothing-new run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from aws_job_streamer.fit import RankedJob, Status, for_digest, rank
from aws_job_streamer.models import Job
from aws_job_streamer.prefilter import keep_worth_scoring
from aws_job_streamer.scoring import ScoredJob

_DEFAULT_DIGEST_LIMIT = 10
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _utcnow() -> datetime:
    """Local clock, so this module stays free of the fetchers' httpx import (module docstring)."""
    return datetime.now(UTC)

Fetcher = Callable[[], list[Job]]
"""A source: called with no arguments, returns the jobs on one board or query."""


class Store(Protocol):
    """The persistence a run needs — satisfied by dedup.JobStore."""

    def new_jobs_only(self, jobs: Sequence[Job]) -> list[Job]: ...
    def save_new(self, ranked: Sequence[RankedJob]) -> None: ...


class Scorer(Protocol):
    """The scoring a run needs — satisfied by scoring.Scorer."""

    def score_many(self, jobs: Sequence[Job]) -> list[ScoredJob]: ...


@dataclass(frozen=True, slots=True)
class PipelineCounts:
    """What happened, for logging and for confirming a run really did cost nothing.

    `fetched >= eligible >= new >= scored`, and `scored == digest + skipped` (minus any job the
    scorer dropped). A run with `new == 0` did no LLM work.
    """

    fetched: int
    eligible: int
    new: int
    scored: int
    skipped: int
    digest: int
    source_failures: int
    deferred: int = 0
    """New jobs the cold-start guard held back this run (too old, or over the per-run scoring
    budget). They are NOT stored, so the next run reconsiders them — a big cold start drains
    across runs instead of blowing the LLM budget at once. 0 when no guard is set."""


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """A run's output: the email-ready digest, the full ranking, and the counts."""

    digest: list[RankedJob]
    ranked: list[RankedJob]
    counts: PipelineCounts


def run_pipeline(  # noqa: PLR0913 — each arg is an injected seam or a real tuning knob
    sources: Sequence[Fetcher],
    *,
    store: Store,
    scorer: Scorer,
    profile: dict[str, Any],
    digest_limit: int = _DEFAULT_DIGEST_LIMIT,
    min_score: int | None = None,
    per_company: int | None = None,
    max_age_days: int | None = None,
    max_score_per_run: int | None = None,
    now: Callable[[], datetime] = _utcnow,
) -> PipelineResult:
    """Run one full cycle and return the ranked digest.

    `min_score` (digest floor) and `per_company` (max jobs per employer) default to None, which
    lets `for_digest` apply its own defaults (65 and 2) so `fit` stays the one source of truth.
    Matches trimmed by either stay in `ranked`, just not in `digest`.

    `max_age_days` and `max_score_per_run` are the cold-start guard: they bound how many NEW jobs
    a single run scores, so the first run over a fresh watchlist cannot blow the LLM budget. The
    overflow is deferred (not scored, not stored) and the next run retries it. Both default to
    None (no bound) — steady-state runs find little new, so the dedup gate already keeps runs cheap.

    Side effect: scored jobs (ranked and skipped) are written to the store, which closes the
    dedup gate for them. Nothing is emailed here — that is Phase 3's job on the returned digest.
    """
    fetched, source_failures = _fetch_all(sources)
    eligible = keep_worth_scoring(fetched)
    new = store.new_jobs_only(eligible)

    to_score = _select_within_budget(
        new, max_age_days=max_age_days, max_score_per_run=max_score_per_run, now=now
    )
    deferred = len(new) - len(to_score)

    # The cost gate: score_many([]) makes no call, so a nothing-new run spends nothing.
    scored = scorer.score_many(to_score)
    ranked = rank(scored, profile=profile)
    store.save_new(ranked)

    overrides: dict[str, Any] = {}
    if min_score is not None:
        overrides["min_score"] = min_score
    if per_company is not None:
        overrides["per_company"] = per_company
    digest = for_digest(ranked, limit=digest_limit, **overrides)
    counts = PipelineCounts(
        fetched=len(fetched),
        eligible=len(eligible),
        new=len(new),
        scored=len(scored),
        skipped=sum(1 for r in ranked if r.status is Status.SKIPPED),
        digest=len(digest),
        source_failures=source_failures,
        deferred=deferred,
    )
    return PipelineResult(digest=digest, ranked=ranked, counts=counts)


def _select_within_budget(
    jobs: Sequence[Job],
    *,
    max_age_days: int | None,
    max_score_per_run: int | None,
    now: Callable[[], datetime],
) -> list[Job]:
    """Choose which NEW jobs to score under the cold-start guard, freshest first.

    A stale posting (older than `max_age_days`) is skipped — being 'early' does not apply to an
    evergreen role, and old postings are what flood a cold start. Unknown age is KEPT (the same
    'never drop on missing data' asymmetry as the prefilter). Then the freshest `max_score_per_run`
    survive; the rest are left for the next run. Either bound unset means 'no bound'.
    """
    kept = list(jobs)
    if max_age_days is not None:
        cutoff = now() - timedelta(days=max_age_days)
        kept = [j for j in kept if j.posted_at is None or j.posted_at >= cutoff]
    kept.sort(key=lambda j: j.posted_at or _EPOCH, reverse=True)
    if max_score_per_run is not None:
        kept = kept[:max_score_per_run]
    return kept


def _fetch_all(sources: Sequence[Fetcher]) -> tuple[list[Job], int]:
    """Fetch every source, isolating failures.

    A source is a live network call — a dead board, a rate limit, a shape change. Any one may
    throw, and losing the whole run because one board is down would be the opposite of resilient
    (the same lesson as the Workday and Adzuna fetchers). So each is caught and counted, and the
    rest proceed. The broad except is deliberate: we cannot enumerate every way a third-party
    call fails, and dropping one source is always safe.
    """
    jobs: list[Job] = []
    failures = 0
    for fetch in sources:
        try:
            jobs.extend(fetch())
        except Exception:  # broad on purpose: per-source isolation is a hard requirement here
            failures += 1
    return jobs, failures
