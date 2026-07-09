from job_tracker_mcp.onenote.htmlio import (
    build_page_html,
    find_marker,
    html_to_text,
)

REAL_SHAPE = """<!DOCTYPE html><html><head><title>Applications</title>
<meta name="created" content="2026-07-01T14:02:11.0000000"/>
<style>body{font-family:Calibri}</style></head>
<body data-absolute-enabled="true" style="font-family:Calibri;font-size:11pt">
<div style="position:absolute;left:48px;top:120px;width:624px">
<p style="margin-top:0pt">Applied to Acme &amp; Co on 7/2</p>
<p>Recruiter: Dana</p>
</div></body></html>"""


def test_html_to_text_strips_positioning():
    out = html_to_text(REAL_SHAPE)
    assert out.splitlines() == ["Applied to Acme & Co on 7/2", "Recruiter: Dana"]
    assert "position:absolute" not in out
    assert "Calibri" not in out          # <style> dropped entirely


def test_html_to_text_drops_head_and_title():
    assert "Applications" not in html_to_text(REAL_SHAPE)


def test_html_to_text_entities():
    assert html_to_text("<p>R&amp;D &lt;Engineer&gt;</p>") == "R&D <Engineer>"


def test_html_to_text_table_to_pipes():
    doc = ("<table><tr><th>Company</th><th>Title</th><th>Date</th></tr>"
           "<tr><td>Acme Corp</td><td>Staff Engineer</td><td>2026-07-02</td></tr></table>")
    assert html_to_text(doc).splitlines() == [
        "Company | Title | Date",
        "Acme Corp | Staff Engineer | 2026-07-02",
    ]


def test_html_to_text_list_items():
    assert html_to_text("<ul><li>one</li><li>two</li></ul>").splitlines() == ["- one", "- two"]


def test_html_to_text_placeholders_images():
    doc = '<p>before</p><img src="https://graph.microsoft.com/v1.0/secret/resource" /><p>after</p>'
    out = html_to_text(doc)
    assert "[image]" in out
    # The src is an authenticated Graph URL. It is never fetched and never surfaces.
    assert "graph.microsoft.com" not in out
    assert "secret" not in out


def test_html_to_text_placeholders_attachments():
    doc = '<object data-attachment="Resume.pdf" data="https://x/y"></object>'
    out = html_to_text(doc)
    assert out == "[attachment: Resume.pdf]"
    assert "https://x/y" not in out


def test_html_to_text_collapses_blank_runs():
    assert html_to_text("<p>a</p><br/><br/><br/><br/><p>b</p>") == "a\nb"


def test_html_to_text_empty():
    assert html_to_text("") == ""


# ------------------------------------------------------------------- writing

def test_build_page_html_title_is_in_head():
    """Graph reads the page title from <title>, not from any POST parameter."""
    doc = build_page_html("My Page", ["hi"])
    assert "<title>My Page</title>" in doc
    assert doc.lstrip().startswith("<!DOCTYPE html>")


def test_build_page_html_escapes():
    doc = build_page_html("R&D <Engineer>", ['<script>alert("x")</script>'])
    assert "<title>R&amp;D &lt;Engineer&gt;</title>" in doc
    assert "<script>" not in doc
    assert "&lt;script&gt;" in doc


def test_build_page_html_escapes_quotes_in_meta():
    doc = build_page_html("t", [], created='2026-07-09T16:29:04-07:00')
    assert '<meta name="created" content="2026-07-09T16:29:04-07:00" />' in doc


def test_build_page_html_marker_present():
    doc = build_page_html("t", ["body"], marker="a1b2c3d4e5f60718")
    assert "job-tracker-id: a1b2c3d4e5f60718" in doc
    assert "<meta name=\"job-tracker-id\"" not in doc   # must be body text, not meta


def test_build_page_html_no_marker_when_unlinked():
    assert "job-tracker-id" not in build_page_html("t", ["body"])


def test_marker_roundtrip():
    """The whole idempotency guarantee: the marker must survive extraction."""
    doc = build_page_html("Applied: Staff Eng", ["Applied via site"],
                          marker="a1b2c3d4e5f60718")
    assert find_marker(html_to_text(doc)) == "a1b2c3d4e5f60718"


def test_find_marker_absent():
    assert find_marker("no marker here") is None
    assert find_marker("job-tracker-id: nothex") is None
    assert find_marker("") is None


def test_find_marker_in_realistic_page():
    text = html_to_text(
        "<p>Applied 7/2</p><p><i>job-tracker-id: 0123456789abcdef</i></p>")
    assert find_marker(text) == "0123456789abcdef"
