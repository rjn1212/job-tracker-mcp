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
# These libraries are chatty and their handlers must never reach stdout, which
# carries the JSON-RPC protocol. Set before anything imports the onenote package.
for _noisy in ("msal", "httpx", "httpcore", "urllib3", "requests"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger("job_tracker_mcp")

mcp = FastMCP("job-tracker")
db = Database()


@functools.cache
def _onenote():
    """Build the OneNote service on first use, never at import.

    `db = Database()` above runs during module import, before FastMCP is
    listening. An msal or httpx import that touched the network there would make
    the server appear crashed to Claude Desktop with no diagnosable output. So
    the onenote package -- and its network stack -- is imported here, lazily.
    """
    from job_tracker_mcp.onenote.service import OneNoteService
    return OneNoteService(db)


def _onenote_guard(fn):
    """Turn the OneNote failure modes into the error strings the model must read."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        from job_tracker_mcp.onenote.auth import AuthError
        from job_tracker_mcp.onenote.graph import GraphError
        try:
            return fn(*args, **kwargs)
        except (AuthError, GraphError) as exc:
            return {"error": str(exc)}
    return wrapper


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


# ---------------------------------------------------------------------------
# Part 2: OneNote. Tools 16-25.
#
# Every one of these degrades to a clean error string when OneNote is
# unconfigured. None of them can hang, and none is reachable during import.
# ---------------------------------------------------------------------------


@mcp.tool
@_tool_guard
def onenote_auth_status() -> dict:
    """Diagnose the OneNote connection. The first tool to call when anything fails.

    Never triggers interactive sign-in and makes no notebook calls. Returns
    configured=false (NOT an error) when the Azure app id is unset, with setup
    instructions in `message`.

    A `reason` of "keychain_timeout" means macOS is showing an approval dialog
    this process cannot answer -- usually because .venv was rebuilt since the
    last sign-in. Re-run onenote-auth.sh.
    """
    return _onenote().auth_status()


@mcp.tool
@_tool_guard
@_onenote_guard
def onenote_list_sections(notebook: str | None = None) -> dict:
    """Enumerate notebooks and their sections, with full section-group paths.

    One-time discovery: use it to confirm the exact displayName of the section
    to track, or to find ids when two sections share a name.
    """
    return _onenote().list_sections(notebook)


@mcp.tool
@_tool_guard
@_onenote_guard
def onenote_sync(full: bool = False, fetch_content: bool = True,
                 max_pages: int = 200) -> dict:
    """Refresh the local mirror of the OneNote section. The ONLY tool that needs network.

    Every other OneNote tool reads the local cache, so call this first if you
    need fresh data. Page content is refetched only when the page changed
    upstream, which keeps a steady-state sync to a single HTTP request.

    full=true refetches every page's content -- use only after the text
    extractor itself has changed. fetch_content=false is a fast metadata-only
    pass. If `truncated` is true, pages exist that were NOT synced.
    """
    return _onenote().sync(full=full, fetch_content=fetch_content, max_pages=max_pages)


@mcp.tool
@_tool_guard
def onenote_list_pages(include_ignored: bool = False, include_deleted: bool = False,
                       modified_since: str | None = None,
                       limit: int = 50, offset: int = 0) -> dict:
    """List cached pages, newest edit first. Reads the mirror; never hits the network.

    Page bodies are NOT included -- a 47-page dump would obliterate your context.
    Use onenote_get_page for one page's text.
    """
    limit = max(1, min(int(limit), 200))
    return _onenote().store.list_pages(include_ignored, include_deleted,
                                       modified_since, limit, max(0, int(offset)))


@mcp.tool
@_tool_guard
def onenote_get_page(page_id: str, format: str = "text",
                     max_chars: int = 20000, offset: int = 0) -> dict:
    """Read one cached page as plain text (or raw html, rarely wanted).

    Long pages are paged: when `truncated` is true, call again with
    offset=next_offset. `stale` means the cached text predates the page's most
    recent edit -- run onenote_sync.

    This is the tool for Tier-B extraction: read an unparsed page, understand the
    application records in it, then call onenote_link_page with the date.
    """
    return _onenote().get_page(page_id, format, max_chars, offset)


@mcp.tool
@_tool_guard
def onenote_search_pages(query: str, limit: int = 20, offset: int = 0) -> dict:
    """Case-insensitive substring search over cached page titles and text.

    Returns matched lines as snippets, not whole pages. Wildcards are escaped.
    """
    if not query or not query.strip():
        return {"error": "query must not be empty"}
    limit = max(1, min(int(limit), 200))
    return _onenote().store.search_pages(query, limit, max(0, int(offset)))


@mcp.tool
@_tool_guard
@_onenote_guard
def onenote_create_page(title: str, body_lines: list[str],
                        job_id: str | None = None, confirm: bool = False) -> dict:
    """Create a page in the OneNote section. The ONLY tool that writes to OneNote.

    Gated behind JOB_TRACKER_ONENOTE_WRITE=1, and a DRY RUN by default: call once
    with confirm=false to show the user the preview, then again with confirm=true
    only after they agree. Page creation cannot be undone through this server --
    there is no delete tool, by design.

    Passing job_id stamps a visible `job-tracker-id:` marker into the page, which
    is what makes this idempotent: a second call for the same job returns
    result="exists" and creates nothing.
    """
    return _onenote().create_page(title, body_lines, job_id, confirm)


@mcp.tool
@_tool_guard
def onenote_link_page(page_id: str, job_id: str, unlink: bool = False,
                      evidence: str | None = None, date_applied: str | None = None,
                      extracted: dict | None = None) -> dict:
    """Assert that a page documents a job, when automatic matching could not.

    This is the second half of Tier-B extraction, and the loop that resolves
    `ambiguous` entries. A manual link outranks everything and is never
    overwritten by a later reconciliation.

    PASS `date_applied` (ISO YYYY-MM-DD) whenever the page states when you
    applied. Without it, reconciliation has no date to record and would use
    today's -- permanently misdating an application you made months ago.

    unlink=true removes a wrong match. It is refused for pages this server
    created; delete those in OneNote first.
    """
    return _onenote().link_page(page_id, job_id, unlink, evidence, date_applied, extracted)


@mcp.tool
@_tool_guard
def onenote_ignore_page(page_id: str, ignore: bool = True) -> dict:
    """Mark a page as not-an-application, so reconciliation stops reporting it.

    A "Job Searching" section holds resume drafts, interview prep, and recruiter
    lists as well as applications. Without this, every report carries the same
    permanent noise and the user learns to skim past all of it.
    """
    return _onenote().ignore_page(page_id, ignore)


@mcp.tool
@_tool_guard
def reconcile_applications(dry_run: bool = True, min_confidence: float = 0.6,
                           apply_marks: bool = False, limit: int = 100) -> dict:
    """Cross-reference tracked jobs against the OneNote mirror. The point of Part 2.

    Reads the local cache; run onenote_sync first for fresh data. Reports:
      matched       -- both sources agree
      onenote_only  -- the page says applied, the database does not
      db_only       -- the database says applied, no page documents it
      ambiguous     -- several jobs plausibly match; a human must pick
      unmatched     -- a page names a company the tracker has never seen
      unparsed_pages-- read them with onenote_get_page, then onenote_link_page

    MUTATION IS OPT-IN TWICE: pass BOTH dry_run=false AND apply_marks=true to
    call mark_applied for onenote_only entries. Ambiguous entries are never acted
    on. db_only is never acted on -- creating pages in bulk is irreversible.
    Status transitions are still enforced: a rejected job lands in `skipped`.
    """
    return _onenote().reconcile(dry_run, min_confidence, apply_marks, limit)


if __name__ == "__main__":
    mcp.run()  # stdio transport
