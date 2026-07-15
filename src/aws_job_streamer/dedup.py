"""The dedup gate: the thing that makes Goal #4 — "never see the same job twice" — true.

It is also what keeps the bill near zero. The 15-minute poll asks this store what is new BEFORE
anything expensive happens, so a run that finds nothing new scores nothing and costs nothing.
Most runs find nothing new.

Identity is `job_id` = sha256(source + source_id) (PLAN.md Decision Log #1) — deliberately not
the url, because Adzuna hands back a different url for the same posting on every fetch.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import boto3

from aws_job_streamer.models import Job

_BATCH_GET_LIMIT = 100
"""DynamoDB BatchGetItem hard cap. Exceed it and the call fails — never a partial answer."""

_BATCH_WRITE_LIMIT = 25
"""DynamoDB BatchWriteItem hard cap."""


def _chunked(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    """Split `items` into chunks of at most `size`.

    >>> [list(c) for c in _chunked([1, 2, 3, 4, 5], 2)]
    [[1, 2], [3, 4], [5]]
    >>> list(_chunked([], 2))
    []
    """
    for start in range(0, len(items), size):
        yield items[start : start + size]


@dataclass(frozen=True, slots=True)
class JobStore:
    """DynamoDB-backed record of every job we have already seen."""

    table_name: str
    region: str = "us-east-2"

    @property
    def _table(self) -> Any:  # noqa: ANN401 — boto3 resources are untyped without boto3-stubs
        return boto3.resource("dynamodb", region_name=self.region).Table(self.table_name)

    def new_jobs_only(self, jobs: Sequence[Job]) -> list[Job]:
        """Return only the jobs never seen before, in their original order.

        Duplicates *within* the batch collapse too: two sources can return the same posting in
        one run, and it must reach the inbox once, not twice.
        """
        if not jobs:
            return []

        unique = _deduplicate(jobs)
        seen = self._seen_ids([job.job_id for job in unique])
        return [job for job in unique if job.job_id not in seen]

    def _seen_ids(self, job_ids: Sequence[str]) -> set[str]:
        """Return which of `job_ids` are already stored.

        Chunked to 100 because BatchGetItem rejects more. An un-chunked call does not truncate —
        it fails — and a caller that swallowed that error would report every job as new and
        re-email the entire board.
        """
        client = boto3.resource("dynamodb", region_name=self.region).meta.client
        found: set[str] = set()

        for chunk in _chunked(job_ids, _BATCH_GET_LIMIT):
            request = {self.table_name: {"Keys": [{"job_id": i} for i in chunk]}}
            while request:
                response = client.batch_get_item(RequestItems=request)
                found.update(
                    item["job_id"] for item in response["Responses"].get(self.table_name, [])
                )
                # DynamoDB may return UnprocessedKeys under load rather than failing. Ignoring
                # them would silently report a seen job as new.
                request = response.get("UnprocessedKeys") or {}
        return found

    def mark_seen(self, jobs: Iterable[Job]) -> None:
        """Record jobs as seen, so a later poll skips them.

        Idempotent: a retried Lambda re-writes the same item rather than corrupting it.
        """
        items = [_to_item(job) for job in jobs]
        if not items:
            return

        table = self._table
        # batch_writer handles the 25-item cap and retries unprocessed items itself.
        with table.batch_writer(overwrite_by_pkeys=["job_id"]) as batch:
            for item in items:
                batch.put_item(Item=item)

    def get(self, job_id: str) -> dict[str, Any] | None:
        """Return the stored record for `job_id`, or None."""
        return self._table.get_item(Key={"job_id": job_id}).get("Item")


def _deduplicate(jobs: Sequence[Job]) -> list[Job]:
    """Drop repeats within one batch, keeping the first and preserving order."""
    seen: set[str] = set()
    unique = []
    for job in jobs:
        if job.job_id not in seen:
            seen.add(job.job_id)
            unique.append(job)
    return unique


def _to_item(job: Job) -> dict[str, Any]:
    """Render a Job as a DynamoDB item.

    Only what a digest actually needs. `status` starts at "new" so the digest can find what has
    not been emailed yet; Phase 2 adds the scoring fields alongside it.
    """
    item: dict[str, Any] = {
        "job_id": job.job_id,
        "source": job.source,
        "source_id": job.source_id,
        "company": job.company,
        "title": job.title,
        "url": job.url,
        "remote": job.remote,
        "status": "new",
        "fetched_at": job.fetched_at.isoformat() if job.fetched_at else None,
    }
    if job.location:
        item["location"] = job.location
    if job.salary:
        item["salary"] = job.salary
        item["salary_is_estimated"] = job.salary_is_estimated
    if job.posted_at:
        item["posted_at"] = job.posted_at.isoformat()
    return {k: v for k, v in item.items() if v is not None}
