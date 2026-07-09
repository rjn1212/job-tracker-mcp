"""MCP server entry point.

CRITICAL: Claude Desktop speaks MCP to this process over stdio. Any byte
written to stdout that is not a JSON-RPC frame corrupts the transport and the
server appears crashed. Never call print(). Logging goes to stderr.
"""

from __future__ import annotations

import functools
import logging
import sys

from fastmcp import FastMCP

from job_tracker_mcp.database import Database

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("job_tracker_mcp")

mcp = FastMCP("job-tracker")
db = Database()


def _tool_guard(fn):
    """Convert any unexpected exception into {'error': ...}.

    Expected conditions (missing id, bad status) are already returned as error
    dicts by Database. This catches the rest, so a bug degrades one tool call
    instead of killing the server. The traceback goes to stderr, which lands in
    ~/Library/Logs/Claude/mcp-server-job-tracker.log
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 -- deliberate catch-all
            log.exception("tool %s failed", fn.__name__)
            return {"error": f"{type(exc).__name__}: {exc}"}
    return wrapper


@mcp.tool
@_tool_guard
def add_job(
    company: str,
    title: str,
    location: str,
    url: str | None = None,
    source: str | None = None,
    fit_score: int | None = None,
    notes: str | None = None,
    email_subject: str | None = None,
) -> dict:
    """Add a job posting to the tracker.

    Idempotent: adding the same (company, title, location) twice records a
    re-sighting rather than a second row, and never overwrites an existing
    fit_score, status, or notes.

    Pass company, title and location EXACTLY as the posting writes them --
    "Acme, Inc.", "Staff Engineer (Remote) [R-12345]", "Remote, US". Do NOT
    rewrite them into some canonical form. The server strips legal suffixes,
    board decorations and location spellings itself, deterministically, and
    keeps the raw strings for display. Hand-normalizing only destroys
    information and cannot make the dedup any better.

    If the company or title matches a rejection pattern, the job is stored
    with status="rejected" and the response has auto_rejected=true.

    The response may include `possible_duplicates`: similar-but-not-identical
    titles at the same employer. The job is still created. Inspect them and
    call merge_jobs if one is genuinely the same requisition.

    For a job alert email containing several postings, prefer bulk_add_jobs.
    """
    return db.add_job(company, title, location, url, source, fit_score, notes, email_subject)


@mcp.tool
@_tool_guard
def bulk_add_jobs(jobs: list[dict]) -> dict:
    """Add many jobs in one call. Use this when triaging a job alert email.

    `jobs` is a list of objects with the same keys as add_job's parameters
    (company, title, location required). At most 100 per call.

    Malformed entries do not abort the batch; each contributes an error to the
    per-entry `results` list, which preserves input order. Returns counts of
    created / duplicates / auto_rejected / errors.
    """
    return db.bulk_add_jobs(jobs)


@mcp.tool
@_tool_guard
def check_job(company: str, title: str, location: str) -> dict:
    """Check whether a job is already tracked, without modifying anything.

    add_job is already idempotent, so calling check_job before it is NOT
    required. Use this only when the decision of whether to even look at a
    posting depends on its current status. For triage, prefer bulk_add_jobs.

    Returns the computed id even when the job does not exist, so it can be
    passed straight to a follow-up tool.
    """
    return db.check_job(company, title, location)


@mcp.tool
@_tool_guard
def mark_applied(id: str, date_applied: str | None = None, notes: str | None = None) -> dict:
    """Mark a job as applied to. `date_applied` is ISO YYYY-MM-DD, default today.

    `notes` is APPENDED to any existing notes (an application note adds to the
    record's history). Contrast update_job, whose `notes` REPLACES.

    A rejected job cannot be applied to; reopen it first with
    update_job(id, status="new").
    """
    return db.mark_applied(id, date_applied, notes)


@mcp.tool
@_tool_guard
def reject_job(
    id: str,
    reason: str | None = None,
    add_company_pattern: bool = False,
    add_keyword_pattern: str | None = None,
    add_source_pattern: bool = False,
) -> dict:
    """Reject a job, optionally learning a pattern that auto-rejects future matches.

    add_company_pattern    -- never show this company again (substring match)
    add_keyword_pattern    -- never show titles containing this word
    add_source_pattern     -- never show jobs from this job board (exact match)

    Patterns apply at insert time only: adding one does NOT retroactively
    reject jobs already in the database. Adding a pattern that already exists
    is not an error.
    """
    return db.reject_job(id, reason, add_company_pattern, add_keyword_pattern, add_source_pattern)


@mcp.tool
@_tool_guard
def get_jobs(
    status: str | None = None,
    source: str | None = None,
    min_fit_score: int | None = None,
    limit: int | None = 50,
    offset: int | None = 0,
) -> dict:
    """List jobs, best fit first, then most recently seen. Unscored jobs sort last.

    status        -- new | applied | rejected | expired
    min_fit_score -- excludes unscored jobs, which cannot meet a threshold
    limit         -- clamped to 200

    `total` is the number of jobs matching the filters, ignoring limit/offset.
    If total > offset + len(jobs), there are more pages.
    """
    return db.get_jobs(status, source, min_fit_score, limit, offset)


@mcp.tool
@_tool_guard
def get_job(id: str) -> dict:
    """Retrieve a single job by id."""
    return db.get_job(id)


@mcp.tool
@_tool_guard
def update_job(
    id: str,
    fit_score: int | None = None,
    notes: str | None = None,
    url: str | None = None,
    status: str | None = None,
) -> dict:
    """Update a job's fit_score, notes, url, or status. Only supplied fields change.

    `notes` REPLACES existing notes entirely (this is the correction tool).
    Contrast mark_applied, whose `notes` APPENDS.

    Status transitions: an applied job can never return to "new". A rejected or
    expired job CAN be reopened to "new". Use status="expired" for a role that
    was filled or withdrawn -- there is no separate mark_expired tool.

    Reopening a job does not remove any rejection pattern that auto-rejected
    it; the next add_job for that company would auto-reject again. Use
    remove_rejected_pattern for that.
    """
    return db.update_job(id, fit_score, notes, url, status)


@mcp.tool
@_tool_guard
def merge_jobs(keep_id: str, duplicate_id: str) -> dict:
    """Record that two rows are the same requisition. Nothing is deleted.

    Use this when add_job reports `possible_duplicates` and you judge that two
    postings really are one job -- e.g. "Staff Engineer" and "Staff Software
    Engineer" at the same company. Prefer keeping the older row (earlier
    date_seen), or the one that has been applied to.

    The duplicate is hidden from get_jobs, search_jobs and get_stats, and any
    future add_job that resolves to it is redirected to the survivor. Your
    judgment is stored permanently, so this is decided once and never again.

    If only the duplicate had been applied to, the survivor adopts that
    application rather than losing it. Reversible with unmerge_job.
    """
    return db.merge_jobs(keep_id, duplicate_id)


@mcp.tool
@_tool_guard
def unmerge_job(id: str) -> dict:
    """Undo a merge, restoring the duplicate to its own visible row.

    The duplicate's own fields were never modified by the merge. The survivor
    keeps the times_seen and notes it absorbed.
    """
    return db.unmerge_job(id)


@mcp.tool
@_tool_guard
def get_rejected_patterns() -> dict:
    """List every active rejection pattern, so the user can see what is auto-filtered.

    Values are shown lowercased, which is the form actually matched against.
    """
    return db.get_rejected_patterns()


@mcp.tool
@_tool_guard
def add_rejected_pattern(pattern_type: str, pattern_value: str, reason: str | None = None) -> dict:
    """Add a rejection pattern directly, without rejecting a specific job.

    pattern_type is one of: company | title_keyword | source.
    Company and title_keyword match on substring; source matches exactly.

    Does not retroactively reject jobs already stored. Adding a duplicate
    returns result="exists" and is not an error.
    """
    return db.add_rejected_pattern(pattern_type, pattern_value, reason)


@mcp.tool
@_tool_guard
def remove_rejected_pattern(id: int) -> dict:
    """Remove a rejection pattern, e.g. to reconsider a previously rejected company.

    Takes the integer id from get_rejected_patterns. Jobs that were already
    auto-rejected by this pattern stay rejected; reopen them individually with
    update_job(id, status="new").
    """
    return db.remove_rejected_pattern(id)


@mcp.tool
@_tool_guard
def get_stats() -> dict:
    """Summary counts for dashboard reporting.

    `unscored_new` is the triage backlog: new jobs with no fit_score yet.
    avg_fit_score_* is null (not 0) when nothing in that status is scored.
    """
    return db.get_stats()


@mcp.tool
@_tool_guard
def search_jobs(
    query: str,
    status: str | None = None,
    limit: int | None = 20,
    offset: int | None = 0,
) -> dict:
    """Case-insensitive substring search over company, title, notes, email_subject.

    Wildcards in `query` are escaped, so searching "100%" matches literally.
    Returns the same shape as get_jobs.
    """
    return db.search_jobs(query, status, limit, offset)


if __name__ == "__main__":
    mcp.run()  # stdio transport
