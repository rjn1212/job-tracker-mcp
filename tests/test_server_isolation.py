"""OneNote must never be able to break Part 1, or hang the stdio transport."""

import asyncio
import contextlib
import io
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")


def _fresh_import(env_extra: dict, code: str) -> subprocess.CompletedProcess:
    """Import the server in a clean process so module-level state is real."""
    import os
    env = {k: v for k, v in os.environ.items() if not k.startswith("JOB_TRACKER_MS")}
    env.update(env_extra)
    return subprocess.run([PY, "-c", code], capture_output=True, text=True,
                          env=env, cwd=str(ROOT), timeout=60)


# Neither httpx nor requests is asserted absent: fastmcp imports httpx directly,
# and its jsonref dependency imports requests. Both are in the startup path
# whatever Part 2 does. What MUST stay out is msal (which reads the Keychain) and
# the onenote modules that build a Graph client -- those are what would turn a
# startup failure into a server that Claude Desktop sees as crashed, with no
# diagnosable output. Assert the invariant we actually control.
IMPORT_AND_COUNT = """
import asyncio, contextlib, io, sys
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    from job_tracker_mcp import server
assert buf.getvalue() == "", "stdout polluted: %r" % buf.getvalue()
tools = asyncio.run(server.mcp.list_tools())
assert all(t.description for t in tools), "a tool has no description"
sys.stderr.write("TOOLS=%d\\n" % len(tools))
assert "msal" not in sys.modules, "msal imported at startup"
for mod in ("service", "auth", "graph", "client"):
    full = "job_tracker_mcp.onenote." + mod
    assert full not in sys.modules, full + " imported at startup"
"""


def test_import_without_client_id(tmp_path):
    """Registers every tool, writes nothing to stdout, pulls in no network stack."""
    r = _fresh_import({"JOB_TRACKER_DB": str(tmp_path / "t.db")}, IMPORT_AND_COUNT)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""
    assert "TOOLS=35" in r.stderr


def test_database_constructor_imports_no_network_stack(tmp_path):
    """Point 4 of Failure Isolation: db = Database() runs before FastMCP listens."""
    # Database alone must pull in NO network stack at all -- not even httpx.
    code = (
        "import sys\n"
        "from job_tracker_mcp.database import Database\n"
        "Database()\n"
        "for mod in ('msal', 'httpx', 'requests'):\n"
        "    assert mod not in sys.modules, mod + ' imported by Database()'\n"
    )
    r = _fresh_import({"JOB_TRACKER_DB": str(tmp_path / "t.db")}, code)
    assert r.returncode == 0, r.stderr


def test_part1_tools_unaffected(tmp_path):
    code = (
        "import sys\n"
        "from job_tracker_mcp import server\n"
        "r = server.add_job('Acme', 'Staff Engineer', 'Remote, US')\n"
        "assert r['result'] == 'created', r\n"
        "s = server.get_stats()\n"
        "assert s['total_jobs'] == 1, s\n"
        "assert server.merge_jobs('x', 'x')['error']\n"
    )
    r = _fresh_import({"JOB_TRACKER_DB": str(tmp_path / "t.db")}, code)
    assert r.returncode == 0, r.stderr


# --------------------------------------- unconfigured OneNote degrades cleanly

@pytest.fixture
def unconfigured(monkeypatch, tmp_path):
    monkeypatch.delenv("JOB_TRACKER_MS_CLIENT_ID", raising=False)
    monkeypatch.setenv("JOB_TRACKER_DB", str(tmp_path / "t.db"))
    from job_tracker_mcp.database import Database
    from job_tracker_mcp.onenote.service import OneNoteService
    db = Database(db_path=tmp_path / "t.db")
    yield OneNoteService(db)
    db.close()


def test_auth_status_unconfigured_is_a_diagnosis_not_an_error(unconfigured):
    """'You have not set this up yet' is a SUCCESSFUL diagnosis.

    An error string would send the model hunting for a bug.
    """
    out = unconfigured.auth_status()
    assert "error" not in out
    assert out["configured"] is False
    assert out["authenticated"] is False
    assert out["reason"] == "not_configured"
    assert "entra.microsoft.com" in out["message"]


def test_auth_status_never_hits_network(unconfigured):
    unconfigured.auth_status()          # the _no_network fixture would explode


def test_persisted_client_id_is_used_when_env_is_unset(unconfigured, monkeypatch):
    """onenote-auth.sh saves the client id, so the server needs no env var.

    The id is a public identifier, not a secret. Requiring it in two unrelated
    places -- a shell export and Claude Desktop's env block -- is how setup fails.
    """
    from job_tracker_mcp.onenote import auth
    from job_tracker_mcp.onenote.service import OneNoteService

    monkeypatch.delenv("JOB_TRACKER_MS_CLIENT_ID", raising=False)
    unconfigured.store.set_state(auth.CLIENT_ID_KEY, "saved-guid")

    svc = OneNoteService(unconfigured.db)      # re-read state on construction
    assert svc.auth_status()["configured"] is True
    assert auth.client_id() == "saved-guid"


def test_env_client_id_wins_over_persisted(unconfigured, monkeypatch):
    from job_tracker_mcp.onenote import auth
    from job_tracker_mcp.onenote.service import OneNoteService

    unconfigured.store.set_state(auth.CLIENT_ID_KEY, "saved-guid")
    monkeypatch.setenv("JOB_TRACKER_MS_CLIENT_ID", "env-guid")
    OneNoteService(unconfigured.db)
    assert auth.client_id() == "env-guid"


def test_client_id_fallback_does_not_leak_between_databases(unconfigured, tmp_path,
                                                            monkeypatch):
    """A stale module-level id must not follow a service onto another database."""
    from job_tracker_mcp.database import Database
    from job_tracker_mcp.onenote import auth
    from job_tracker_mcp.onenote.service import OneNoteService

    monkeypatch.delenv("JOB_TRACKER_MS_CLIENT_ID", raising=False)
    unconfigured.store.set_state(auth.CLIENT_ID_KEY, "saved-guid")
    OneNoteService(unconfigured.db)
    assert auth.client_id() == "saved-guid"

    other = Database(db_path=tmp_path / "other.db")
    try:
        assert OneNoteService(other).auth_status()["configured"] is False
    finally:
        other.close()


def test_onenote_tools_unconfigured_return_errors_not_exceptions(unconfigured):
    from job_tracker_mcp.onenote.auth import AuthError
    with pytest.raises(AuthError):      # server.py's _onenote_guard turns this into {"error"}
        unconfigured.list_sections()


def test_cached_reads_work_with_no_auth(unconfigured):
    """Reading the mirror must never require a token."""
    assert unconfigured.store.list_pages()["total"] == 0
    assert unconfigured.get_page("nope")["error"]
    assert unconfigured.store.search_pages("x")["total"] == 0


def test_reconcile_works_offline(unconfigured):
    unconfigured.db.add_job(company="Acme", title="Staff Engineer", location="Remote")
    out = unconfigured.reconcile()
    assert "error" not in out
    assert out["matched_count"] == 0
    assert out["dry_run"] is True


def test_create_page_requires_write_env(unconfigured, monkeypatch):
    monkeypatch.delenv("JOB_TRACKER_ONENOTE_WRITE", raising=False)
    out = unconfigured.create_page("T", ["b"], confirm=True)
    assert "JOB_TRACKER_ONENOTE_WRITE" in out["error"]


def test_stdout_purity_of_onenote_imports():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        import job_tracker_mcp.onenote.auth  # noqa: F401
        import job_tracker_mcp.onenote.client  # noqa: F401
        import job_tracker_mcp.onenote.graph  # noqa: F401
        import job_tracker_mcp.onenote.service  # noqa: F401
    assert buf.getvalue() == ""


# ------------------------------------------------------------------ prompts

def test_prompts_are_registered_and_documented():
    import asyncio
    from job_tracker_mcp import server
    prompts = asyncio.run(server.mcp.list_prompts())
    names = {p.name for p in prompts}
    assert names == {"triage_job_alerts", "setup_job_filters", "review_new_jobs"}
    assert all(p.description for p in prompts)


def test_prompts_render_through_a_real_mcp_client():
    """Listing them is not enough; Claude Desktop calls get_prompt."""
    import asyncio
    from fastmcp import Client
    from job_tracker_mcp import server

    async def go():
        async with Client(server.mcp) as c:
            r = await c.get_prompt("triage_job_alerts", {"since": "2026-07-01"})
            return r.messages[0].content.text

    body = asyncio.run(go())
    assert "2026-07-01" in body                  # the argument is substituted
    assert len(body) > 1000


def test_triage_prompt_pins_the_things_that_go_wrong():
    from job_tracker_mcp.server import triage_job_alerts
    body = triage_job_alerts(since="2026-07-01")
    assert "2026-07-01" in body
    assert "NOT today's date" in body            # date_seen is the email's date
    assert "VERBATIM" in body                    # no hand-normalizing
    assert "Never call mark_applied" in body     # applying is the human's act
    assert "possible_duplicates" in body


def test_triage_prompt_names_the_real_servers_and_tools():
    """The apple-mail build is restricted to one account and mailbox.

    Omitting either yields 'access restricted', so the prompt must supply both.
    """
    from job_tracker_mcp.server import triage_job_alerts
    body = triage_job_alerts()
    assert "`apple-mail`" in body and "`job-tracker`" in body
    assert "RandyN Exchange" in body and "Job_Alerts_Viewed" in body
    for tool in ("search_messages", "get_message", "bulk_add_jobs"):
        assert tool in body


def test_triage_prompt_warns_off_the_broken_date_field():
    """date_sent is 0001-01-01T00:00:00Z in this mailbox; date_received is real."""
    from job_tracker_mcp.server import triage_job_alerts
    body = triage_job_alerts()
    assert "date_received" in body
    assert "NEVER `date_sent`" in body
    assert "0001-01-01" in body                  # names the value it would produce


def test_triage_prompt_accepts_a_different_mailbox():
    """account/mailbox are prompt arguments, so the restriction can move."""
    from job_tracker_mcp.server import triage_job_alerts
    body = triage_job_alerts(account="Other", mailbox="INBOX")
    assert '"Other"' in body and '"INBOX"' in body


def test_filter_prompt_explains_the_three_pattern_types():
    from job_tracker_mcp.server import setup_job_filters
    body = setup_job_filters()
    for kind in ("company", "title_keyword", "source"):
        assert kind in body
    assert "STILL STORED" in body                # filtered jobs are kept
    assert "WAIT for me to approve" in body      # never installs unilaterally


def test_triage_prompt_uses_the_watermark():
    from job_tracker_mcp.server import triage_job_alerts
    body = triage_job_alerts()
    assert "get_triage_state" in body
    assert "record_triage_run" in body
    assert "INCLUSIVE" in body                   # overlap is free, gaps are not
    assert "Never pass \"now\"" in body
    assert "DO NOT call record_triage_run" in body   # never advance past a failure


def test_triage_prompt_since_overrides_watermark():
    from job_tracker_mcp.server import triage_job_alerts
    assert "overrode the watermark" in triage_job_alerts(since="2026-07-01")
    assert "overrode the watermark" not in triage_job_alerts()


def test_filter_prompt_distinguishes_reject_from_demote():
    from job_tracker_mcp.server import setup_job_filters
    body = setup_job_filters()
    assert "NEGATIVE" in body and "POSITIVE" in body
    assert "title_include" in body and "title_exclude" in body
    assert "EVERY JOB IS ON-PROFILE" in body      # empty include list is not a trap
    assert "err GENEROUS" in body                 # narrow includes demote good jobs
    assert "include_off_profile=true" in body     # verify after installing


def test_importing_server_does_not_open_the_live_database():
    """server.py runs `db = Database()` at module scope.

    Without JOB_TRACKER_DB pointing elsewhere, `pytest` would open -- and
    migrate -- the user's real data/jobs.db. It did, once.
    """
    from pathlib import Path
    from job_tracker_mcp import server
    live = (Path(__file__).resolve().parent.parent / "data" / "jobs.db").resolve()
    assert server.db.path.resolve() != live


def test_triage_prompt_describes_the_board_layouts():
    """Built In inverts the order: company before title. Assume nothing."""
    from job_tracker_mcp.server import triage_job_alerts
    body = triage_job_alerts()
    for board in ("LinkedIn", "Indeed", "Built In"):
        assert board in body
    assert "COMPANY FIRST" in body                  # Built In's inversion
    assert "SEVERAL POSTINGS" in body               # never stop at the first
    assert "never trust the subject line" in body   # Indeed names one of five


def test_triage_prompt_covers_every_sender_and_trap():
    """Nine senders, five layouts, one to skip, and three ways to misread them."""
    from job_tracker_mcp.server import triage_job_alerts
    body = triage_job_alerts()
    for sender in ("LinkedIn Job Alerts", "Job Recommendations",
                   "Indeed", "Built In", "jobs2web", "Oracle"):
        assert sender in body, sender
    assert "COMPANY FIRST" in body                    # Built In's inversion
    assert "STOP AT THE FOOTER" in body               # the signature looks like a title
    assert "intended for Randy Nelson" in body        # names the exact trap line
    assert 'append " (Remote)" to the location' in body
    assert "SEVERAL POSTINGS" in body


def test_triage_prompt_skips_the_salary_trends_newsletter():
    """messages-noreply is not a job alert. It lists companies, not postings."""
    from job_tracker_mcp.server import triage_job_alerts
    body = triage_job_alerts()
    assert "SKIP ENTIRELY" in body
    assert "messages-noreply@linkedin.com" in body
    assert "files companies as jobs" in body


def test_triage_prompt_covers_the_new_senders():
    from job_tracker_mcp.server import triage_job_alerts
    body = triage_job_alerts()
    for s in ("match.indeed.com", "jobalert.indeed.com", "noreply@oracle.com",
              "jobs2web.com", "jobs-noreply"):
        assert s in body, s
    assert "PRECEDES the title" in body        # match.indeed's work-type line
    assert "NO company and NO location" in body  # Oracle's bare title list
    assert "times_seen=2" in body              # honest about the Oracle collapse


def test_triage_prompt_pushes_sorting_and_paging_to_the_server():
    """apple-mail-mcp now sorts and filters by date, so Claude must not.

    sort="oldest" reverses BEFORE the limit, so a limited query returns the
    oldest N. Newest-first would advance the watermark past unread mail.
    """
    from job_tracker_mcp.server import triage_job_alerts
    body = triage_job_alerts(batch_size=20)
    assert 'sort      = "oldest"' in body
    assert "limit     = 20" in body
    assert "date_from" in body
    assert "OMIT it on the first run" in body     # no watermark yet
    assert "INCLUSIVE" in body                    # overlap free, gaps permanent
    assert "run this prompt again" in body        # resumable, no self-looping


def test_review_prompt_confines_claude_to_the_database():
    """The whole point: no web search, no memory, no invented urls or salaries."""
    from job_tracker_mcp.server import review_new_jobs
    body = review_new_jobs()
    assert "USE ONLY THE `job-tracker` MCP TOOLS" in body
    assert "Do not search the web" in body
    assert "NEVER INVENT A URL" in body
    assert "NEVER INVENT A SALARY" in body
    assert "does not mean zero" in body          # NULL salary is not a low salary
    assert "DO NOT CALL mark_applied" in body


def test_review_prompt_is_registered():
    import asyncio
    from job_tracker_mcp import server
    names = {p.name for p in asyncio.run(server.mcp.list_prompts())}
    assert names == {"triage_job_alerts", "setup_job_filters", "review_new_jobs"}
