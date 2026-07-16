"""The watchlist — probe-verified boards turned into pipeline fetchers."""

from __future__ import annotations

from aws_job_streamer.watchlist import WATCHLIST, Board, to_fetchers


class TestWatchlist:
    def test_every_board_uses_a_known_fetcher(self) -> None:
        assert all(b.source in {"greenhouse", "lever", "ashby"} for b in WATCHLIST)

    def test_slugs_are_unique_per_source(self) -> None:
        """A duplicated board would fetch, score and pay for the same jobs twice."""
        keys = [(b.source, b.slug) for b in WATCHLIST]
        assert len(keys) == len(set(keys))

    def test_no_board_is_missing_a_company_name(self) -> None:
        assert all(b.company and b.slug for b in WATCHLIST)

    def test_the_list_is_non_trivial(self) -> None:
        assert len(WATCHLIST) >= 20


class TestToFetchers:
    def test_produces_one_callable_per_board(self) -> None:
        boards = [Board("greenhouse", "acme", "Acme"), Board("ashby", "beta", "Beta")]

        fetchers = to_fetchers(boards)

        assert len(fetchers) == 2
        assert all(callable(f) for f in fetchers)

    def test_binds_each_board_independently(self) -> None:
        """Late-binding closures are a classic loop bug: every fetcher must keep its OWN slug."""
        boards = [Board("ashby", "one", "One"), Board("ashby", "two", "Two")]

        fetchers = to_fetchers(boards)

        # partial keeps the bound slug as the first positional arg.
        assert fetchers[0].args[0] == "one"  # type: ignore[attr-defined]
        assert fetchers[1].args[0] == "two"  # type: ignore[attr-defined]

    def test_greenhouse_is_not_passed_a_company_kwarg(self) -> None:
        """Greenhouse's fetch_jobs takes no `company` (its API already names the employer); passing
        it raises TypeError and silently killed every Greenhouse board in the first real run."""
        fetcher = Board("greenhouse", "nex", "Nex").to_fetcher()

        assert fetcher.args == ("nex",)  # type: ignore[attr-defined]
        assert "company" not in fetcher.keywords  # type: ignore[attr-defined]

    def test_lever_and_ashby_do_get_the_company(self) -> None:
        for source in ("lever", "ashby"):
            fetcher = Board(source, "acme", "Acme Inc").to_fetcher()

            assert fetcher.keywords["company"] == "Acme Inc"  # type: ignore[attr-defined]

    def test_defaults_to_the_full_watchlist(self) -> None:
        assert len(to_fetchers()) == len(WATCHLIST)
