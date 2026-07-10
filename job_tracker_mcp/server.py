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
    date_seen: str | None = None,
) -> dict:
    """Add a job posting to the tracker.

    Idempotent: adding the same (company, title, location) twice records a
    re-sighting rather than a second row, and never overwrites an existing
    fit_score, status, or notes.

    ALWAYS pass `date_seen` (ISO YYYY-MM-DD) as the date of the alert email the
    posting came from -- NOT today. It records when the job first appeared. If
    you omit it, importing a backlog of three weeks of emails stamps every job
    with today's date and the history is lost. On a re-sighting the server keeps
    the EARLIEST date_seen and the LATEST last_seen, so emails may be processed
    in any order.

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
    return db.add_job(company, title, location, url, source, fit_score, notes,
                      email_subject, date_seen)


@mcp.tool
@_tool_guard
def bulk_add_jobs(jobs: list[dict]) -> dict:
    """Add many jobs in one call. Use this when triaging a job alert email.

    `jobs` is a list of objects with the same keys as add_job's parameters
    (company, title, location required). At most 100 per call.

    Set `date_seen` on every entry to the alert email's date, not today. One
    call per email keeps that simple.

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
    include_off_profile: bool = False,
) -> dict:
    """List jobs, best fit first, then most recently seen. Unscored jobs sort last.

    status        -- new | applied | rejected | expired
    min_fit_score -- excludes unscored jobs, which cannot meet a threshold
    limit         -- clamped to 200
    include_off_profile -- by default, NEW jobs that match no profile rule are
                     hidden. They are stored, not rejected. Pass true to see
                     them, e.g. to check whether a rule is too narrow.

    Applied, rejected and expired jobs are never hidden by profile rules --
    history must not vanish because you later narrowed what you are looking for.

    `total` is the number of jobs matching the filters, ignoring limit/offset.
    If total > offset + len(jobs), there are more pages.
    """
    return db.get_jobs(status, source, min_fit_score, limit, offset, include_off_profile)


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


@mcp.tool
@_tool_guard
def get_profile_rules() -> dict:
    """Show the positive rules that decide which jobs are on-profile.

    Unlike rejected_patterns these DEMOTE rather than reject. A job matching no
    title_include rule (or matching a title_exclude) is still stored as "new",
    flagged off_profile, and hidden from the default get_jobs list. Nothing is
    discarded, and get_jobs(include_off_profile=true) shows everything.
    """
    return db.get_profile_rules()


@mcp.tool
@_tool_guard
def add_profile_rule(rule_type: str, value: str, reason: str | None = None) -> dict:
    """Add a positive rule. rule_type is title_include or title_exclude.

    title_include -- a job is on-profile if its title contains ANY include value.
                     With no include rules at all, every job is on-profile.
    title_exclude -- a job is off-profile if its title contains ANY exclude value.
                     Excludes beat includes.

    Both are substring matches against the canonicalized title, so "(Remote)",
    salary and req ids are already gone -- but seniority is NOT. Case-insensitive.

    off_profile is DERIVED, so adding a rule immediately reclassifies existing
    jobs. The response says how many. This never changes any job's status: an
    off-profile job is hidden, not rejected. Contrast add_rejected_pattern,
    which applies only to future inserts because rejecting IS a decision.

    Beware narrow include lists. "director" demotes "Head of Engineering" and
    "VP, Engineering". Check with get_jobs(include_off_profile=true) afterward.
    """
    return db.add_profile_rule(rule_type, value, reason)


@mcp.tool
@_tool_guard
def remove_profile_rule(id: int) -> dict:
    """Remove a positive rule, and reclassify every job against what remains."""
    return db.remove_profile_rule(id)


@mcp.tool
@_tool_guard
def recompute_profiles() -> dict:
    """Re-derive off_profile for every live job from the current rules.

    Rules already recompute on add/remove, so this is only needed after a direct
    database edit or a code change to the matcher. Safe: it never touches status.
    """
    return db.recompute_profiles()


@mcp.tool
@_tool_guard
def get_triage_state(account: str, mailbox: str) -> dict:
    """Where the last triage run stopped for this mailbox. Call this FIRST.

    Returns `last_message_dt` -- the date_received of the newest alert email
    already processed -- or has_run=false if this mailbox has never been
    triaged, in which case process everything.

    Read messages with date_received >= last_message_dt, INCLUSIVELY. Re-reading
    the boundary message is free: add_job is idempotent and a re-sighting keeps
    the earliest date_seen. A gap is not free -- it loses a job silently.
    """
    return db.get_triage_state(account, mailbox)


@mcp.tool
@_tool_guard
def record_triage_run(account: str, mailbox: str, last_message_dt: str,
                      emails_processed: int = 0, postings_seen: int = 0) -> dict:
    """Advance the watermark. Call this LAST, only after every posting is filed.

    `last_message_dt` must be the date_received of the NEWEST email you actually
    processed -- copied verbatim from search_messages, offset and all. Never
    "now": mail that arrived while you were working has a later date_received
    and must be picked up next time, not skipped because the clock moved.

    If the run failed partway, do not call this. A watermark that ran ahead of
    the work silently loses every email in between. The watermark never moves
    backward, so a partial re-run of older mail cannot erase progress.
    """
    return db.record_triage_run(account, mailbox, last_message_dt,
                                emails_processed, postings_seen)


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


# ---------------------------------------------------------------------------
# MCP prompts.
#
# The workflow lives here rather than in a pasted chat message or a CLAUDE.md,
# for one reason: a prompt served by the server ships with the tools it calls,
# in the same commit, and cannot drift out of sync with them. Claude Desktop
# and Claude Code both read it from the server.
#
# What must be IDENTICAL every run belongs in code, not in this text:
# dedup (the id hash), filters (rejected_patterns), the status lifecycle.
# A prompt cannot guarantee those -- it can only usually remember them.
# ---------------------------------------------------------------------------


@mcp.prompt
def triage_job_alerts(
    since: str = "",
    max_emails: int = 25,
    account: str = "RandyN Exchange",
    mailbox: str = "Job_Alerts_Viewed",
) -> str:
    """The standard job-alert triage run. Use this every time, unchanged.

    Reads job alert emails via the `apple-mail` server, files every posting
    through this `job-tracker` server, and reports what needs a human.
    """
    override = (f"\n    The user overrode the watermark: use since = {since} instead of\n"
                f"    last_message_dt, but STILL record the run at the end.\n"
                if since else "")
    return f"""\
Triage my job alert emails. Follow these steps in order. Do not improvise.

Two MCP servers are involved, by these exact names:
  - `apple-mail`   : reads Apple Mail. Tools: list_mailboxes, search_messages,
                     get_message, get_attachments.
  - `job-tracker`  : this server. Everything else below.

STEP 0 — Find out where you left off.
Call `job-tracker` get_triage_state(account="{account}", mailbox="{mailbox}").

  - has_run = false  -> this mailbox has never been triaged. Process EVERY
                        message in it. This is the first full pass.
  - has_run = true   -> process only messages whose date_received is
                        >= last_message_dt, INCLUSIVE of that boundary message.

Inclusive is deliberate. Re-reading the boundary email is free, because add_job
is idempotent and a re-sighting keeps the earliest date_seen. A gap is not free:
it silently loses a job. When in doubt, overlap.
{override}
STEP 1 — Fetch the alerts.
Call `apple-mail` search_messages with EXACTLY:
    account = "{account}"
    mailbox = "{mailbox}"
    limit   = {max_emails}
Both `account` and `mailbox` are mandatory -- that build is restricted to this
one mailbox and returns "access restricted" for any other. `limit` may go up to
1000; raise it for the first full pass.

search_messages has NO date filter. It returns
{{account, mailbox, count, messages[]}}, where each message has:
    id, subject, sender, date_received, date_sent, read_status, flagged

Apply the STEP 0 cutoff yourself, after the call.

USE `date_received`, NEVER `date_sent`. date_sent is unset in this mailbox and
comes back as "0001-01-01T00:00:00Z". date_received looks like
"2026-07-09T17:33:43-05:00" -- it already carries the local offset, so its first
ten characters are the local calendar date. Take that YYYY-MM-DD directly; do
not convert it.

If `count` equals your `limit`, there may be more mail than you fetched. Say so
rather than assuming you saw everything.

STEP 2 — Extract postings, one email at a time.
For each surviving message, call `apple-mail` get_message(message_id=<its id>,
include_content=true). The body arrives under the "message" key as PLAIN TEXT,
not HTML.

EVERY ALERT CONTAINS SEVERAL POSTINGS -- typically five. Never stop at the first
one, and never trust the subject line: Indeed's subject names one job and the
body holds five. Count what you extracted and say so.

Five senders appear in this mailbox, with FOUR different layouts. Read what is
there; do not assume.

  LinkedIn Job Alerts AND LinkedIn Job Recommendations -- identical layout.
  Title first, then company and location joined by a middle dot:
      Director, Software Engineering (1139652)
      The Judge Group · Chicago, IL
      $186K-$248K / year            <- optional, own line
      Easy Apply                    <- noise; also "Actively recruiting", "N alumni"

  Indeed -- title, company, location, salary, each on its own line:
      Senior Product Manager
      DocuSign
      Chicago, IL
      $140,100 - $206,775 a year    <- optional
      Degree in Computer Science... <- snippet; noise
      Just posted                   <- noise; also "1 day ago", "Easily apply"

  Built In -- COMPANY FIRST, then the title. The order is inverted:
      CVS Health
      Senior Engineering Manager API Platform
      In Office | Multiple Locations
      $118,450-$260,590             <- optional
  Built In separates work-type from location with an invisible character
  (U+FFFC, often a box). "In Office", "Remote", "Hybrid" are the work type;
  the rest is the location. Ignore the box characters.

  Direct-from-company notifications (e.g. jobs2web.com) -- ONE job, and the
  company is named NOWHERE except the sender domain and the preamble:
      From: fitchgroupincp-jobnotification@noreply12.jobs2web.com
      "You are receiving this email because you joined the Fitch Group..."
      Software Engineering, Full Stack - AI - Chicago, IL, US
  Here the single line is "<title> - <City, ST, Country>". Take the company from
  the preamble or the domain. Set source to the company, not to a job board.

TWO TRAPS.

1. STOP AT THE FOOTER. Extraction ends at "See all jobs", "View all jobs" or
   "GET MORE RECOMMENDATIONS". Below that, LinkedIn prints
   "This email was intended for Randy Nelson (SVP - Senior Manager of Software
   Engineering)" -- which looks exactly like a job title and is not one.

2. "(Remote)" SOMETIMES SITS IN THE TITLE. LinkedIn writes both
   "Director, Software Engineering (Remote)" with location "Oak Brook, IL", and
   "Head of Software Engineering" with location "Chicago, IL (Remote)". Same
   fact, two places. When the TITLE says (Remote) and the location line names a
   city with no remote marker, append " (Remote)" to the location you pass, so
   both spellings reach the same row. Leave the title alone -- the server strips
   "(Remote)" from titles itself.

Location is NEVER part of the company line's job title on any of these boards.

Copy the strings VERBATIM. Do not tidy them. "Acme, Inc." stays "Acme, Inc.";
"Staff Engineer (Remote) [R-1234]" keeps its decorations; "Remote, US" stays as
written. The server canonicalizes all three itself, deterministically, and
hand-normalizing destroys information without improving the dedup.

STEP 3 — File them.
Call `job-tracker` bulk_add_jobs ONCE PER EMAIL, with:
  - date_seen  = that message's date_received[:10], in YYYY-MM-DD.
                 NOT today's date. This records when the posting first appeared,
                 and the server keeps the earliest across re-sightings, so it is
                 safe to process emails out of order.
  - email_subject = that message's subject line.
  - source     = the job board.
Never pass more than 100 postings in one call.

STEP 4 — Read the response. Do not skip this.
  - created        : new jobs, now status="new", awaiting a fit score.
  - duplicates     : already tracked. The server kept the earliest date_seen.
                     Nothing to do.
  - auto_rejected  : matched one of my rejection patterns. Already stored as
                     status="rejected". Do NOT re-add or argue with them; just
                     tell me how many and which patterns fired.
  - errors         : report them verbatim.
  - possible_duplicates : a similar title at the same company. The job WAS
                     created. Look at each pair and decide whether they are the
                     same requisition. If they are, call merge_jobs(keep_id,
                     duplicate_id), keeping the older row. If unsure, leave both
                     and tell me.

STEP 5 — Score the new ones.
Call get_jobs(status="new") and set a fit_score of 1-10 on each via
update_job(id, fit_score=N). Score against my actual background; be honest
rather than generous. Unscored jobs sort last, so an unscored backlog is
invisible -- get_stats().unscored_new tells you how many remain.

STEP 6 — Report, in prose, in this order:
  1. How many emails, how many postings.
  2. What is worth my attention: new jobs with fit_score >= 7, newest first.
  3. What was auto-rejected, and by which pattern. If a pattern is catching
     things it should not, say so.
  4. Any possible_duplicates you did not resolve, and why.
  5. get_stats(): the total, the by_status counts, and unscored_new.

STEP 7 — Record where you stopped. Do this LAST.
Call `job-tracker` record_triage_run with:
    account          = "{account}"
    mailbox          = "{mailbox}"
    last_message_dt  = the date_received of the NEWEST message you actually
                       processed, copied verbatim, offset and all.
    emails_processed = how many you read
    postings_seen    = how many postings you filed

Never pass "now" as last_message_dt. Mail that arrived while you were working
has a later date_received, and a watermark set to the clock would skip it
forever.

If anything went wrong -- a tool errored, you could not read an email, you ran
out of context -- DO NOT call record_triage_run. Tell me what failed. A
watermark that ran ahead of the work silently loses every email in between, and
nothing will ever go back for them.

Never call mark_applied. Applying is something I do, and I will tell you.
Never call add_rejected_pattern on your own initiative. Suggest it; I decide.
"""


@mcp.prompt
def setup_job_filters() -> str:
    """Help me define the auto-rejection filters, then install them.

    Run this once, and again whenever a filter is wrong. Filters are DATA in
    the database, not instructions -- so they cannot be forgotten mid-run.
    """
    return """\
Help me define my job-alert filters, then install them.

All tools below are on the `job-tracker` MCP server.

THERE ARE TWO KINDS OF FILTER, and they behave differently on purpose.

  NEGATIVE (rejected_patterns) -- a match REJECTS the job.
  POSITIVE (profile_rules)     -- a non-match DEMOTES the job.

The asymmetry is deliberate. A bad negative rule wrongly rejects one company,
and the row is still stored, tagged with the pattern that caught it, and one
update_job from coming back. A bad POSITIVE rule silently drops everything it
did not anticipate -- "Head of Engineering", "Distinguished Engineer", "VP,
Platform" -- and the good jobs are exactly the ones with unusual titles. So a
positive non-match never rejects: it sets off_profile, which hides the job from
the default list and nothing more.

=== NEGATIVE FILTERS: rejected_patterns ===

A match stores the job with status="rejected". Three kinds:

  company        substring match against the canonicalized company name.
                 "infosys" matches "Infosys BPM Ltd" and "Infosys, Inc."
                 Legal suffixes (Inc, LLC, Ltd, Corp) are already stripped, so
                 do not include them in the pattern.

  title_keyword  substring match against the canonicalized job title.
                 "staffing" matches "Staffing Coordinator".
                 The title has already had "(Remote)", salary ranges and req
                 ids stripped -- but NOT seniority. "Senior" and "II" survive.

  source         EXACT match against the job board name, case-insensitive.
                 "dice" matches source "Dice" but NOT "Dice Inc". Exact, because
                 sources come from a small closed set and substring matching
                 there would surprise.

Matching is case-insensitive. Patterns are checked at INSERT time only: adding
a pattern never retroactively rejects jobs already in the database. That is on
purpose -- rejecting is a decision, and a retroactive one could bury a job I
already applied to.

=== POSITIVE FILTERS: profile_rules ===

Two kinds, both substring matches against the canonical title:

  title_include  a job is on-profile if its title contains ANY include value.
                 WITH NO INCLUDE RULES AT ALL, EVERY JOB IS ON-PROFILE.
  title_exclude  a job is off-profile if its title contains ANY exclude value.
                 Excludes beat includes: "Junior Director" is off-profile even
                 with include "director".

Both compare against the CANONICAL title -- "(Remote)", "[R-1234]" and salary
ranges are already stripped; seniority is not. Write rules in that form. A rule
the canonicalizer would rewrite is REFUSED, not installed dead; the error names
the form that works. These rules see the TITLE ONLY: not the company, not the
location, not the email body. Filter companies with a `company` pattern.

A non-matching job is stored as status="new" with off_profile=true. It is hidden
from get_jobs() but visible with get_jobs(include_off_profile=true), and it is
counted in get_stats().off_profile_new.

off_profile is DERIVED, not a decision, so add_profile_rule and
remove_profile_rule immediately RECLASSIFY existing jobs and report how many.
Status is never touched. Applied jobs are never hidden -- history must not
vanish because I narrowed my search later.

Include lists are dangerous when narrow. "director" demotes "Head of
Engineering". Propose a GENEROUS include list, then check it.

A matching job is STILL STORED, with status="rejected" and a note recording
which pattern fired. It is not discarded. This is deliberate:
  - dedup: the same posting arrives in next week's alert; a stored rejection
    means you decide once, not every week.
  - audit: you can see what the filter ate, which is the only way to notice a
    filter is too broad.
  - reversal: remove_rejected_pattern, then update_job(id, status="new").
A silently discarded job is the one thing that cannot be undone.

BE CAREFUL WITH SUBSTRINGS. "eng" matches "Engineering". "ai" matches "Air",
"Maintain", and "Detail". Prefer whole words. Test a candidate pattern against
what is already tracked before installing it:
    search_jobs(query="<pattern>")
and read what comes back. If it catches something you want, do not install it.

WHAT TO DO NOW.

1. Ask me what I want. Probe for:
   - staffing agencies and body shops by name        -> company
   - title words that mean "never"                    -> title_keyword
     (contract, clearance) -- these REJECT.
   - job boards I do not trust                        -> source
   - the titles I actually want                       -> title_include
   - title words that mean "not my level"             -> title_exclude
     (junior, intern, associate) -- these DEMOTE.

   The line between title_keyword (reject) and title_exclude (demote) is: would
   I ever want to see this job again? If never, reject. If it is merely off my
   path, demote.

2. Test every candidate BEFORE installing it. Call
       search_jobs(query="<value>")
   and read what it would have caught among my real jobs. A rule that catches a
   job I applied to is a bad rule. Say so, and propose a narrower one.

3. For the include list, err GENEROUS. Then, after installing, call
       get_jobs(include_off_profile=true)
   and show me every job the rules demoted. If a good job is in there, the
   include list is too narrow -- fix it before we go further.

4. Show me the final lists and WAIT for me to approve them.

5. Only then call add_rejected_pattern(...) and add_profile_rule(...) for each.
   Always give a reason -- in six months it is the only thing that explains why
   a company is invisible.

6. Finish with get_rejected_patterns() and get_profile_rules() so I can see
   exactly what is installed, then get_stats() for off_profile_new.

Do not install anything I have not explicitly approved.
"""


if __name__ == "__main__":
    mcp.run()  # stdio transport
