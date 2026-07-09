"""Graph layer, entirely via httpx.MockTransport. Zero network."""

import logging

import httpx
import pytest

from job_tracker_mcp.onenote.client import Ambiguous, NotFound, OneNoteClient
from job_tracker_mcp.onenote.graph import AuthExpired, GraphClient, GraphError

TOKEN = "fake-token-do-not-log"


def client(handler, sleeps=None):
    g = GraphClient(lambda: TOKEN,
                    transport=httpx.MockTransport(handler),
                    sleep=(sleeps.append if sleeps is not None else lambda s: None))
    return g


def json_ok(payload):
    return httpx.Response(200, json=payload)


def nb(id_, name):
    return {"id": id_, "displayName": name}


# ------------------------------------------------------------------ resolve

def test_resolve_section():
    calls = []

    def handler(req):
        calls.append(str(req.url))
        p = req.url.path
        if p.endswith("/notebooks"):
            return json_ok({"value": [nb("nb1", "Randy's Notebook"), nb("nb2", "Other")]})
        if p.endswith("/notebooks/nb1/sections"):
            return json_ok({"value": [nb("s1", "Job Searching"), nb("s2", "AI Article")]})
        if p.endswith("/sectionGroups"):
            return json_ok({"value": []})
        raise AssertionError(f"unexpected {p}")

    c = OneNoteClient(client(handler))
    out = c.resolve_section("Randy's Notebook", "Job Searching")
    assert out["section_id"] == "s1"
    assert out["notebook_id"] == "nb1"


def test_resolve_section_apostrophe_sends_no_filter():
    """The notebook name has an apostrophe; OData would need it doubled."""
    urls = []

    def handler(req):
        urls.append(str(req.url))
        p = req.url.path
        if p.endswith("/notebooks"):
            return json_ok({"value": [nb("nb1", "Randy's  NOTEBOOK")]})
        if p.endswith("/sections"):
            return json_ok({"value": [nb("s1", "job searching")]})
        return json_ok({"value": []})

    c = OneNoteClient(client(handler))
    # Matching is normalized in Python: casefolded, whitespace collapsed.
    assert c.resolve_section("Randy's Notebook", "Job Searching")["section_id"] == "s1"
    assert all("$filter" not in u for u in urls)


def test_resolve_ambiguous_errors():
    def handler(req):
        if req.url.path.endswith("/notebooks"):
            return json_ok({"value": [nb("nb1", "N")]})
        if req.url.path.endswith("/sections"):
            return json_ok({"value": [nb("s1", "Job Searching"), nb("s2", "job  searching")]})
        return json_ok({"value": []})

    with pytest.raises(Ambiguous, match="Ambiguous: 2 sections"):
        OneNoteClient(client(handler)).resolve_section("N", "Job Searching")


def test_resolve_missing_section_lists_alternatives():
    def handler(req):
        if req.url.path.endswith("/notebooks"):
            return json_ok({"value": [nb("nb1", "N")]})
        if req.url.path.endswith("/sections"):
            return json_ok({"value": [nb("s1", "Recipes")]})
        return json_ok({"value": []})

    with pytest.raises(NotFound, match="Recipes"):
        OneNoteClient(client(handler)).resolve_section("N", "Job Searching")


def test_sections_recurse_into_section_groups():
    def handler(req):
        p = req.url.path
        if p.endswith("/notebooks/nb1/sections"):
            return json_ok({"value": []})
        if p.endswith("/notebooks/nb1/sectionGroups"):
            return json_ok({"value": [nb("g1", "Archive")]})
        if p.endswith("/sectionGroups/g1/sections"):
            return json_ok({"value": [nb("s9", "Job Searching")]})
        if p.endswith("/sectionGroups/g1/sectionGroups"):
            return json_ok({"value": []})
        raise AssertionError(p)

    secs = OneNoteClient(client(handler)).sections_of("nb1")
    assert secs[0]["id"] == "s9"
    assert secs[0]["path"] == ["Archive", "Job Searching"]


# ------------------------------------------------------------------- paging

def test_paging_follows_nextlink_verbatim():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if "skip" not in str(req.url):
            return json_ok({"value": [{"id": "1"}],
                            "@odata.nextLink": "https://graph.microsoft.com/v1.0/x?skip=1"})
        return json_ok({"value": [{"id": "2"}]})

    rows = list(client(handler).get_all("/x", {"$top": 100, "$select": "id"}))
    assert [r["id"] for r in rows] == ["1", "2"]
    # Graph embeds the full query in nextLink; re-appending $top causes a 400.
    assert seen[1] == "https://graph.microsoft.com/v1.0/x?skip=1"
    assert "$top" not in seen[1]


def test_list_pages_truncates_and_says_so():
    def handler(req):
        return json_ok({"value": [{"id": f"p{i}", "title": f"T{i}",
                                   "createdDateTime": "c", "lastModifiedDateTime": "m"}
                                  for i in range(10)]})

    pages, truncated = OneNoteClient(client(handler)).list_pages("s1", max_pages=3)
    assert len(pages) == 3 and truncated is True


# ------------------------------------------------------------------ retries

def test_429_honors_retry_after():
    sleeps, calls = [], []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={})
        return json_ok({"ok": True})

    g = client(handler, sleeps)
    assert g.get_json("/x") == {"ok": True}
    assert sleeps == [2.0]              # exactly what Graph asked for, no jitter
    assert g.throttled_retries == 1


def test_429_without_retry_after_backs_off():
    sleeps, calls = [], []

    def handler(req):
        calls.append(1)
        return json_ok({"ok": True}) if len(calls) > 1 else httpx.Response(429, json={})

    g = client(handler, sleeps)
    g.get_json("/x")
    assert sleeps and sleeps[0] == 1.0   # 2**0


def test_401_refreshes_once_then_gives_up():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})

    with pytest.raises(AuthExpired):
        client(handler).get_json("/x")
    assert len(calls) == 2               # exactly two attempts; never loops


def test_401_then_success():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(401, json={})
        return json_ok({"ok": True})

    assert client(handler).get_json("/x") == {"ok": True}


def test_503_retries_then_surfaces_graph_error():
    sleeps = []

    def handler(req):
        return httpx.Response(503, json={"error": {"code": "ServiceUnavailable",
                                                   "message": "try later"}})

    with pytest.raises(GraphError, match="ServiceUnavailable.*try later"):
        client(handler, sleeps).get_json("/x")
    assert len(sleeps) == 4              # 5 attempts, 4 sleeps


def test_404_surfaces_graph_error_body():
    def handler(req):
        return httpx.Response(404, json={"error": {"code": "ItemNotFound",
                                                   "message": "section gone"}})

    with pytest.raises(GraphError, match="ItemNotFound: section gone") as e:
        client(handler).get_json("/me/onenote/sections/s1")
    assert e.value.status == 404


def test_transport_error_retries_then_raises():
    sleeps = []

    def handler(req):
        raise httpx.ConnectError("boom")

    with pytest.raises(GraphError, match="Network error after 5 attempts"):
        client(handler, sleeps).get_json("/x")


# ------------------------------------------------------------------ writing

def test_create_page_posts_to_section_id_never_sectionname():
    captured = {}

    def handler(req):
        captured["url"] = str(req.url)
        captured["ctype"] = req.headers.get("Content-Type")
        captured["body"] = req.content.decode()
        return httpx.Response(201, json={"id": "newpage", "title": "T",
                                         "createdDateTime": "c",
                                         "lastModifiedDateTime": "m",
                                         "links": {"oneNoteWebUrl": {"href": "https://w"}}})

    row = OneNoteClient(client(handler)).create_page(
        "s1", "T", ["line"], marker="a1b2c3d4e5f60718")

    assert captured["url"].endswith("/me/onenote/sections/s1/pages")
    # A typo or rename with ?sectionName= silently creates a SECOND section.
    assert "sectionName" not in captured["url"]
    assert captured["ctype"] == "text/html"
    assert "job-tracker-id: a1b2c3d4e5f60718" in captured["body"]
    assert row["page_id"] == "newpage"
    assert row["web_url"] == "https://w"


def test_page_content_extracts_text():
    def handler(req):
        assert req.url.path.endswith("/pages/p1/content")
        return httpx.Response(200, text="<p>Applied to Acme</p>")

    html, text = OneNoteClient(client(handler)).page_content("p1")
    assert text == "Applied to Acme"


# ------------------------------------------------------------------ secrets

def test_authorization_header_redacted_in_logs(caplog):
    def handler(req):
        assert req.headers["Authorization"] == f"Bearer {TOKEN}"   # really sent
        return json_ok({"ok": True})

    with caplog.at_level(logging.DEBUG, logger="job_tracker_mcp.onenote.graph"):
        client(handler).get_json("/x")

    assert caplog.text                       # we did log the request
    assert TOKEN not in caplog.text          # but never the token
    assert "Bearer <redacted>" in caplog.text


def test_no_network_fixture_actually_blocks():
    """The fixture must catch a real socket, not just httpx."""
    with pytest.raises(AssertionError, match="real network connection"):
        import socket
        socket.create_connection(("example.com", 80))
