"""The curated company watchlist — WHO we fetch, the single biggest lever on digest quality.

Garbage-in beats any downstream filter: point the pipeline at companies whose work is Lubo's lane
(fintech/payments data teams, data-platform shops, AI/LLM-infra) and off-criteria jobs become rare
at the source. Every board here was **probe-verified** to return real jobs (PLAN.md Decision Log
#8: a 200 with an empty body is a 404 in disguise) — the slug and source are confirmed, not
guessed. Companies with no public ATS board (Symmetric, Workwhile, Logicbroker, dbt Labs, …) are
deliberately absent rather than listed dead.

The scorer and the 65-point digest floor decide relevance; this list decides the candidate pool.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial

from aws_job_streamer.fetchers import adzuna, ashby, greenhouse, lever, remotive
from aws_job_streamer.models import Job

Fetcher = Callable[[], list[Job]]

_FETCHERS: dict[str, Callable[..., list[Job]]] = {
    "greenhouse": greenhouse.fetch_jobs,
    "lever": lever.fetch_jobs,
    "ashby": ashby.fetch_jobs,
}


@dataclass(frozen=True, slots=True)
class Board:
    """One probe-verified ATS board: which fetcher reads it, its slug, and the display name."""

    source: str
    slug: str
    company: str

    def to_fetcher(self) -> Fetcher:
        """Bind this board to a no-argument callable the pipeline can fetch.

        Greenhouse's board API already carries the employer name, so its `fetch_jobs` takes no
        `company` argument; Lever and Ashby do not name the company, so it is passed to them.
        """
        fetch = _FETCHERS[self.source]
        if self.source == "greenhouse":
            return partial(fetch, self.slug)
        return partial(fetch, self.slug, company=self.company)


# Curated 2026-07-16, all probe-verified live. Grouped by why each is on Lubo's list.
WATCHLIST: tuple[Board, ...] = (
    # Companies from his own application history (gold set) that have a public board.
    Board("ashby", "modelyst", "Modelyst"),
    Board("lever", "foodsmart", "Foodsmart"),
    Board("greenhouse", "nex", "Nex"),
    # Fintech / payments — his HelloPayments/ISO domain, where data & platform teams live.
    Board("ashby", "ramp", "Ramp"),
    Board("greenhouse", "brex", "Brex"),
    Board("greenhouse", "mercury", "Mercury"),
    Board("greenhouse", "marqeta", "Marqeta"),
    Board("ashby", "moderntreasury", "Modern Treasury"),
    Board("ashby", "plaid", "Plaid"),
    Board("greenhouse", "checkr", "Checkr"),
    Board("lever", "finix", "Finix"),
    Board("greenhouse", "highnote", "Highnote"),
    Board("ashby", "unit", "Unit"),
    Board("greenhouse", "lithic", "Lithic"),
    Board("greenhouse", "gusto", "Gusto"),
    Board("greenhouse", "melio", "Melio"),
    Board("greenhouse", "mesh", "Mesh Payments"),
    Board("ashby", "column", "Column"),
    Board("greenhouse", "found", "Found"),
    Board("greenhouse", "tabapay", "TabaPay"),
    Board("ashby", "astra", "Astra"),
    # Data-platform / data-engineering shops — his exact stack IS the job.
    Board("greenhouse", "databricks", "Databricks"),
    Board("ashby", "snowflake", "Snowflake"),
    Board("greenhouse", "fivetran", "Fivetran"),
    Board("ashby", "airbyte", "Airbyte"),
    Board("ashby", "astronomer", "Astronomer"),
    Board("ashby", "confluent", "Confluent"),
    Board("greenhouse", "cribl", "Cribl"),
    Board("greenhouse", "hightouch", "Hightouch"),
    Board("greenhouse", "sigmacomputing", "Sigma Computing"),
    Board("greenhouse", "starburst", "Starburst"),
    Board("ashby", "montecarlodata", "Monte Carlo"),
    Board("ashby", "prefect", "Prefect"),
    Board("greenhouse", "datadog", "Datadog"),
    Board("greenhouse", "stripe", "Stripe"),
    # AI / LLM infrastructure — his LLM/RAG/agent lane.
    Board("greenhouse", "anthropic", "Anthropic"),
    Board("ashby", "cohere", "Cohere"),
    Board("ashby", "langchain", "LangChain"),
    Board("ashby", "baseten", "Baseten"),
    Board("ashby", "modal", "Modal"),
    Board("ashby", "pinecone", "Pinecone"),
    Board("ashby", "weaviate", "Weaviate"),
    Board("greenhouse", "scaleai", "Scale AI"),
)


def to_fetchers(boards: Sequence[Board] = WATCHLIST) -> list[Fetcher]:
    """Turn a watchlist into the no-argument fetchers `run_pipeline` consumes.

    Each fetcher isolates its own failures inside the pipeline, so one dead board never sinks a run.
    """
    return [board.to_fetcher() for board in boards]


# Remotive is keyword search, not a company board, so it is driven by queries rather than slugs.
# Every result is remote → it feeds the workable digest directly ("catch more, miss nothing").
# Kept focused: broad enough to cover his lane, few enough that overlap/cost stay small.
REMOTIVE_SEARCHES: tuple[str, ...] = (
    "data engineer",
    "AI engineer",
    "machine learning engineer",
    "data platform",
    "backend engineer",
)


def remotive_fetchers(searches: Sequence[str] = REMOTIVE_SEARCHES) -> list[Fetcher]:
    """Build a Remotive fetcher per search term."""
    return [partial(remotive.fetch_jobs, term) for term in searches]


# Adzuna geocodes every posting to a physical city, so its unique value is LOCAL search in the
# metros he can work — which no ATS board can do (they are per-company, not per-place). We point it
# ONLY at his workable metros (Tampa/Sarasota target + Chicago bridge); remote is Remotive's job.
# Probe-confirmed: a Sarasota search returns Tampa TARGET_METRO jobs; a Chicago search Chicagoland.
_ADZUNA_LOCATIONS: tuple[tuple[str, int], ...] = (
    ("Sarasota", 60),  # covers Venice, Tampa, Bradenton, St. Petersburg — his target metro
    ("Chicago", 40),  # covers Chicagoland — his bridge
)
_ADZUNA_PHRASES: tuple[str, ...] = (
    "data engineer",
    "AI engineer",
    "machine learning engineer",
    "data platform",
)


@dataclass(frozen=True, slots=True)
class AdzunaQuery:
    """One Adzuna local search: a phrase within `distance` miles of `where`."""

    phrase: str
    where: str
    distance: int

    def to_fetcher(self) -> Fetcher:
        return partial(
            adzuna.fetch_jobs,
            self.phrase,
            where=self.where,
            distance=self.distance,
            max_days_old=30,
            max_results=25,
        )


ADZUNA_QUERIES: tuple[AdzunaQuery, ...] = tuple(
    AdzunaQuery(phrase, where, dist)
    for where, dist in _ADZUNA_LOCATIONS
    for phrase in _ADZUNA_PHRASES
)


def adzuna_fetchers(queries: Sequence[AdzunaQuery] = ADZUNA_QUERIES) -> list[Fetcher]:
    """Build an Adzuna fetcher per local query. Needs ADZUNA_APP_ID/APP_KEY in the environment."""
    return [q.to_fetcher() for q in queries]


def all_sources() -> list[Fetcher]:
    """Every source a full run pulls: ATS watchlist + Remotive (remote) + Adzuna (local metros)."""
    return to_fetchers() + remotive_fetchers() + adzuna_fetchers()
