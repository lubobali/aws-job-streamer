"""Phase 3 — the email digest: the moment the pipeline's output reaches Lubo's inbox.

Three concerns, tested separately:

  * **Rendering** (pure functions) — turn ranked jobs into HTML + plain text. Job fields come
    from job descriptions, which are UNTRUSTED, so everything is HTML-escaped and only http(s)
    URLs become links. A title with <script> must render as text; a javascript: URL must not be
    clickable.

  * **The mailer** — a thin wrapper over SES (the sender proven to land in the inbox,
    jobs@lubobali.com, DKIM-signed). The SES client is injected so the call shape is asserted
    without touching AWS.

  * **send-then-mark ordering** — email FIRST, then flip the jobs to "emailed". If the send
    fails, nothing is marked and the jobs are retried next run. Marking first would flag a job
    as sent that never arrived — silently lost. A duplicate email is recoverable; a lost job is
    not. (Same principle as score-then-store in the pipeline.)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from aws_job_streamer.digest import (
    DigestMailer,
    render_html,
    render_subject,
    render_text,
    send_digest,
)
from aws_job_streamer.fit import RankedJob, Status
from aws_job_streamer.location_rank import Tier
from aws_job_streamer.models import Job
from aws_job_streamer.scoring import ScoredJob

NOW = datetime(2026, 7, 16, tzinfo=UTC)


def a_ranked(  # noqa: PLR0913 — a builder; every field is an independent knob a test may set
    *,
    title: str = "Data Engineer",
    company: str = "Acme",
    url: str = "https://boards.greenhouse.io/acme/jobs/1",
    location: str | None = "Remote (US)",
    remote: bool = True,
    salary: str | None = None,
    salary_is_estimated: bool = False,
    score: int = 80,
    reason: str = "Strong Python and Spark match.",
    tier: Tier = Tier.REMOTE_US,
    source_id: str = "1",
) -> RankedJob:
    job = Job(
        source="greenhouse",
        source_id=source_id,
        company=company,
        title=title,
        url=url,
        location=location,
        remote=remote,
        salary=salary,
        salary_is_estimated=salary_is_estimated,
        fetched_at=NOW,
    )
    scored = ScoredJob(job=job, score=score, reason=reason)
    return RankedJob(scored=scored, location_tier=tier, status=Status.RANKED)


class TestRenderHtml:
    def test_shows_the_core_fields(self) -> None:
        html = render_html(
            [a_ranked(title="Senior Data Engineer", company="Ramp", score=92, reason="Great fit.")]
        )

        assert "Senior Data Engineer" in html
        assert "Ramp" in html
        assert "92" in html
        assert "Great fit." in html

    def test_the_title_links_to_the_apply_url(self) -> None:
        html = render_html([a_ranked(url="https://jobs.example.com/123")])

        assert 'href="https://jobs.example.com/123"' in html

    def test_orders_jobs_as_given(self) -> None:
        html = render_html(
            [a_ranked(title="First", source_id="1"), a_ranked(title="Second", source_id="2")]
        )

        assert html.index("First") < html.index("Second")

    def test_shows_a_real_salary(self) -> None:
        html = render_html([a_ranked(salary="$150k - $200k")])

        assert "$150k - $200k" in html

    def test_flags_an_estimated_salary(self) -> None:
        """An estimated salary must never look like a stated fact (Adzuna guesses two-thirds)."""
        html = render_html([a_ranked(salary="$119,026", salary_is_estimated=True)])

        assert "$119,026" in html
        assert "estimated" in html.lower()

    def test_omits_salary_when_absent(self) -> None:
        html = render_html([a_ranked(salary=None)])

        assert "salary" not in html.lower() or "$" not in html

    def test_marks_a_remote_job(self) -> None:
        assert "remote" in render_html([a_ranked(remote=True, tier=Tier.REMOTE_US)]).lower()

    def test_names_the_target_metro_tier(self) -> None:
        html = render_html([a_ranked(tier=Tier.TARGET_METRO_HYBRID, location="Tampa, FL")])

        assert "Tampa" in html

    def test_a_remote_job_hides_the_noisy_raw_location(self) -> None:
        """The clean tier label replaces multi-clause junk."""
        html = render_html(
            [
                a_ranked(
                    tier=Tier.REMOTE_US,
                    location="Remote only (hires in FL, TX, VA); Visa not available",
                )
            ]
        )

        assert "Remote (US)" in html
        assert "hires in FL" not in html
        assert "Visa not available" not in html

    def test_flags_a_remote_job_at_a_target_metro_company(self) -> None:
        """The one signal worth rescuing from the noise: a Tampa-based company."""
        html = render_html(
            [a_ranked(tier=Tier.REMOTE_US, location="Remote only; company in Tampa")]
        )

        assert "Tampa/Sarasota area" in html

    def test_an_other_us_job_keeps_its_city(self) -> None:
        """ "US" alone is too vague — an Austin job should say Austin."""
        html = render_html([a_ranked(tier=Tier.OTHER_US, location="Austin, TX", remote=False)])

        assert "Austin" in html

    def test_a_target_metro_tier_is_not_double_flagged(self) -> None:
        html = render_html([a_ranked(tier=Tier.TARGET_METRO_ONSITE, location="Sarasota, FL")])

        assert "Tampa/Sarasota area" not in html  # the tier label already says it


class TestRenderHtmlIsSafe:
    """Job fields come from untrusted job descriptions."""

    def test_escapes_html_in_the_title(self) -> None:
        html = render_html([a_ranked(title="Engineer <script>alert(1)</script>")])

        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_escapes_html_in_the_company_and_reason(self) -> None:
        html = render_html([a_ranked(company="<b>Acme</b>", reason="uses <img> tags")])

        assert "<b>Acme</b>" not in html
        assert "<img>" not in html

    def test_a_javascript_url_does_not_become_a_link(self) -> None:
        """A hostile url must never be clickable."""
        html = render_html([a_ranked(url="javascript:steal()")])

        assert "javascript:" not in html
        assert "href=" not in html or "steal" not in html

    def test_a_data_url_does_not_become_a_link(self) -> None:
        html = render_html([a_ranked(url="data:text/html,<script>bad</script>")])

        assert "data:text/html" not in html

    def test_a_normal_https_url_is_kept(self) -> None:
        html = render_html([a_ranked(url="https://safe.example.com/job")])

        assert 'href="https://safe.example.com/job"' in html


class TestRenderText:
    def test_includes_the_core_fields(self) -> None:
        text = render_text([a_ranked(title="Data Engineer", company="Acme", score=88)])

        assert "Data Engineer" in text
        assert "Acme" in text
        assert "88" in text

    def test_includes_the_apply_url_plainly(self) -> None:
        text = render_text([a_ranked(url="https://jobs.example.com/9")])

        assert "https://jobs.example.com/9" in text

    def test_has_no_html_tags(self) -> None:
        text = render_text([a_ranked(title="Data Engineer")])

        assert "<" not in text and ">" not in text


class TestRenderSubject:
    def test_states_the_count(self) -> None:
        subject = render_subject([a_ranked(), a_ranked(source_id="2"), a_ranked(source_id="3")])

        assert "3" in subject

    def test_singular_for_one_match(self) -> None:
        subject = render_subject([a_ranked()])

        assert "1" in subject
        assert "matches" not in subject  # "1 match", not "1 matches"


@dataclass
class FakeSes:
    """Captures the send_email call so the message shape is asserted without touching AWS."""

    sent: list[dict[str, Any]] = field(default_factory=list)
    fail: bool = False

    def send_email(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401 — mirrors boto3's send_email
        if self.fail:
            raise RuntimeError("SES rejected the message")
        self.sent.append(kwargs)
        return {"MessageId": "test-message-id"}


class TestDigestMailer:
    def test_sends_from_and_to_the_configured_addresses(self) -> None:
        ses = FakeSes()
        mailer = DigestMailer(sender="jobs@lubobali.com", recipient="me@gmail.com", client=ses)

        mailer.send([a_ranked()])

        call = ses.sent[0]
        assert "jobs@lubobali.com" in call["Source"]
        assert call["Destination"]["ToAddresses"] == ["me@gmail.com"]

    def test_sends_both_html_and_text_bodies(self) -> None:
        ses = FakeSes()
        DigestMailer(sender="a@b.com", recipient="c@d.com", client=ses).send([a_ranked()])

        body = ses.sent[0]["Message"]["Body"]
        assert "Html" in body
        assert "Text" in body

    def test_returns_the_message_id(self) -> None:
        ses = FakeSes()
        mailer = DigestMailer(sender="a@b.com", recipient="c@d.com", client=ses)

        assert mailer.send([a_ranked()]) == "test-message-id"


@dataclass
class FakeStore:
    emailed: list[str] = field(default_factory=list)

    def mark_emailed(self, job_ids: Sequence[str]) -> None:
        self.emailed.extend(job_ids)


class TestSendDigest:
    def test_sends_then_marks_emailed(self) -> None:
        ses = FakeSes()
        store = FakeStore()
        jobs = [a_ranked(source_id="1"), a_ranked(source_id="2")]
        mailer = DigestMailer(sender="a@b.com", recipient="c@d.com", client=ses)

        result = send_digest(jobs, mailer=mailer, store=store)

        assert result.sent is True
        assert result.count == 2
        assert len(store.emailed) == 2  # both jobs marked after the send

    def test_does_not_send_an_empty_digest(self) -> None:
        ses = FakeSes()
        store = FakeStore()
        mailer = DigestMailer(sender="a@b.com", recipient="c@d.com", client=ses)

        result = send_digest([], mailer=mailer, store=store)

        assert result.sent is False
        assert ses.sent == []
        assert store.emailed == []

    def test_a_failed_send_does_not_mark_anything(self) -> None:
        """The ordering guarantee: if the email never went out, the jobs stay new for retry."""
        ses = FakeSes(fail=True)
        store = FakeStore()
        mailer = DigestMailer(sender="a@b.com", recipient="c@d.com", client=ses)

        with pytest.raises(RuntimeError):
            send_digest([a_ranked()], mailer=mailer, store=store)

        assert store.emailed == []
