"""The daily heartbeat — parsing heartbeat lines and rendering the summary (the pure half)."""

from __future__ import annotations

from aws_job_streamer.daily_summary import parse_heartbeat, render_daily, summarize


class TestParseHeartbeat:
    def test_pulls_health_scored_emailed(self) -> None:
        line = "job-streamer run health=ok (healthy) | scored=91 digest=10 emailed=10 msg=-"

        row = parse_heartbeat(line)

        assert row == {"health": "ok", "scored": 91, "emailed": 10}

    def test_a_non_heartbeat_line_is_ignored(self) -> None:
        assert parse_heartbeat("HTTP Request: POST https://openrouter.ai ... 200 OK") is None

    def test_missing_counts_default_to_zero(self) -> None:
        row = parse_heartbeat("job-streamer run health=warn (1/128 sources failed)")

        assert row == {"health": "warn", "scored": 0, "emailed": 0}


class TestSummarize:
    def test_aggregates_a_day_of_runs(self) -> None:
        rows = [
            {"health": "ok", "scored": 20, "emailed": 3},
            {"health": "ok", "scored": 0, "emailed": 0},
            {"health": "warn", "scored": 5, "emailed": 1},
        ]

        s = summarize(rows, cost_per_score=0.004)

        assert s.runs == 3
        assert s.scored == 25
        assert s.emailed == 4
        assert s.warns == 1
        assert s.errors == 0
        assert s.healthy is True
        assert round(s.cost, 3) == 0.100

    def test_an_error_run_makes_the_day_unhealthy(self) -> None:
        s = summarize([{"health": "error", "scored": 0, "emailed": 0}], cost_per_score=0.004)

        assert s.errors == 1
        assert s.healthy is False


class TestRenderDaily:
    def test_healthy_day_reads_clean(self) -> None:
        s = summarize(
            [{"health": "ok", "scored": 40, "emailed": 5}], cost_per_score=0.004
        )
        subject, html, text = render_daily(s)

        assert "5 matches" in subject
        assert "Everything healthy" in text
        assert "$0.16" in text  # 40 * 0.004
        assert "40" in html

    def test_error_day_flags_it(self) -> None:
        s = summarize([{"health": "error", "scored": 0, "emailed": 0}], cost_per_score=0.004)
        _, _, text = render_daily(s)

        assert "ERROR" in text
