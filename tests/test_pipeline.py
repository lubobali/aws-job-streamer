"""The pipeline glue — fetch -> prefilter -> dedup -> score -> rank -> store -> digest.

These tests pin the ORCHESTRATION, not the parts: the stages already have their own suites. What
matters here is the wiring, and three properties in particular:

  * **The dedup gate runs before scoring.** This is the whole cost model — a run that finds
    nothing new must not call the LLM at all. Most 15-minute runs find nothing new.
  * **One dead source does not lose the run.** A network fetch fails many ways; the others must
    still come through. The same discipline as the Workday fetcher and the batch scorer.
  * **The prefilter runs before the paid stages,** so foreign jobs are never scored or stored.

The store and scorer are injected, so the wiring is tested with fakes that record how they were
called — including *whether the scorer was called at all*.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from aws_job_streamer.fit import RankedJob
from aws_job_streamer.models import Job
from aws_job_streamer.pipeline import Fetcher, PipelineResult, run_pipeline
from aws_job_streamer.scoring import ScoredJob

PROFILE = {"skip_flags": {"years_required_above": 8}}
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def a_job(source_id: str, *, location: str = "Remote (US)", remote: bool = True) -> Job:
    return Job(
        source="greenhouse",
        source_id=source_id,
        company="Acme",
        title="Data Engineer",
        url=f"https://x.io/j/{source_id}",
        location=location,
        remote=remote,
        fetched_at=NOW,
    )


def source(*jobs: Job) -> object:
    """A source thunk returning a fixed set of jobs."""
    return lambda: list(jobs)


def failing_source() -> list[Job]:
    raise RuntimeError("board is down")


@dataclass
class FakeStore:
    """Records everything, so tests can assert what was persisted and what the gate returned."""

    seen: set[str] = field(default_factory=set)
    saved: list[RankedJob] = field(default_factory=list)

    def new_jobs_only(self, jobs: Sequence[Job]) -> list[Job]:
        out, batch = [], set()
        for job in jobs:
            if job.job_id not in self.seen and job.job_id not in batch:
                batch.add(job.job_id)
                out.append(job)
        return out

    def save_new(self, ranked: Sequence[RankedJob]) -> None:
        self.saved.extend(ranked)
        self.seen.update(r.scored.job.job_id for r in ranked)


@dataclass
class FakeScorer:
    """Scores each job; records the batch it was handed so the cost gate can be asserted."""

    scores: dict[str, int] = field(default_factory=dict)
    calls: list[list[Job]] = field(default_factory=list)
    years: dict[str, int] = field(default_factory=dict)

    def score_many(self, jobs: Sequence[Job]) -> list[ScoredJob]:
        self.calls.append(list(jobs))
        return [
            ScoredJob(
                job=j,
                score=self.scores.get(j.source_id, 70),
                reason="reason",
                workplace="remote",
                years_required=self.years.get(j.source_id),
            )
            for j in jobs
        ]


def run(
    sources: Sequence[Fetcher],
    store: FakeStore | None = None,
    scorer: FakeScorer | None = None,
    *,
    digest_limit: int = 10,
) -> PipelineResult:
    return run_pipeline(
        sources,
        store=store or FakeStore(),
        scorer=scorer or FakeScorer(),
        profile=PROFILE,
        digest_limit=digest_limit,
    )


class TestHappyPath:
    def test_scores_ranks_and_returns_a_digest(self) -> None:
        scorer = FakeScorer(scores={"a": 90, "b": 70})  # both above the 65 digest floor
        result = run([source(a_job("a"), a_job("b"))], scorer=scorer)

        assert [r.scored.job.source_id for r in result.digest] == ["a", "b"]
        assert result.digest[0].scored.score == 90

    def test_persists_every_scored_job(self) -> None:
        store = FakeStore()
        run([source(a_job("a"), a_job("b"))], store=store)

        assert {r.scored.job.source_id for r in store.saved} == {"a", "b"}

    def test_a_below_floor_job_is_scored_and_stored_but_not_emailed(self) -> None:
        """The floor keeps a weak match out of the inbox without losing it from the record."""
        store = FakeStore()
        scorer = FakeScorer(scores={"weak": 50})
        result = run([source(a_job("weak"))], store=store, scorer=scorer)

        assert result.digest == []  # not emailed
        assert {r.scored.job.source_id for r in store.saved} == {"weak"}  # but persisted
        assert result.counts.scored == 1

    def test_the_floor_can_be_overridden(self) -> None:
        scorer = FakeScorer(scores={"mid": 70})
        result = run_pipeline(
            [source(a_job("mid"))],
            store=FakeStore(),
            scorer=scorer,
            profile=PROFILE,
            min_score=80,
        )

        assert result.digest == []

    def test_combines_multiple_sources(self) -> None:
        result = run([source(a_job("a")), source(a_job("b")), source(a_job("c"))])

        assert len(result.digest) == 3


class TestCostGate:
    """The dedup gate before scoring is the entire cost story."""

    def test_already_seen_jobs_are_never_scored(self) -> None:
        store = FakeStore(seen={a_job("old").job_id})
        scorer = FakeScorer()

        run([source(a_job("old"))], store=store, scorer=scorer)

        scored_ids = [j.source_id for batch in scorer.calls for j in batch]
        assert scored_ids == []  # the scorer saw nothing

    def test_a_run_with_nothing_new_makes_no_scoring_call(self) -> None:
        store = FakeStore(seen={a_job("x").job_id, a_job("y").job_id})
        scorer = FakeScorer()

        result = run([source(a_job("x"), a_job("y"))], store=store, scorer=scorer)

        assert result.counts.new == 0
        assert result.counts.scored == 0
        assert all(batch == [] for batch in scorer.calls)

    def test_only_the_new_job_is_scored(self) -> None:
        store = FakeStore(seen={a_job("old").job_id})
        scorer = FakeScorer()

        run([source(a_job("old"), a_job("new"))], store=store, scorer=scorer)

        scored_ids = [j.source_id for batch in scorer.calls for j in batch]
        assert scored_ids == ["new"]


class TestPrefilterBeforePaidStages:
    def test_a_foreign_job_is_never_scored(self) -> None:
        scorer = FakeScorer()

        run([source(a_job("us"), a_job("uk", location="London, UK", remote=False))], scorer=scorer)

        scored_ids = [j.source_id for batch in scorer.calls for j in batch]
        assert scored_ids == ["us"]

    def test_a_foreign_job_is_never_stored(self) -> None:
        store = FakeStore()

        run([source(a_job("sydney", location="Sydney, Australia", remote=False))], store=store)

        assert store.saved == []


class TestResilientFetch:
    def test_one_dead_source_does_not_lose_the_others(self) -> None:
        result = run([source(a_job("a")), failing_source, source(a_job("b"))])

        assert {r.scored.job.source_id for r in result.digest} == {"a", "b"}

    def test_a_source_failure_is_counted(self) -> None:
        result = run([source(a_job("a")), failing_source])

        assert result.counts.source_failures == 1

    def test_every_source_failing_still_returns_cleanly(self) -> None:
        result = run([failing_source, failing_source])

        assert result.digest == []
        assert result.counts.source_failures == 2


class TestSkipsAndDigest:
    def test_a_skipped_job_is_stored_but_not_in_the_digest(self) -> None:
        store = FakeStore()
        scorer = FakeScorer(years={"senior": 10})

        result = run([source(a_job("ok"), a_job("senior"))], store=store, scorer=scorer)

        assert [r.scored.job.source_id for r in result.digest] == ["ok"]
        assert {r.scored.job.source_id for r in store.saved} == {"ok", "senior"}

    def test_digest_respects_the_limit(self) -> None:
        jobs = [a_job(str(n)) for n in range(15)]
        result = run([source(*jobs)], digest_limit=10)

        assert len(result.digest) == 10

    def test_counts_are_accurate(self) -> None:
        scorer = FakeScorer(years={"senior": 10})
        result = run(
            [source(a_job("a"), a_job("b"), a_job("senior"), a_job("uk", location="London, UK"))],
            scorer=scorer,
        )

        assert result.counts.fetched == 4
        assert result.counts.eligible == 3  # uk dropped
        assert result.counts.new == 3
        assert result.counts.scored == 3
        assert result.counts.skipped == 1  # senior
        assert result.counts.digest == 2


class TestEmpty:
    def test_no_sources_returns_an_empty_result(self) -> None:
        result = run([])

        assert result.digest == []
        assert result.counts.fetched == 0

    def test_no_sources_makes_no_scoring_call(self) -> None:
        scorer = FakeScorer()
        run([], scorer=scorer)

        assert all(batch == [] for batch in scorer.calls)
