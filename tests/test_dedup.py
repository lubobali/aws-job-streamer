"""The dedup gate — Goal #4, "never see the same job twice".

Tested against a real DynamoDB API (moto's in-process fake), so the actual boto3 calls run.
Mocking our own store instead would test nothing but the mock.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from aws_job_streamer.dedup import JobStore
from aws_job_streamer.fit import RankedJob, Status
from aws_job_streamer.location_rank import Tier
from aws_job_streamer.models import Job
from aws_job_streamer.scoring import ScoredJob

TABLE = "test-jobs"
REGION = "us-east-2"
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def store() -> Iterator[JobStore]:
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield JobStore(table_name=TABLE, region=REGION)


def a_job(source_id: str = "1", **overrides: object) -> Job:
    defaults = {
        "source": "greenhouse",
        "source_id": source_id,
        "company": "Acme",
        "title": "Data Engineer",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "posted_at": datetime(2026, 7, 15, tzinfo=UTC),
        "fetched_at": NOW,
    }
    return Job(**(defaults | overrides))  # type: ignore[arg-type]


class TestNewJobsOnly:
    def test_every_job_is_new_on_an_empty_table(self, store: JobStore) -> None:
        jobs = [a_job("1"), a_job("2"), a_job("3")]

        assert store.new_jobs_only(jobs) == jobs

    def test_a_job_seen_before_is_not_new(self, store: JobStore) -> None:
        """The whole point: the second poll must not re-surface the first poll's jobs."""
        first_poll = [a_job("1"), a_job("2")]
        store.mark_seen(store.new_jobs_only(first_poll))

        second_poll = store.new_jobs_only([a_job("1"), a_job("2")])

        assert second_poll == []

    def test_only_the_genuinely_new_job_survives_a_second_poll(self, store: JobStore) -> None:
        store.mark_seen(store.new_jobs_only([a_job("1"), a_job("2")]))

        fresh = store.new_jobs_only([a_job("1"), a_job("2"), a_job("3")])

        assert [j.source_id for j in fresh] == ["3"]

    def test_the_same_posting_refetched_with_a_new_url_is_still_not_new(
        self, store: JobStore
    ) -> None:
        """Decision Log #1 end-to-end: Adzuna's url changes every fetch; the id must not."""
        store.mark_seen(store.new_jobs_only([a_job("1", url="https://x.io/ad/1?se=AAA")]))

        again = store.new_jobs_only([a_job("1", url="https://x.io/ad/1?se=ZZZ")])

        assert again == []

    def test_the_same_id_at_two_sources_are_different_jobs(self, store: JobStore) -> None:
        store.mark_seen(store.new_jobs_only([a_job("1", source="greenhouse")]))

        other = store.new_jobs_only([a_job("1", source="lever")])

        assert len(other) == 1

    def test_duplicates_within_one_batch_are_collapsed(self, store: JobStore) -> None:
        """Two sources can return the same posting in a single run; only one may pass."""
        fresh = store.new_jobs_only([a_job("1"), a_job("1"), a_job("2")])

        assert [j.source_id for j in fresh] == ["1", "2"]

    def test_an_empty_batch_costs_no_call(self, store: JobStore) -> None:
        assert store.new_jobs_only([]) == []

    def test_order_is_preserved(self, store: JobStore) -> None:
        jobs = [a_job("3"), a_job("1"), a_job("2")]

        assert [j.source_id for j in store.new_jobs_only(jobs)] == ["3", "1", "2"]


class TestMarkSeen:
    def test_stores_the_fields_the_digest_needs(self, store: JobStore) -> None:
        store.mark_seen([a_job("1", title="Senior Data Engineer", company="Ramp")])

        item = store.get(a_job("1").job_id)

        assert item is not None
        assert item["title"] == "Senior Data Engineer"
        assert item["company"] == "Ramp"
        assert item["source"] == "greenhouse"

    def test_marking_nothing_is_not_an_error(self, store: JobStore) -> None:
        store.mark_seen([])

        assert store.get("nonexistent") is None

    def test_is_idempotent(self, store: JobStore) -> None:
        """A retried Lambda must not corrupt the record."""
        store.mark_seen([a_job("1")])
        store.mark_seen([a_job("1")])

        assert store.get(a_job("1").job_id) is not None

    def test_stores_status_new_so_the_digest_can_find_unsent_jobs(self, store: JobStore) -> None:
        store.mark_seen([a_job("1")])

        item = store.get(a_job("1").job_id)

        assert item is not None
        assert item["status"] == "new"

    def test_writes_more_than_one_batch_of_25(self, store: JobStore) -> None:
        """DynamoDB's BatchWriteItem caps at 25 items — a 60-job run must not silently lose 35."""
        jobs = [a_job(str(n)) for n in range(60)]

        store.mark_seen(jobs)

        assert store.new_jobs_only(jobs) == []

    def test_a_job_with_no_posted_at_is_still_stored(self, store: JobStore) -> None:
        """posted_at is None when a source will not say — that must not break the write."""
        store.mark_seen([a_job("1", posted_at=None)])

        assert store.get(a_job("1").job_id) is not None


class TestBatchLimits:
    def test_reads_more_than_one_batch_of_100(self, store: JobStore) -> None:
        """BatchGetItem caps at 100 keys. Over that, un-batched code silently reports
        everything as new and re-emails the lot."""
        jobs = [a_job(str(n)) for n in range(250)]
        store.mark_seen(jobs)

        assert store.new_jobs_only(jobs) == []

    def test_a_large_mixed_batch_returns_exactly_the_new_ones(self, store: JobStore) -> None:
        seen = [a_job(str(n)) for n in range(150)]
        store.mark_seen(seen)

        mixed = seen + [a_job(str(n)) for n in range(150, 160)]
        fresh = store.new_jobs_only(mixed)

        assert [j.source_id for j in fresh] == [str(n) for n in range(150, 160)]


def a_ranked(  # noqa: PLR0913 — a builder; every field is an independent knob a test may set
    source_id: str = "1",
    *,
    score: int = 80,
    reason: str = "good fit",
    status: Status = Status.RANKED,
    skip_reason: str | None = None,
    tier: Tier = Tier.REMOTE_US,
) -> RankedJob:
    scored = ScoredJob(job=a_job(source_id), score=score, reason=reason)
    return RankedJob(scored=scored, location_tier=tier, status=status, skip_reason=skip_reason)


def stored(store: JobStore, source_id: str = "1") -> dict[str, object]:
    """Fetch a record and assert it exists, so tests can subscript it without a None guard."""
    item = store.get(a_job(source_id).job_id)
    assert item is not None
    return item


class TestSaveNew:
    """Persisting the scored verdict — what the digest reads and what stops a re-score.

    A job is written only once it has a score, so "in the store" means "scored", and a job that
    failed scoring is simply not stored and gets retried next run rather than silently lost.
    """

    def test_stores_the_score_and_reason(self, store: JobStore) -> None:
        store.save_new([a_ranked("1", score=92, reason="excellent match")])

        item = stored(store)

        assert item["fit_score"] == 92
        assert item["fit_reason"] == "excellent match"

    def test_a_ranked_job_is_status_new(self, store: JobStore) -> None:
        """new = scored and ready to email; Phase 3 flips it to emailed."""
        store.save_new([a_ranked("1", status=Status.RANKED)])

        assert stored(store)["status"] == "new"

    def test_a_skipped_job_is_stored_as_skipped(self, store: JobStore) -> None:
        """Skipped jobs are stored so they are not re-scored — but marked, so the digest omits
        them and they stay auditable."""
        store.save_new([a_ranked("1", status=Status.SKIPPED, skip_reason="requires 10+ years")])

        item = stored(store)

        assert item["status"] == "skipped"
        assert item["skip_reason"] == "requires 10+ years"

    def test_stores_the_location_tier_for_the_digest_ordering(self, store: JobStore) -> None:
        store.save_new([a_ranked("1", tier=Tier.TARGET_METRO_HYBRID)])

        assert stored(store)["location_tier"] == Tier.TARGET_METRO_HYBRID.value

    def test_a_saved_job_is_no_longer_new(self, store: JobStore) -> None:
        """The point: saving a scored job closes the dedup gate for it."""
        store.save_new([a_ranked("1")])

        assert store.new_jobs_only([a_job("1")]) == []

    def test_saving_nothing_is_not_an_error(self, store: JobStore) -> None:
        store.save_new([])

        assert store.get("nonexistent") is None

    def test_writes_more_than_one_batch_of_25(self, store: JobStore) -> None:
        ranked = [a_ranked(str(n)) for n in range(60)]

        store.save_new(ranked)

        assert store.new_jobs_only([a_job(str(n)) for n in range(60)]) == []
