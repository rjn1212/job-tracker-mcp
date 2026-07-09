"""Pure reconciliation logic. No DB, no HTTP. The important tests."""

import pytest

from job_tracker_mcp.models import canonicalize_company, canonicalize_title
from job_tracker_mcp.onenote.reconcile import (
    DEFAULT_MIN_CONFIDENCE,
    JobRecord,
    LinkRecord,
    PageRecord,
    extract_table_rows,
    match_pages,
    normalize_date,
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
    r = match_pages([page("Resume drafts and interview prep", title="Resume drafts")],
                    [job("a1", "Acme", "Staff Engineer")], [])
    assert len(r.unparsed_pages) == 1
    assert "onenote_get_page" in r.unparsed_pages[0]["proposed_action"]


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


@pytest.mark.parametrize("raw,expected", [
    ("2026-07-02", "2026-07-02"),
    ("7/2/2026", "2026-07-02"),
    ("7/2", "2026-07-02"),
    ("applied 12/25", "2026-12-25"),
    ("no date here", None),
])
def test_normalize_date(raw, expected):
    assert normalize_date(raw, 2026) == expected
