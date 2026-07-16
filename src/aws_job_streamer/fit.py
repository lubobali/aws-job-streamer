"""Turn the LLM's facts into the ranked digest — the arithmetic half of Phase 2.

The division of labour is deliberate (LUBO'S RULES): the LLM in `scoring.py` READS a posting and
reports facts (score, workplace, office days, years required); every NUMBER and every DECISION
happens here, in plain Python that can be read and tested.

Two rules this module encodes, both learned from the 18 jobs Lubo actually applied to:

  * **The years wall is a Python decision from a reported number.** He applies to 1-3, 4-8, 5+
    and 6+ year roles; only 8+ is a real disqualifier. So the skip is computed from the LLM's
    `years_required` number, not from the LLM's opinion. Azure-mandatory and wrong-discipline are
    *semantic* judgements with no number to compute, so those come from the LLM's flags.

  * **Skips MARK, they never delete.** A wrongly-flagged job must still be inspectable, not vanish
    — the same anti-silent-drop discipline as the location ranker and the fetchers.

Ranking: **fit score first, location as the tiebreaker, freshness last.** A strong match in
Chicago beats a weak one that happens to be remote — Lubo wants the best *jobs*, and location
decides only between jobs he'd be comparably happy to do. Because the LLM scores coarsely
(observed: 92, 88, 72, 45), comparably-good jobs share a score and location does the real sorting
within each cluster. (If location should instead always win — float every remote role above a
slightly-better onsite one — that is a one-line change to the sort key; flagged for review.)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from aws_job_streamer.location_rank import Tier, Workplace, location_tier
from aws_job_streamer.scoring import ScoredJob

_DEFAULT_YEARS_WALL = 8
_DEFAULT_DIGEST_LIMIT = 10

# The only flag that hard-skips. Deliberately NOT "wrong_discipline": discipline is exactly what
# the score already measures (a wrong-discipline job scores near zero and sinks off the top-N on
# its own), so hard-skipping on it is redundant AND wrongly hides a low-but-wanted role — e.g.
# 4C, a Node/TS sports-trading job Lubo genuinely targets for the domain, which scores ~42 and
# should appear low in the digest, not vanish. Azure-mandatory is different: a role can be a
# strong SKILL match (high score) yet still be off-limits because it forces a cloud he won't use,
# and the score does not capture that refusal. Same logic keeps the numeric years wall below.
_FLAG_REASONS = {
    "azure_mandatory": "Azure is a mandatory requirement",
}

# The one work_authorization value that skips: a requirement he cannot satisfy. "us_ok",
# "us_citizen_or_clearance" (his moat) and "unknown" are all kept — see _skip_reason.
_FOREIGN_AUTHORIZATION = "foreign_required"


class Status(Enum):
    """Whether a job reaches the digest."""

    RANKED = "ranked"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class RankedJob:
    """A scored job placed in the final order, carrying why it did or didn't make the cut."""

    scored: ScoredJob
    location_tier: Tier
    status: Status
    skip_reason: str | None = None


def rank_one(scored: ScoredJob, *, profile: dict[str, Any]) -> RankedJob:
    """Apply the skip rules and compute the location tier for a single scored job."""
    tier = _location_tier(scored)
    reason = _skip_reason(scored, profile=profile)
    status = Status.SKIPPED if reason else Status.RANKED
    return RankedJob(scored=scored, location_tier=tier, status=status, skip_reason=reason)


def rank(scored_jobs: Sequence[ScoredJob], *, profile: dict[str, Any]) -> list[RankedJob]:
    """Return every job in final order — ranked ones first, then skipped ones. Nothing is dropped.

    Order among ranked jobs: fit score (desc), then location tier (better first), then freshness.
    Skipped jobs always sort last, whatever their score: a skipped 90 sits below a ranked 50.
    """
    placed = [rank_one(s, profile=profile) for s in scored_jobs]
    return sorted(placed, key=_sort_key)


def for_digest(
    ranked: Sequence[RankedJob], *, limit: int = _DEFAULT_DIGEST_LIMIT
) -> list[RankedJob]:
    """Return the top `limit` RANKED jobs — what actually goes in the email.

    Skipped jobs are excluded here, not in `rank()`, so the full ranking (including skips) stays
    inspectable. This is an explicit filter, never a silent drop.
    """
    return [r for r in ranked if r.status is Status.RANKED][:limit]


def _location_tier(scored: ScoredJob) -> Tier:
    """Compute the location tier using the workplace facts the LLM extracted.

    This is what finally fills tiers 2 (target-metro hybrid) and 4 (hybrid, rare travel): no job
    API states "hybrid, 2 office days a month" — only the JD prose does, and the LLM read it.
    """
    return location_tier(
        scored.job,
        workplace=_to_workplace(scored.workplace),
        office_days_per_month=scored.office_days_per_month,
    )


def _to_workplace(value: str | None) -> Workplace:
    """Map the LLM's workplace string to the enum, tolerating anything unexpected.

    >>> _to_workplace("hybrid").name
    'HYBRID'
    >>> _to_workplace(None).name
    'UNKNOWN'
    >>> _to_workplace("something odd").name
    'UNKNOWN'
    """
    try:
        return Workplace(value)
    except ValueError:
        return Workplace.UNKNOWN


def _skip_reason(scored: ScoredJob, *, profile: dict[str, Any]) -> str | None:
    """Return why this job is skipped, or None to keep it.

    The years and work-authorization checks are decisions in Python off a reported fact. The Azure
    flag is a semantic judgement, so it honours the LLM's flag — but only the one known flag, so a
    novel flag can never silently drop a job.
    """
    wall = profile.get("skip_flags", {}).get("years_required_above", _DEFAULT_YEARS_WALL)
    if scored.years_required is not None and scored.years_required >= wall:
        return f"requires {scored.years_required}+ years (his wall is {wall})"

    # Skip ONLY on an authorization he cannot satisfy — not on a foreign location. A foreign OFFICE
    # open to a remote US worker is fine; "must be authorized to work in the UK" is not. US
    # citizenship / clearance is his moat and reads as us_citizen_or_clearance, which is kept.
    if scored.work_authorization == _FOREIGN_AUTHORIZATION:
        return "requires work authorization he does not have (non-US citizenship/visa)"

    for flag in scored.skip_flags:
        if flag in _FLAG_REASONS:
            return _FLAG_REASONS[flag]
    return None


# Sorted ascending, so every part is negated to read "best first". `_EPOCH` gives an undated job
# the oldest possible timestamp, so it sorts after anything with a real date.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _sort_key(r: RankedJob) -> tuple[int, int, int, float]:
    """The ranking key. Ascending sort, so smaller = better; every term is negated.

    1. status  — ranked (0) before skipped (1); a skip beats no score at all.
    2. score   — higher fit first. The primary signal.
    3. tier    — lower tier (better location) first, breaking score ties.
    4. posted  — fresher first; being early to a new posting is the point.
    """
    status_rank = 0 if r.status is Status.RANKED else 1
    posted = (r.scored.job.posted_at or _EPOCH).timestamp()
    return (status_rank, -r.scored.score, r.location_tier.value, -posted)
