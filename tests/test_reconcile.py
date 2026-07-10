"""Pure reconciliation logic. No DB, no HTTP. The important tests."""

import pytest
from zoneinfo import ZoneInfo

from job_tracker_mcp.models import canonicalize_company, canonicalize_title
from job_tracker_mcp.onenote.reconcile import (
    DEFAULT_MIN_CONFIDENCE,
    JobRecord,
    LinkRecord,
    PageRecord,
    extract_table_rows,
    is_anchor_page,
    extract_applied_date,
    graph_date_to_local,
    match_pages,
    normalize_date,
    parse_page_title,
    scope_to_subtree,
)


def job(jid, company, title, status="new", date_applied=None):
    return JobRecord(id=jid, company=company, title=title,
                     company_canonical=canonicalize_company(company),
                     title_canonical=canonicalize_title(title),
                     status=status, date_applied=date_applied)


def table(*rows, header="Company | Title | Date | Status"):
    return "\n".join([header, *rows])


def page(text, pid="p1", title="Log", ignored=False):
    return PageRecord(page_id=pid, title=title, text=text, is_ignored=ignored)


# --------------------------------------------------------- the central pin

def test_location_is_not_part_of_the_key():
    """The regression test for the central design decision.

    The job is stored with location="Remote, US". The page never mentions a
    location. Hashing (company, title, "unknown") would produce an id matching
    nothing, and a reconciler that silently matches nothing reports
    "0 discrepancies" and looks like it works.
    """
    jobs = [job("a1", "Acme Corp", "Staff Engineer")]
    p = page(table("Acme Corp | Staff Engineer | 2026-07-02 | Applied"))
    r = match_pages([p], jobs, [])
    assert len(r.onenote_only) == 1
    assert r.onenote_only[0]["job_id"] == "a1"
    assert r.onenote_only[0]["date_applied"] == "2026-07-02"


# ---------------------------------------------------------------- the ladder

def test_manual_beats_marker():
    """A marker can go stale: copy a page as a template, edit it, the id rides along."""
    jobs = [job("a1", "Acme", "Staff Engineer"), job("b2", "Globex", "SRE")]
    p = page("job-tracker-id: " + "a1".ljust(16, "0"))
    links = [LinkRecord("p1", "b2", "manual", 1.0)]
    r = match_pages([p], jobs, links)
    assert r.onenote_only[0]["job_id"] == "b2"
    assert r.onenote_only[0]["match_kind"] == "manual"


def test_marker_match_wins_over_text():
    jobs = [job("0123456789abcdef", "Acme", "Staff Engineer")]
    p = page("Applied to Globex for Director of Sales\n"
             "job-tracker-id: 0123456789abcdef")
    r = match_pages([p], jobs, [])
    assert r.onenote_only[0]["job_id"] == "0123456789abcdef"
    assert r.onenote_only[0]["match_kind"] == "marker"


def test_manual_link_date_applied_is_used():
    jobs = [job("a1", "Acme", "Staff Engineer")]
    links = [LinkRecord("p1", "a1", "manual", 1.0, date_applied="2026-06-01")]
    r = match_pages([page("free-form prose")], jobs, links)
    assert r.onenote_only[0]["date_applied"] == "2026-06-01"
    assert "2026-06-01" in r.onenote_only[0]["proposed_action"]


def test_exact_match_uses_canonical_company():
    """A page saying "Acme Corp" must match a job stored as "Acme, Inc." at tier 3."""
    jobs = [job("a1", "Acme, Inc.", "Staff Engineer")]
    p = page(table("Acme Corp | Staff Engineer | 2026-07-02 | Applied"))
    r = match_pages([p], jobs, [])
    assert r.onenote_only[0]["match_kind"] == "exact"


def test_exact_match_normalization():
    jobs = [job("a1", "acme corp.", "staff engineer")]
    p = page(table("Acme  Corp. | Staff Engineer | 2026-07-02 | Applied"))
    r = match_pages([p], jobs, [])
    assert r.onenote_only[0]["match_kind"] == "exact"


def test_exact_match_ignores_title_decoration():
    jobs = [job("a1", "Acme", "Staff Engineer")]
    p = page(table("Acme | Staff Engineer (Remote) | 2026-07-02 | Applied"))
    r = match_pages([p], jobs, [])
    assert r.onenote_only[0]["match_kind"] == "exact"


# ------------------------------------------------------------- the metric

@pytest.mark.parametrize("db_title,page_title,should_match", [
    # Under SequenceMatcher the SECOND pair scores HIGHER than the first (0.73 vs
    # 0.76 is not the half of it: "senior staff engineer" scores 0.80). Assert
    # BOTH directions, or the test passes with the wrong metric.
    ("Staff Engineer", "Staff Software Engineer", True),
    ("Backend Engineer", "Frontend Engineer", False),
    ("Data Engineer", "Data Scientist", False),
])
def test_fuzzy_metric_is_word_overlap_not_characters(db_title, page_title, should_match):
    jobs = [job("a1", "Acme", db_title)]
    p = page(table(f"Acme | {page_title} | 2026-07-02 | Applied"))
    r = match_pages([p], jobs, [])
    matched = bool(r.onenote_only) and r.onenote_only[0]["match_kind"] == "fuzzy"
    assert matched is should_match


def test_default_min_confidence_matches_the_true_duplicate():
    """Guards a threshold that silently matches nothing and reports 0 discrepancies."""
    assert DEFAULT_MIN_CONFIDENCE == 0.6
    jobs = [job("a1", "Acme", "Staff Engineer")]
    p = page(table("Acme | Staff Software Engineer | 2026-07-02 | Applied"))
    r = match_pages([p], jobs, [])
    assert len(r.onenote_only) == 1


def test_two_candidates_above_threshold_is_ambiguous():
    """Never picks one. The same company posting two similar roles needs a human."""
    jobs = [job("a1", "Acme", "Staff Engineer II"),
            job("b2", "Acme", "Senior Staff Engineer")]
    p = page(table("Acme | Staff Engineer | 2026-07-02 | Applied"))
    r = match_pages([p], jobs, [])
    assert not r.onenote_only
    assert len(r.ambiguous) == 1
    assert {c["job_id"] for c in r.ambiguous[0]["candidates"]} == {"a1", "b2"}


def test_fuzzy_below_threshold_is_ambiguous_not_matched():
    jobs = [job("a1", "Acme", "Senior Staff Platform Engineer")]
    p = page(table("Acme | Staff Engineer | 2026-07-02 | Applied"))
    r = match_pages([p], jobs, [])
    assert not r.onenote_only
    assert len(r.ambiguous) == 1


def test_different_company_is_unmatched_not_silent():
    """A company the tracker has never seen must be REPORTED, not dropped.

    It parsed cleanly and matched nothing. Reporting nowhere would mean the
    reconciler silently omits a job you applied to.
    """
    jobs = [job("a1", "Globex", "Staff Engineer")]
    p = page(table("Acme | Staff Engineer | 2026-07-02 | Applied"))
    r = match_pages([p], jobs, [])
    assert not r.onenote_only and not r.ambiguous
    assert len(r.unmatched) == 1
    assert r.unmatched[0]["extracted"]["company"] == "Acme"
    assert "add_job" in r.unmatched[0]["proposed_action"]


# -------------------------------------------------------------- partitions

def test_matched_when_both_agree():
    jobs = [job("a1", "Acme", "Staff Engineer", status="applied",
                date_applied="2026-07-02")]
    p = page(table("Acme | Staff Engineer | 2026-07-02 | Applied"))
    r = match_pages([p], jobs, [])
    assert len(r.matched) == 1 and r.matched[0]["agrees"] is True
    assert not r.onenote_only and not r.db_only


def test_db_only_reports_never_acts():
    jobs = [job("a1", "Acme", "Staff Engineer", status="applied",
                date_applied="2026-06-28")]
    r = match_pages([], jobs, [])
    assert len(r.db_only) == 1
    assert "onenote_create_page" in r.db_only[0]["proposed_action"]


def test_unapplied_db_jobs_are_not_db_only():
    r = match_pages([], [job("a1", "Acme", "Staff Engineer", status="new")], [])
    assert not r.db_only


def test_ignored_pages_excluded():
    jobs = [job("a1", "Acme", "Staff Engineer")]
    p = page(table("Acme | Staff Engineer | 2026-07-02 | Applied"), ignored=True)
    r = match_pages([p], jobs, [])
    assert not r.onenote_only and not r.unparsed_pages


def test_unparsed_page_gets_actionable_next_step():
    """A page whose title yields no company at all -- e.g. an anchor page."""
    r = match_pages([page("nothing structured here", title="2026")],
                    [job("a1", "Acme", "Staff Engineer")], [])
    assert len(r.unparsed_pages) == 1
    assert "onenote_get_page" in r.unparsed_pages[0]["proposed_action"]


def test_untracked_company_page_is_unmatched_not_silent():
    r = match_pages([page("free prose", title="Resume drafts")],
                    [job("a1", "Acme", "Staff Engineer")], [])
    assert len(r.unmatched) == 1
    assert r.unmatched[0]["extracted"]["company"] == "Resume drafts"


def test_merged_jobs_are_not_candidates():
    """match_pages is given live jobs only; a merged job cannot be proposed."""
    live = [job("a1", "Acme", "Staff Engineer")]
    p = page(table("Acme | Staff Engineer | 2026-07-02 | Applied"))
    r = match_pages([p], live, [])
    assert r.onenote_only[0]["job_id"] == "a1"
    assert all(e["job_id"] != "b2" for e in r.onenote_only)


# -------------------------------------------------------------- extraction

def test_table_extraction():
    rows = extract_table_rows(
        table("Acme Corp | Staff Engineer | 2026-07-02 | Applied",
              "Globex | SRE | 7/5 | Applied"), 2026)
    assert rows == [
        {"company": "Acme Corp", "title": "Staff Engineer",
         "date_applied": "2026-07-02", "status": "Applied"},
        {"company": "Globex", "title": "SRE",
         "date_applied": "2026-07-05", "status": "Applied"},
    ]


def test_table_extraction_requires_a_header():
    assert extract_table_rows("a | b | c\nd | e | f", 2026) == []


def test_table_extraction_ignores_prose():
    assert extract_table_rows("I applied to Acme on 7/2. It went well.", 2026) == []




# ------------------------------------------------ page-title extraction

@pytest.mark.parametrize("raw,expected", [
    ("Capital One - Senior Lead Software Engineer, Full Stack",
     {"company": "Capital One", "title": "Senior Lead Software Engineer, Full Stack",
      "rejected": False}),
    ("Rula - Engineering Manager - Partnerships",
     {"company": "Rula", "title": "Engineering Manager - Partnerships", "rejected": False}),
    # A leading "x" means Randy was turned down; it is not part of the company name.
    ("x Capital One - Director of Software Engineering",
     {"company": "Capital One", "title": "Director of Software Engineering",
      "rejected": True}),
    ("*X* Capital One - Senior Manager, Software Engineering, Full Stack",
     {"company": "Capital One", "title": "Senior Manager, Software Engineering, Full Stack",
      "rejected": True}),
    # No space after the dash, as typed in the real notebook.
    ("Mitr -Global IT Consulting Org",
     {"company": "Mitr", "title": "Global IT Consulting Org", "rejected": False}),
    # Company-only pages: guessing a role here would be fabrication.
    ("Deloitte", {"company": "Deloitte", "title": None, "rejected": False}),
    ("Turnberry Solutions",
     {"company": "Turnberry Solutions", "title": None, "rejected": False}),
    ("Chase Manager of Software Engineering",
     {"company": "Chase Manager of Software Engineering", "title": None, "rejected": False}),
    # A hyphen with no surrounding space is part of the name, not a separator.
    ("Coca-Cola", {"company": "Coca-Cola", "title": None, "rejected": False}),
    ("84.51 degrees - VP, Software Engineering",
     {"company": "84.51 degrees", "title": "VP, Software Engineering", "rejected": False}),
    ("2026", None),
    ("", None),
])
def test_parse_page_title(raw, expected):
    assert parse_page_title(raw) == expected


def test_rejected_page_proposes_reject_not_applied():
    """An "x" page means turned down. Proposing mark_applied would be backwards."""
    jobs = [job("a1", "Capital One", "Director of Software Engineering")]
    p = PageRecord(page_id="p1", title="x Capital One - Director of Software Engineering",
                   text="Applied: 06/19/2026", page_order=1)
    r = match_pages([p], jobs, [])
    e = r.onenote_only[0]
    assert e["rejected"] is True
    assert e["proposed_action"].startswith("reject_job")


# ------------------------------------------------------- date extraction

@pytest.mark.parametrize("raw,expected", [
    ("2026-07-02", "2026-07-02"),
    ("7/2/2026", "2026-07-02"),
    ("06/19/2026", "2026-06-19"),
    ("07-09-2026", "2026-07-09"),        # Turnberry uses dashes
    ("15-Jun-2026", "2026-06-15"),       # Zoro's body
    ("15-June-2026", "2026-06-15"),
    ("7/2", "2026-07-02"),
    ("no date here", None),
    ("99-99-2026", None),
])
def test_normalize_date(raw, expected):
    assert normalize_date(raw, 2026) == expected


def test_applied_label_beats_date_label():
    text = "Date: 01-01-2026\nApplied: 06/19/2026"
    assert extract_applied_date(text, 2026) == ("2026-06-19", "applied")


def test_date_label_used_when_no_applied_label():
    assert extract_applied_date("Date:07-09-2026", 2026) == ("2026-07-09", "date")


def test_candidates_does_not_masquerade_as_a_date_label():
    """Without \\b, `date\\s*:` matches the tail of "candidates:" and eats garbage."""
    text = "The ideal candidates: must possess 3 years of 10/10/2020 experience"
    assert extract_applied_date(text, 2026) == (None, None)


def test_bare_date_on_first_line_is_used():
    """The Zoro page: a date alone on line 1, then the role. No label."""
    jobs = [job("a1", "Zoro", "Sr. Manager, Software Engineering")]
    p = PageRecord(page_id="p1", title="Zoro - Sr. Manager, Software Engineering",
                   text="15-Jun-2026\nSr. Manager, Software Engineering",
                   created_dt="2026-06-16T00:00:00Z", page_order=1)
    r = match_pages([p], jobs, [])
    e = r.onenote_only[0]
    assert e["date_applied"] == "2026-06-15"      # the page, not its creation date
    assert e["date_source"] == "first_line"


@pytest.mark.parametrize("first_line", [
    "R243298",                     # Capital One: a req id, not a date
    "Rob Bishop",                  # Mitr: a recruiter's name
    "Applied on 15-Jun-2026",      # prose around a date is not a bare date line
    "Salary 100000-200000-2026",
])
def test_first_line_must_be_nothing_but_a_date(first_line):
    assert extract_applied_date(f"{first_line}\nmore text", 2026) == (None, None)


def test_no_date_anywhere_falls_back_to_page_creation():
    """Capital One's page has a req id where a date would be, and no date at all."""
    created = "2026-07-08T19:58:08.55Z"
    jobs = [job("a1", "Capital One", "Senior Lead Software Engineer")]
    p = PageRecord(page_id="p1", title="Capital One - Senior Lead Software Engineer",
                   text="R243298\nSenior Lead Software Engineer, Full Stack",
                   created_dt=created, page_order=1)
    r = match_pages([p], jobs, [])
    assert r.onenote_only[0]["date_source"] == "page_created"
    # The LOCAL day, whatever zone this machine runs in -- never the naive iso[:10].
    assert r.onenote_only[0]["date_applied"] == graph_date_to_local(created)


def test_date_source_is_reported():
    jobs = [job("a1", "Acme", "Staff Engineer")]
    p = PageRecord(page_id="p1", title="Acme - Staff Engineer",
                   text="Applied: 06/19/2026", created_dt="2026-07-01T00:00:00Z",
                   page_order=1)
    r = match_pages([p], jobs, [])
    assert r.onenote_only[0]["date_applied"] == "2026-06-19"
    assert r.onenote_only[0]["date_source"] == "applied"


def test_page_title_match_uses_created_date():
    jobs = [job("a1", "Capital One", "Senior Lead Software Engineer, Full Stack")]
    p = PageRecord(page_id="p1", title="Capital One - Senior Lead Software Engineer, Full Stack",
                   text="", created_dt="2026-07-08T13:04:00Z", page_order=1)
    r = match_pages([p], jobs, [])
    assert r.onenote_only[0]["match_kind"] == "exact"
    assert r.onenote_only[0]["date_applied"] == "2026-07-08"


def test_company_only_page_resolves_when_unambiguous():
    jobs = [job("a1", "Deloitte", "Engineering Manager")]
    p = PageRecord(page_id="p1", title="Deloitte", text="",
                   created_dt="2026-06-19T00:00:00Z", page_order=1)
    r = match_pages([p], jobs, [])
    assert r.onenote_only[0]["job_id"] == "a1"


def test_company_only_page_is_ambiguous_when_several_jobs():
    """Never guess which of two Deloitte roles a bare "Deloitte" page means."""
    jobs = [job("a1", "Deloitte", "Engineering Manager"),
            job("b2", "Deloitte", "Director of Data")]
    p = PageRecord(page_id="p1", title="Deloitte", text="", page_order=1)
    r = match_pages([p], jobs, [])
    assert not r.onenote_only
    assert {c["job_id"] for c in r.ambiguous[0]["candidates"]} == {"a1", "b2"}


# ------------------------------------------------------- subtree scoping

def _p(pid, title, order):
    return PageRecord(page_id=pid, title=title, text="", page_order=order)


REAL_SHAPE = [
    _p("prep", "Interview Questions", 0),
    _p("y23", "2023", 1),
    _p("dtn", "DTN", 2),
    _p("y25", "2025", 3),
    _p("old1", "Capital One - Director", 4),
    _p("y26", "2026", 5),
    _p("new1", "Zoro", 6),
    _p("new2", "Deloitte", 7),
]


def test_scope_to_subtree_takes_children_only():
    got = [p.page_id for p in scope_to_subtree(REAL_SHAPE, "2026")]
    assert got == ["new1", "new2"]


def test_scope_stops_at_the_next_anchor():
    got = [p.page_id for p in scope_to_subtree(REAL_SHAPE, "2023")]
    assert got == ["dtn"]                       # stops at 2025, excludes 2023 itself


def test_scope_unknown_root_yields_nothing_not_everything():
    """Silently reconciling the whole section would be worse than reconciling none."""
    assert scope_to_subtree(REAL_SHAPE, "2099") == []


def test_scope_ignores_pages_without_order():
    pages = [*REAL_SHAPE, PageRecord(page_id="x", title="Stray", text="")]
    assert "x" not in [p.page_id for p in scope_to_subtree(pages, "2026")]


@pytest.mark.parametrize("t,expected", [("2026", True), ("2025 - Jun", False),
                                        ("Deloitte", False), (" 2023 ", True)])
def test_is_anchor_page(t, expected):
    assert is_anchor_page(t) is expected


# ------------------------------------------------- UTC vs local calendar date

CHICAGO = ZoneInfo("America/Chicago")


@pytest.mark.parametrize("iso,expected", [
    # Zoro: 02:29 UTC is 21:29 the previous evening in Central.
    ("2026-06-16T02:29:29.801Z", "2026-06-15"),
    ("2026-07-05T04:24:10.285Z", "2026-07-04"),
    # Capital One: 19:58 UTC is 14:58 the same day.
    ("2026-07-08T19:58:08.55Z", "2026-07-08"),
    ("2026-01-23T02:32:46.937Z", "2026-01-22"),   # across a CST boundary too
    ("2026-06-16T00:00:00Z", "2026-06-15"),       # exactly midnight UTC
])
def test_graph_date_to_local(iso, expected):
    assert graph_date_to_local(iso, CHICAGO) == expected


def test_graph_date_to_local_handles_none_and_garbage():
    assert graph_date_to_local(None, CHICAGO) is None
    assert graph_date_to_local("", CHICAGO) is None
    # Unparseable: degrade to the leading slice rather than lose the field entirely.
    assert graph_date_to_local("not-a-date", CHICAGO) == "not-a-date"


def test_evening_page_is_dated_the_previous_local_day():
    """2026-06-16T02:29Z is 9:29pm on June 15 in Central. Zoro's real case."""
    assert graph_date_to_local("2026-06-16T02:29:29.801Z", CHICAGO) == "2026-06-15"
    assert "2026-06-16T02:29:29.801Z"[:10] == "2026-06-16"   # what the bug produced
