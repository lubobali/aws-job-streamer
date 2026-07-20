"""Phase 3 — render the ranked digest and send it to Lubo's inbox.

This is where the pipeline's output becomes the product: a clean, ranked email he reads on his
phone each morning and decides from. It never applies for him — it surfaces and explains
(human-in-the-loop, per GUARDRAILS).

Three parts, cleanly separated:
  * pure `render_*` functions — job fields into HTML + plain text;
  * `DigestMailer` — a thin wrapper over SES (proven to land in the inbox from jobs@lubobali.com,
    DKIM-signed), with the client injected so it is testable without AWS;
  * `send_digest` — the send-THEN-mark orchestration.

**Security: every job field is untrusted.** Titles, companies and reasons are lifted from public
job descriptions; one in the gold-set literally embeds instructions to the reader. So all text is
HTML-escaped, and only http(s) URLs become links — a `javascript:` or `data:` URL is shown as
plain text, never made clickable.

**Ordering: send first, then mark.** If the email fails, nothing is marked and the jobs are
retried next run. Marking first would flag a job as sent that never arrived — silently lost. A
duplicate email is recoverable; a lost job is not.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import boto3

from aws_job_streamer.fit import RankedJob
from aws_job_streamer.location_rank import Tier, mentions_target_metro

# Human-readable location label per tier, for the digest.
_TIER_LABEL = {
    Tier.REMOTE_US: "Remote (US)",
    Tier.TARGET_METRO_HYBRID: "Tampa Bay · hybrid",
    Tier.TARGET_METRO_ONSITE: "Sarasota / Tampa · on-site",
    Tier.HYBRID_RARE_TRAVEL: "Hybrid · rare travel",
    Tier.CURRENT_BASE: "Chicago",
    Tier.OTHER_US: "US",
}

# Score band -> colour, so the eye triages before reading. Ordered high to low.
_SCORE_COLOURS = ((85, "#1a7f37"), (70, "#0b57d0"), (50, "#b26a00"), (0, "#6b6b6b"))

_SAFE_SCHEMES = {"http", "https"}


def render_subject(ranked: Sequence[RankedJob]) -> str:
    """The subject line — lead with the count so the inbox preview is useful."""
    n = len(ranked)
    noun = "match" if n == 1 else "matches"
    return f"{n} new job {noun} — aws-job-streamer"


def render_text(ranked: Sequence[RankedJob], *, note: str | None = None) -> str:
    """Plain-text alternative — for clients that block HTML and for deliverability.

    No markup at all: a title with tags is shown verbatim as text, which is safe here. `note` is an
    optional footer line (the run's spend summary), so he can see what his money bought.
    """
    lines = [f"{len(ranked)} new job matches — ranked best-fit first.", ""]
    for i, r in enumerate(ranked, 1):
        job = r.scored.job
        lines.append(f"{i}. [{r.scored.score}] {job.title} — {job.company}")
        lines.append(f"   {_location_line(r)}")
        lines.append(f"   {r.scored.reason}")
        if job.salary:
            lines.append(f"   Salary: {_salary_text(job.salary, job.salary_is_estimated)}")
        lines.append(f"   {job.url}")
        lines.append("")
    lines.append("You review and decide — aws-job-streamer never applies for you.")
    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


def render_html(ranked: Sequence[RankedJob], *, note: str | None = None) -> str:
    """The HTML digest. Inline-styled and self-contained (email clients strip <style> and block
    external assets), mobile-first, and safe against untrusted job text. `note` is an optional
    footer line (the run's spend summary)."""
    rows = "".join(_html_row(r) for r in ranked)
    note_html = (
        f'<p style="margin:8px 0 0;color:#bbb;font-size:11px">{html.escape(note)}</p>'
        if note
        else ""
    )
    return f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;\
max-width:640px;margin:0 auto;color:#1a1a1a;padding:8px">
  <h2 style="margin:0 0 2px">{len(ranked)} new job matches</h2>
  <p style="margin:0 0 16px;color:#666;font-size:13px">Ranked best-fit first</p>
  <table style="width:100%;border-collapse:collapse">{rows}</table>
  <p style="margin:20px 0 0;color:#999;font-size:12px">
    You review and decide — aws-job-streamer surfaces and explains, it never applies for you.
  </p>{note_html}
</div>"""


def _html_row(r: RankedJob) -> str:
    job = r.scored.job
    title = html.escape(job.title)
    company = html.escape(job.company)
    reason = html.escape(r.scored.reason)
    badge = _score_badge(r.scored.score)
    location = html.escape(_location_line(r))
    heading = _linked_title(job.url, title)

    salary_line = ""
    if job.salary:
        salary_line = (
            f'<div style="color:#888;font-size:13px;margin-top:2px">'
            f"{html.escape(_salary_text(job.salary, job.salary_is_estimated))}</div>"
        )

    return f"""\
<tr><td style="padding:14px 0;border-bottom:1px solid #eee">
  <div style="margin-bottom:3px">{badge}&nbsp;{heading}</div>
  <div style="color:#444;font-size:13px">{company} · {location}</div>
  <div style="color:#555;font-size:14px;margin-top:4px">{reason}</div>
  {salary_line}
</td></tr>"""


def _linked_title(url: str, escaped_title: str) -> str:
    """Return the title as a link if the URL is safe, otherwise as plain bold text.

    A job's url is untrusted — only http(s) may become a clickable href. Anything else (a
    `javascript:` or `data:` scheme) is rendered as text so it can never execute.

    >>> _linked_title("https://x.io/j", "Data Engineer").startswith('<a href="https://x.io/j"')
    True
    >>> _linked_title("javascript:alert(1)", "Data Engineer")
    '<span style="font-size:16px;font-weight:600">Data Engineer</span>'
    """
    if _is_safe_url(url):
        return (
            f'<a href="{html.escape(url, quote=True)}" '
            f'style="font-size:16px;font-weight:600;color:#0b57d0;text-decoration:none">'
            f"{escaped_title}</a>"
        )
    return f'<span style="font-size:16px;font-weight:600">{escaped_title}</span>'


def _is_safe_url(url: str) -> bool:
    """Only http(s) URLs are safe to place in an href.

    >>> _is_safe_url("https://x.io/j")
    True
    >>> _is_safe_url("javascript:alert(1)")
    False
    >>> _is_safe_url("data:text/html,<script>")
    False
    """
    try:
        return urlparse(url).scheme.lower() in _SAFE_SCHEMES
    except ValueError:
        return False


def _score_badge(score: int) -> str:
    colour = next(c for threshold, c in _SCORE_COLOURS if score >= threshold)
    return (
        f'<span style="display:inline-block;background:{colour};color:#fff;font-size:12px;'
        f'font-weight:700;border-radius:4px;padding:1px 6px">{score}</span>'
    )


_TARGET_TIERS = (Tier.TARGET_METRO_HYBRID, Tier.TARGET_METRO_ONSITE)


def _location_line(r: RankedJob) -> str:
    """A short, clean location string.

    The tier label already encodes the arrangement cleanly ("Remote (US)", "Chicago") — so the
    raw location string, which is often multi-clause noise ("Remote only (hires in FL, TX...);
    company in Tampa; Visa not available"), is dropped. The one exception is OTHER_US, whose "US"
    label is too vague, so the actual city is kept (cleaned).

    A company sitting in his target metro is surfaced as a flag even for a remote role — a remote
    job at a Tampa company is worth a second look. On a target-metro tier the flag would be
    redundant, so it is only added elsewhere.

    >>> from aws_job_streamer.location_rank import Tier
    """
    tier = r.location_tier
    raw = r.scored.job.location or ""

    # The tier label is clean and complete for every tier except OTHER_US, whose "US" is too
    # vague — there, keep the actual city.
    base = _TIER_LABEL[tier]
    if tier is Tier.OTHER_US:
        base = _clean_location(raw) or base

    if tier not in _TARGET_TIERS and mentions_target_metro(raw):
        base = f"{base} · Tampa/Sarasota area"
    return base


def _clean_location(raw: str) -> str:
    """Reduce a noisy raw location to its useful head: drop parenthetical asides and trailing
    clauses.

    >>> _clean_location("Seattle, WA or Remote USA")
    'Seattle, WA or Remote USA'
    >>> _clean_location("Chicago, IL or Peoria, IL (office-based, NOT remote)")
    'Chicago, IL or Peoria, IL'
    >>> _clean_location("(not stated)")
    ''
    """
    without_parens = re.sub(r"\([^)]*\)", "", raw)
    head = without_parens.split(";")[0].split(" — ")[0]
    return head.strip().strip(",").strip()


def _salary_text(salary: str, estimated: bool) -> str:
    """Never present an estimated salary as a stated fact (Adzuna guesses ~2/3 of them).

    >>> _salary_text("$150k - $200k", False)
    'Salary: $150k - $200k'
    >>> _salary_text("$119,026", True)
    'Salary: $119,026 (estimated, not employer-stated)'
    """
    if estimated:
        return f"Salary: {salary} (estimated, not employer-stated)"
    return f"Salary: {salary}"


@dataclass(frozen=True, slots=True)
class DigestMailer:
    """Sends the digest via Amazon SES from the verified, DKIM-signed sender.

    The SES client is injected so the message shape is testable without touching AWS.
    """

    sender: str
    recipient: str
    region: str = "us-east-2"
    client: Any = None  # an injected boto3 SES client, untyped without stubs

    def send(self, ranked: Sequence[RankedJob], *, note: str | None = None) -> str:
        """Render and send the digest, returning the SES MessageId. `note` is the spend footer."""
        response = self._ses().send_email(
            Source=self.sender,
            Destination={"ToAddresses": [self.recipient]},
            Message={
                "Subject": {"Data": render_subject(ranked)},
                "Body": {
                    "Html": {"Data": render_html(ranked, note=note)},
                    "Text": {"Data": render_text(ranked, note=note)},
                },
            },
        )
        return response["MessageId"]

    def _ses(self) -> Any:  # noqa: ANN401 — boto3 client is untyped without stubs
        if self.client is not None:
            return self.client
        return boto3.client("ses", region_name=self.region)


@dataclass(frozen=True, slots=True)
class DigestResult:
    """What a digest run did."""

    sent: bool
    count: int
    message_id: str | None


class _MarkEmailedStore(Protocol):
    """The store surface send_digest needs — satisfied structurally by dedup.JobStore."""

    def mark_emailed(self, job_ids: Sequence[str]) -> None: ...


def send_digest(
    ranked: Sequence[RankedJob],
    *,
    mailer: DigestMailer,
    store: _MarkEmailedStore,
    note: str | None = None,
) -> DigestResult:
    """Send the digest, then mark its jobs emailed. Skips entirely when there is nothing to send.

    The order is load-bearing: the mark only runs if `send` returned, so a failed send leaves the
    jobs as "new" and next run retries them. An empty digest sends no email — a "nothing today"
    message is noise. `note` is an optional spend-summary footer.
    """
    if not ranked:
        return DigestResult(sent=False, count=0, message_id=None)

    message_id = mailer.send(ranked, note=note)
    store.mark_emailed([r.scored.job.job_id for r in ranked])
    return DigestResult(sent=True, count=len(ranked), message_id=message_id)
