"""The composition root — the pure config/loading helpers. The boto3/SES/HTTP wiring in run() is
covered by the components' own tests and the live end-to-end run."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aws_job_streamer.runner import Settings, load_dotenv, load_profile


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
        for var in ("COLD_START_MAX_AGE_DAYS", "MAX_SCORE_PER_RUN"):
            monkeypatch.delenv(var, raising=False)

        settings = Settings.from_env()

        assert settings.max_age_days == 30
        assert settings.max_score_per_run == 200

    def test_env_overrides_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "k")
        monkeypatch.setenv("JOBS_TABLE", "other-table")

        assert Settings.from_env().table_name == "other-table"
