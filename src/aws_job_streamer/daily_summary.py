"""The daily heartbeat email — proof the app is alive even on a quiet day.

Every pipeline run already logs one structured heartbeat line (runner.assess_run). Once a day a
scheduled invocation reads the last 24h of those lines from CloudWatch, aggregates them, and emails
a plain summary: how many times it ran, how many jobs it scored, how many matched, and the cost —
plus a clear health verdict. So silence never means "is it broken?"; the daily note answers it.

This module is the PURE half — parsing a heartbeat line and rendering the summary — so it is fully
testable. The CloudWatch query and the SES send are glue in the Lambda handler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEALTH = re.compile(r"health=(\w+)")
_SCORED = re.compile(r"scored=(\d+)")
_EMAILED = re.compile(r"emailed=(\d+)")


def parse_heartbeat(message: str) -> dict[str, object] | None:
    """Pull the fields a summary needs out of one heartbeat line, or None if it is not one.

    >>> r = parse_heartbeat("job-streamer run health=warn (x) | scored=91 emailed=10 msg=-")
    >>> r["health"], r["scored"], r["emailed"]
    ('warn', 91, 10)
    >>> parse_heartbeat("some unrelated log line") is None
    True
    """
    health = _HEALTH.search(message)
    if "job-streamer run" not in message or health is None:
        return None
    scored = _SCORED.search(message)
    emailed = _EMAILED.search(message)
    return {
        "health": health.group(1),
        "scored": int(scored.group(1)) if scored else 0,
        "emailed": int(emailed.group(1)) if emailed else 0,
    }


@dataclass(frozen=True, slots=True)
class DailySummary:
    runs: int
    scored: int
    emailed: int
    errors: int
    warns: int
    cost: float

    @property
    def healthy(self) -> bool:
        return self.errors == 0


def summarize(rows: list[dict[str, object]], *, cost_per_score: float) -> DailySummary:
    """Aggregate a day's heartbeat rows."""
    scored = sum(int(r["scored"]) for r in rows)
    return DailySummary(
        runs=len(rows),
        scored=scored,
        emailed=sum(int(r["emailed"]) for r in rows),
        errors=sum(1 for r in rows if r["health"] == "error"),
        warns=sum(1 for r in rows if r["health"] == "warn"),
        cost=scored * cost_per_score,
    )


def render_daily(summary: DailySummary) -> tuple[str, str, str]:
    """Return (subject, html, text) for the daily heartbeat email."""
    s = summary
    verdict = (
        "Everything healthy."
        if s.healthy
        else f"⚠️ {s.errors} run(s) reported an ERROR today — worth a look."
    )
    warn_note = f" ({s.warns} run(s) had a minor warning.)" if s.warns and s.healthy else ""
    subject = f"aws-job-streamer daily check — {s.emailed} matches, {s.runs} runs"
    text = (
        f"aws-job-streamer daily check.\n\n"
        f"In the last 24 hours it ran {s.runs} time(s), scored {s.scored} new job(s) in a "
        f"location you can take, and emailed you {s.emailed} match(es). Estimated spend "
        f"~${s.cost:.2f}.\n\n"
        f"{verdict}{warn_note}\n\n"
        f"Job digests arrive separately whenever new matches appear — this note just "
        f"confirms the app is running."
    )
    html = (
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:560px;'
        f'margin:0 auto;color:#1a1a1a;padding:8px;font-size:14px">'
        f'<h2 style="margin:0 0 12px">aws-job-streamer — daily check</h2>'
        f"<p style=\"margin:0 0 8px\">In the last 24 hours it ran <b>{s.runs}</b> time(s), "
        f"scored <b>{s.scored}</b> new job(s) you could take, and emailed you "
        f"<b>{s.emailed}</b> match(es). Estimated spend <b>~${s.cost:.2f}</b>.</p>"
        f'<p style="margin:0 0 8px;color:{"#1a7f37" if s.healthy else "#b42318"}">{verdict}'
        f"{warn_note}</p>"
        f'<p style="margin:12px 0 0;color:#999;font-size:12px">Job digests arrive separately '
        f"whenever new matches appear — this note just confirms the app is running.</p></div>"
    )
    return subject, html, text
