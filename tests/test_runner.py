"""The composition root — the pure config/loading helpers. The boto3/SES/HTTP wiring in run() is
covered by the components' own tests and the live end-to-end run."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from aws_job_streamer.digest import DigestResult
from aws_job_streamer.pipeline import PipelineCounts, PipelineResult
from aws_job_streamer.runner import (
    RunHealth,
    Settings,
    assess_run,
    load_dotenv,
    load_profile,
)
from aws_job_streamer.scoring import DEFAULT_MODEL


def _result(**overrides: int) -> PipelineResult:
    base = {
        "fetched": 100,
        "eligible": 80,
        "new": 10,
        "scored": 10,
        "skipped": 2,
        "digest": 5,
        "source_failures": 0,
    }
    return PipelineResult(digest=[], ranked=[], counts=PipelineCounts(**(base | overrides)))


class TestAssessRun:
    def test_a_clean_run_is_ok(self) -> None:
        summary = assess_run(_result(), source_count=6, digest_result=None)

        assert summary.health is RunHealth.OK

    def test_all_sources_failing_is_an_error(self) -> None:
        summary = assess_run(
            _result(fetched=0, source_failures=6), source_count=6, digest_result=None
        )

        assert summary.health is RunHealth.ERROR

    def test_a_partial_source_failure_is_a_warning(self) -> None:
        summary = assess_run(_result(source_failures=2), source_count=6, digest_result=None)

        assert summary.health is RunHealth.WARN

    def test_all_scoring_failing_is_an_error(self) -> None:
        """A live 402 made every score fail; the per-job skip left scored=0 but health OK. It must
        read as ERROR — a total scoring outage is a broken run, not a quiet one."""
        summary = assess_run(
            _result(new=200, deferred=0, scored=0), source_count=6, digest_result=None
        )

        assert summary.health is RunHealth.ERROR

    def test_a_normal_nothing_new_run_is_still_ok(self) -> None:
        """0 scored because 0 attempted (all deduped) is the normal cheap run, NOT an outage."""
        summary = assess_run(
            _result(new=0, deferred=0, scored=0), source_count=6, digest_result=None
        )

        assert summary.health is RunHealth.OK

    def test_zero_fetched_without_failures_is_a_warning(self) -> None:
        """No errors but nothing came back — a board shape change hides exactly like this."""
        summary = assess_run(_result(fetched=0), source_count=6, digest_result=None)

        assert summary.health is RunHealth.WARN

    def test_the_line_reports_counts_and_the_email(self) -> None:
        digest = DigestResult(sent=True, count=5, message_id="ses-123")
        summary = assess_run(_result(), source_count=6, digest_result=digest)

        line = summary.line()
        assert "fetched=100" in line
        assert "emailed=5" in line
        assert "ses-123" in line

    def test_health_maps_to_log_levels(self) -> None:
        assert RunHealth.OK.log_level == logging.INFO
        assert RunHealth.WARN.log_level == logging.WARNING
        assert RunHealth.ERROR.log_level == logging.ERROR


class TestLoadDotenv:
    def test_loads_key_values(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env = tmp_path / ".env"
        env.write_text("FOO=bar\n# a comment\n\nBAZ=qux\n")
        monkeypatch.delenv("FOO", raising=False)
        monkeypatch.delenv("BAZ", raising=False)

        load_dotenv(env)

        assert os.environ["FOO"] == "bar"
        assert os.environ["BAZ"] == "qux"

    def test_does_not_override_existing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Lambda's real env vars must win over any committed .env."""
        env = tmp_path / ".env"
        env.write_text("FOO=from_file\n")
        monkeypatch.setenv("FOO", "from_env")

        load_dotenv(env)

        assert os.environ["FOO"] == "from_env"

    def test_a_missing_file_is_fine(self, tmp_path: Path) -> None:
        load_dotenv(tmp_path / "nope.env")  # must not raise


class TestLoadProfile:
    def test_loads_an_explicit_path(self, tmp_path: Path) -> None:
        p = tmp_path / "p.json"
        p.write_text(json.dumps({"headline": "x"}))

        assert load_profile(p)["headline"] == "x"


class TestSettings:
    def test_requires_the_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            Settings.from_env()

    def test_defaults_are_lubos_real_infra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        for var in ("JOBS_TABLE", "AWS_REGION", "DIGEST_SENDER", "DIGEST_RECIPIENT"):
            monkeypatch.delenv(var, raising=False)

        settings = Settings.from_env()

        assert settings.table_name == "aws-job-streamer-jobs"
        assert settings.region == "us-east-2"
        assert settings.sender == "jobs@lubobali.com"

    def test_cold_start_guard_is_on_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The scheduled run must be budget-safe without any extra config."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        for var in ("COLD_START_MAX_AGE_DAYS", "MAX_SCORE_PER_RUN", "SCORER_MODEL"):
            monkeypatch.delenv(var, raising=False)

        settings = Settings.from_env()

        assert settings.max_age_days == 14  # freshness cut, to bound the cold-start volume
        assert settings.max_score_per_run == 200

    def test_scorer_model_is_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The model is a cost lever (Haiku ~1/3 of Sonnet); overridable without a code change."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        monkeypatch.setenv("SCORER_MODEL", "anthropic/claude-haiku-4.5")

        assert Settings.from_env().scorer_model == "anthropic/claude-haiku-4.5"

    def test_an_empty_scorer_model_falls_through_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Terraform sets SCORER_MODEL='' to mean 'unset'; it must not become a blank model id."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        monkeypatch.setenv("SCORER_MODEL", "")

        assert Settings.from_env().scorer_model == DEFAULT_MODEL

    def test_env_overrides_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        monkeypatch.setenv("JOBS_TABLE", "other-table")

        assert Settings.from_env().table_name == "other-table"
