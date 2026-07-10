"""Sync phases, write gating, and reconcile wired to a real (temp) database."""

import httpx
import pytest

from job_tracker_mcp.onenote.graph import GraphClient
from job_tracker_mcp.onenote.service import OneNoteService

PAGE_HTML = "<p>Applied to Acme on 7/2</p>"
TABLE_HTML = (
    "<table><tr><th>Company</th><th>Title</th><th>Date</th><th>Status</th></tr>"
    "<tr><td>Acme Corp</td><td>Staff Engineer</td><td>2026-07-02</td><td>Applied</td></tr>"
    "</table>"
)


def graph_for(handler, sleeps=None):
    return GraphClient(lambda: "tok", transport=httpx.MockTransport(handler),
                       sleep=(sleeps.append if sleeps is not None else lambda s: None))


def resolving_handler(pages, content=PAGE_HTML, on_request=None):
    def handler(req):
        if on_request:
            on_request(req)
        p = req.url.path
        if p.endswith("/notebooks"):
            return httpx.Response(200, json={"value": [{"id": "nb1",
                                                        "displayName": "Randy's Notebook"}]})
        if p.endswith("/notebooks/nb1/sections"):
            return httpx.Response(200, json={"value": [{"id": "s1",
                                                        "displayName": "Job Searching"}]})
        if p.endswith("/sectionGroups"):
            return httpx.Response(200, json={"value": []})
        if p == "/v1.0/me/onenote/sections/s1":
            return httpx.Response(200, json={"id": "s1", "displayName": "Job Searching"})
        if p.endswith("/sections/s1/pages"):
            return httpx.Response(200, json={"value": pages})
        if "/content" in p:
            return httpx.Response(200, text=content)
        raise AssertionError(f"unexpected {p}")
    return handler


def gpage(pid, modified="2026-07-01T00:00:00Z", title="Log"):
    return {"id": pid, "title": title, "createdDateTime": "2026-07-01T00:00:00Z",
            "lastModifiedDateTime": modified, "links": {}}


@pytest.fixture
def svc(db, monkeypatch):
    monkeypatch.setenv("JOB_TRACKER_MS_CLIENT_ID", "fake-guid")
    # Empty root page == reconcile the whole section. The subtree scoping is
    # exercised separately, in test_reconcile.py and test_scoping below.
    monkeypatch.setenv("JOB_TRACKER_ONENOTE_ROOT_PAGE", "")
    return lambda handler, **kw: OneNoteService(db, graph_for(handler, **kw))


# ------------------------------------------------------------------- sync

def test_sync_fetches_then_caches(db, svc):
    s = svc(resolving_handler([gpage("p1")]))
    out = s.sync()
    assert out["pages_new"] == 1 and out["content_fetched"] == 1
    assert s.store.get_page_row("p1")["content_text"] == "Applied to Acme on 7/2"

    # Second sync: nothing changed upstream, so no content call is made.
    fetched = []
    s2 = svc(resolving_handler([gpage("p1")],
                               on_request=lambda r: fetched.append(str(r.url))))
    out = s2.sync()
    assert out["content_fetched"] == 0 and out["content_skipped_cached"] == 1
    assert not any("/content" in u for u in fetched)


def test_sync_refetches_when_page_changed(db, svc):
    svc(resolving_handler([gpage("p1")])).sync()
    out = svc(resolving_handler([gpage("p1", modified="2026-07-09T00:00:00Z")])).sync()
    assert out["pages_updated"] == 1 and out["content_fetched"] == 1


def test_sync_full_forces_refetch(db, svc):
    svc(resolving_handler([gpage("p1")])).sync()
    out = svc(resolving_handler([gpage("p1")])).sync(full=True)
    assert out["content_fetched"] == 1


def test_sync_metadata_only(db, svc):
    out = svc(resolving_handler([gpage("p1")])).sync(fetch_content=False)
    assert out["content_fetched"] == 0
    assert s_row(db, "p1")["content_text"] is None


def s_row(db, pid):
    return db.conn.execute("SELECT * FROM onenote_pages WHERE page_id = ?", (pid,)).fetchone()


def test_sync_tombstones_vanished_pages(db, svc):
    svc(resolving_handler([gpage("p1"), gpage("p2")])).sync()
    out = svc(resolving_handler([gpage("p1")])).sync()
    assert out["pages_tombstoned"] == 1
    assert s_row(db, "p2")["deleted_at"] is not None      # never DELETEd


def test_sync_truncation_is_announced(db, svc):
    pages = [gpage(f"p{i}") for i in range(5)]
    out = svc(resolving_handler(pages)).sync(max_pages=2)
    assert out["truncated"] is True
    assert "TRUNCATED" in out["message"]                  # never a silent success


def test_sync_holds_no_db_lock_during_http(db, svc):
    """The lock must be free while a request is in flight.

    Otherwise a 40-page sync freezes every other tool in the server for the
    length of 40 network round-trips.
    """
    observed = []

    def probe(req):
        acquired = db._lock.acquire(blocking=False)
        observed.append(acquired)
        if acquired:
            db._lock.release()

    svc(resolving_handler([gpage("p1"), gpage("p2")], on_request=probe)).sync()
    assert observed, "no HTTP requests were made"
    assert all(observed), "the database lock was held across an HTTP call"


def test_sync_renamed_section_reports_rather_than_following(db, svc):
    svc(resolving_handler([gpage("p1")])).sync()

    def renamed(req):
        if req.url.path == "/v1.0/me/onenote/sections/s1":
            return httpx.Response(200, json={"id": "s1", "displayName": "Archive 2026"})
        return resolving_handler([gpage("p1")])(req)

    from job_tracker_mcp.onenote.graph import GraphError
    with pytest.raises(GraphError, match="renamed"):
        svc(renamed).sync()


# ------------------------------------------------------------- create_page

def test_create_page_preview_makes_no_request(db, svc, monkeypatch):
    monkeypatch.setenv("JOB_TRACKER_ONENOTE_WRITE", "1")
    calls = []
    s = svc(resolving_handler([], on_request=lambda r: calls.append(r)))
    out = s.create_page("T", ["body"], confirm=False)
    assert out["result"] == "preview"
    assert "<title>T</title>" in out["html"]
    assert calls == []                                    # dry run touches nothing


def test_create_page_write_disabled_makes_no_request(db, svc, monkeypatch):
    monkeypatch.delenv("JOB_TRACKER_ONENOTE_WRITE", raising=False)
    calls = []
    s = svc(resolving_handler([], on_request=lambda r: calls.append(r)))
    assert "JOB_TRACKER_ONENOTE_WRITE" in s.create_page("T", ["b"], confirm=True)["error"]
    assert calls == []


def test_create_page_is_idempotent(db, svc, monkeypatch):
    monkeypatch.setenv("JOB_TRACKER_ONENOTE_WRITE", "1")
    job_id = db.add_job(company="Acme", title="Staff Engineer", location="Remote")["id"]

    posts = []

    def handler(req):
        if req.method == "POST":
            posts.append(str(req.url))
            return httpx.Response(201, json={"id": "np", "title": "T",
                                             "createdDateTime": "c",
                                             "lastModifiedDateTime": "m", "links": {}})
        return resolving_handler([])(req)

    first = svc(handler).create_page("T", ["b"], job_id=job_id, confirm=True)
    assert first["result"] == "created" and len(posts) == 1

    second = svc(handler).create_page("T", ["b"], job_id=job_id, confirm=True)
    assert second["result"] == "exists"
    assert len(posts) == 1                                # no second page


def test_create_page_rejects_merged_job(db, svc, monkeypatch):
    monkeypatch.setenv("JOB_TRACKER_ONENOTE_WRITE", "1")
    a = db.add_job(company="Acme", title="Staff Engineer", location="Remote")["id"]
    b = db.add_job(company="Acme", title="Staff Software Engineer", location="Remote")["id"]
    db.merge_jobs(a, b)
    out = svc(resolving_handler([])).create_page("T", ["x"], job_id=b, confirm=True)
    assert a in out["error"]


# ------------------------------------------------------------- reconcile

def test_reconcile_end_to_end(db, svc):
    db.add_job(company="Acme Corp", title="Staff Engineer", location="Remote, US")
    svc(resolving_handler([gpage("p1")], content=TABLE_HTML)).sync()

    s = svc(resolving_handler([gpage("p1")], content=TABLE_HTML))
    out = s.reconcile()
    assert out["dry_run"] is True
    assert len(out["onenote_only"]) == 1
    assert out["onenote_only"][0]["date_applied"] == "2026-07-02"
    assert db.get_jobs()["jobs"][0]["status"] == "new"      # dry run mutated nothing

    out = s.reconcile(dry_run=False, apply_marks=True)
    assert len(out["applied"]) == 1
    job = db.get_jobs()["jobs"][0]
    assert job["status"] == "applied" and job["date_applied"] == "2026-07-02"
    assert "Reconciled from OneNote" in job["notes"]


def test_reconcile_respects_transitions(db, svc):
    """A rejected job proposed for mark_applied lands in `skipped`, DB unchanged."""
    jid = db.add_job(company="Acme Corp", title="Staff Engineer", location="Remote")["id"]
    db.reject_job(jid)
    svc(resolving_handler([gpage("p1")], content=TABLE_HTML)).sync()

    out = svc(resolving_handler([gpage("p1")], content=TABLE_HTML)).reconcile(
        dry_run=False, apply_marks=True)
    assert not out["applied"] and len(out["skipped"]) == 1
    assert "Invalid status transition" in out["skipped"][0]["reason"]
    assert db.get_job(jid)["status"] == "rejected"


def test_reconcile_persists_links_once(db, svc):
    db.add_job(company="Acme Corp", title="Staff Engineer", location="Remote")
    s = svc(resolving_handler([gpage("p1")], content=TABLE_HTML))
    s.sync()
    s.reconcile()
    s.reconcile()
    assert len(s.store.links()) == 1                       # ON CONFLICT DO NOTHING


def test_reconcile_ignores_ignored_pages(db, svc):
    db.add_job(company="Acme Corp", title="Staff Engineer", location="Remote")
    s = svc(resolving_handler([gpage("p1")], content=TABLE_HTML))
    s.sync()
    s.ignore_page("p1")
    assert s.reconcile()["onenote_only"] == []


def test_reconcile_excludes_merged_jobs(db, svc):
    a = db.add_job(company="Acme Corp", title="Staff Engineer", location="Remote")["id"]
    b = db.add_job(company="Acme Corp", title="Staff Software Engineer", location="R2")["id"]
    db.merge_jobs(a, b)
    s = svc(resolving_handler([gpage("p1")], content=TABLE_HTML))
    s.sync()
    out = s.reconcile()
    assert all(e["job_id"] != b for e in out["onenote_only"])


def test_link_page_requires_valid_date(db, svc):
    db.add_job(company="Acme", title="Staff Engineer", location="Remote")
    jid = db.get_jobs()["jobs"][0]["id"]
    s = svc(resolving_handler([gpage("p1")]))
    s.sync()
    assert "error" in s.link_page("p1", jid, date_applied="07/02/2026")
    assert s.link_page("p1", jid, date_applied="2026-07-02")["result"] == "linked"


def test_unlink_refused_for_tool_created_pages(db, svc, monkeypatch):
    monkeypatch.setenv("JOB_TRACKER_ONENOTE_WRITE", "1")
    jid = db.add_job(company="Acme", title="Staff Engineer", location="Remote")["id"]

    def handler(req):
        if req.method == "POST":
            return httpx.Response(201, json={"id": "np", "title": "T",
                                             "createdDateTime": "c",
                                             "lastModifiedDateTime": "m", "links": {}})
        return resolving_handler([])(req)

    svc(handler).create_page("T", ["b"], job_id=jid, confirm=True)
    out = svc(handler).link_page("np", jid, unlink=True)
    assert "Cannot unlink" in out["error"]


# --------------------------------------------------------- subtree scoping

def test_reconcile_scopes_to_root_page(db, svc, monkeypatch):
    """Only the pages indented under the root page are applications.

    The section also holds interview prep and previous years' applications.
    """
    monkeypatch.setenv("JOB_TRACKER_ONENOTE_ROOT_PAGE", "2026")
    db.add_job(company="Acme Corp", title="Staff Engineer", location="Remote")
    pages = [gpage("prep", title="Interview Questions"),
             gpage("y25", title="2025"),
             gpage("old", title="Acme Corp - Staff Engineer"),   # 2025's child
             gpage("y26", title="2026"),
             gpage("new", title="Acme Corp - Staff Engineer")]   # 2026's child
    s = svc(resolving_handler(pages, content="<p>notes</p>"))
    s.sync()
    out = s.reconcile()
    assert out["scope"] == {"root_page": "2026", "pages_in_scope": 1}
    assert [e["page_id"] for e in out["onenote_only"]] == ["new"]


def test_reconcile_unknown_root_page_scopes_to_nothing(db, svc, monkeypatch):
    """Silently reconciling the whole section would be worse than reconciling none."""
    monkeypatch.setenv("JOB_TRACKER_ONENOTE_ROOT_PAGE", "2099")
    db.add_job(company="Acme Corp", title="Staff Engineer", location="Remote")
    s = svc(resolving_handler([gpage("p1", title="Acme Corp - Staff Engineer")]))
    s.sync()
    out = s.reconcile()
    assert out["scope"]["pages_in_scope"] == 0
    assert out["onenote_only"] == []


def test_page_order_is_persisted(db, svc):
    s = svc(resolving_handler([gpage("a"), gpage("b"), gpage("c")]))
    s.sync()
    orders = {r["page_id"]: r["page_order"]
              for r in db.conn.execute("SELECT page_id, page_order FROM onenote_pages")}
    assert orders == {"a": 0, "b": 1, "c": 2}
