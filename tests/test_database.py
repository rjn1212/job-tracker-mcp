import pytest

from job_tracker_mcp.models import (
    canonicalize_company,
    canonicalize_location,
    canonicalize_title,
    generate_job_id,
)


def add(db, **kw):
    kw.setdefault("company", "Acme Corp")
    kw.setdefault("title", "Staff Engineer")
    kw.setdefault("location", "Remote")
    return db.add_job(**kw)


# ---------------------------------------------------------------- add / dedup

def test_add_job_new(db):
    r = add(db)
    assert r["result"] == "created"
    assert r["status"] == "new"
    assert r["auto_rejected"] is False
    assert db.get_job(r["id"])["times_seen"] == 1


def test_add_job_duplicate(db):
    first = add(db)
    second = add(db)
    assert second["result"] == "duplicate"
    assert second["times_seen"] == 2
    job = db.get_job(first["id"])
    assert job["times_seen"] == 2
    assert job["date_seen"] == job["last_seen"]
    assert db.get_stats()["total_jobs"] == 1


def test_add_job_duplicate_preserves_triage(db):
    r = add(db, fit_score=9, notes="great fit")
    add(db, fit_score=2, notes="clobbered?")
    job = db.get_job(r["id"])
    assert job["fit_score"] == 9
    assert job["notes"] == "great fit"


def test_add_job_duplicate_backfills_url(db):
    r = add(db)
    add(db, url="https://example.com/job", source="LinkedIn")
    job = db.get_job(r["id"])
    assert job["url"] == "https://example.com/job"
    assert job["source"] == "LinkedIn"


def test_add_job_duplicate_does_not_overwrite_url(db):
    r = add(db, url="https://first.example")
    add(db, url="https://second.example")
    assert db.get_job(r["id"])["url"] == "https://first.example"


def test_add_job_requires_company_and_title(db):
    assert "error" in db.add_job(company="  ", title="T", location="Remote")
    assert "error" in db.add_job(company="C", title="", location="Remote")


# ------------------------------------------------------------- natural key

def test_id_normalization(db):
    a = add(db, company="Acme  Corp", title="Staff Engineer ", location="REMOTE")
    b = add(db, company="acme corp", title="staff engineer", location="remote")
    assert a["id"] == b["id"]
    assert b["result"] == "duplicate"
    assert db.get_stats()["total_jobs"] == 1


def test_id_handles_none_location():
    assert generate_job_id("A", "B", None)  # must not raise
    assert canonicalize_location(None) == "unknown"


@pytest.mark.parametrize("raw,expected", [
    ("Remote", "remote"),
    ("Remote, US", "remote"),
    ("United States (Remote)", "remote"),
    ("100% Remote", "remote"),
    ("Fully Remote", "remote"),
    ("WFH", "remote"),
    ("Work From Home", "remote"),
    ("Remote, Anywhere", "remote"),
    ("Remote in the US", "remote"),
    ("Remote - EMEA", "remote emea"),
    ("Hybrid - Remote 2 days", "hybrid remote 2 days"),
    ("Remote in Austin, TX", "remote in austin tx"),
    ("Austin, TX, USA", "austin tx"),
    ("Austin, TX", "austin tx"),
    ("", "unknown"),
    ("Unknown", "unknown"),
    ("N/A", "unknown"),
])
def test_canonicalize_location(raw, expected):
    assert canonicalize_location(raw) == expected


def test_remote_variants_dedupe(db):
    spellings = ["Remote", "Remote, US", "United States (Remote)", "100% Remote", "WFH"]
    ids = {add(db, location=loc)["id"] for loc in spellings}
    assert len(ids) == 1
    job = db.get_job(ids.pop())
    assert job["times_seen"] == 5
    assert job["location"] == "Remote"  # first spelling wins
    assert job["location_canonical"] == "remote"
    assert db.get_stats()["total_jobs"] == 1


def test_restricted_remote_is_distinct(db):
    add(db, location="Remote, US")
    add(db, location="Remote - EMEA")
    assert db.get_stats()["total_jobs"] == 2


def test_hybrid_is_not_remote(db):
    add(db, location="Remote")
    add(db, location="Hybrid - Remote 2 days")
    assert db.get_stats()["total_jobs"] == 2


def test_country_suffix_dropped(db):
    add(db, location="Austin, TX, USA")
    add(db, location="Austin, TX")
    assert db.get_stats()["total_jobs"] == 1
    # Documented limitation, pinned so that changing it is deliberate.
    add(db, location="Austin, Texas")
    assert db.get_stats()["total_jobs"] == 2


def test_location_canonical_stored(db):
    r = add(db, location="United States (Remote)")
    job = db.get_job(r["id"])
    assert job["location"] == "United States (Remote)"
    assert job["location_canonical"] == "remote"


# ------------------------------------------------------------------- applied

def test_mark_applied(db):
    r = add(db)
    assert db.mark_applied(r["id"])["result"] == "updated"
    job = db.get_job(r["id"])
    assert job["status"] == "applied"
    assert job["date_applied"] is not None


def test_mark_applied_appends_notes(db):
    r = add(db, notes="first")
    db.mark_applied(r["id"], notes="second")
    assert db.get_job(r["id"])["notes"] == "first\nsecond"


def test_mark_applied_rejects_rejected_job(db):
    r = add(db)
    db.reject_job(r["id"])
    assert "error" in db.mark_applied(r["id"])
    assert db.get_job(r["id"])["status"] == "rejected"


def test_mark_applied_missing_id(db):
    assert "error" in db.mark_applied("nope")


def test_mark_applied_bad_date(db):
    r = add(db)
    assert "error" in db.mark_applied(r["id"], date_applied="07/09/2026")


# ------------------------------------------------------------------ patterns

def test_reject_job_with_company_pattern(db):
    r = add(db, company="Infosys BPM Ltd", title="Java Developer")
    out = db.reject_job(r["id"], reason="body shop", add_company_pattern=True)
    # The pattern is the canonical company: the legal suffix is already gone.
    assert out["patterns_added"] == ["company:infosys bpm"]

    # Substring: "infosys bpm" does not match the distinct entity "Infosys Ltd".
    db.add_rejected_pattern("company", "infosys")
    second = add(db, company="Infosys Limited", title="Python Developer")
    assert second["auto_rejected"] is True
    assert second["status"] == "rejected"
    assert "company:infosys" in second["matched_patterns"]
    assert "Auto-rejected" in db.get_job(second["id"])["notes"]


def test_company_pattern_survives_suffix_variation(db):
    """A pattern learned from "Acme, Inc." must also filter "Acme Corporation"."""
    db.add_rejected_pattern("company", canonicalize_company("Acme, Inc."))
    r = add(db, company="Acme Corporation", title="Java Developer")
    assert r["auto_rejected"] is True


def test_reject_job_with_keyword_pattern(db):
    r = add(db, title="Java Developer")
    db.reject_job(r["id"], add_keyword_pattern="staffing")
    second = add(db, company="Other Co", title="Staffing Coordinator")
    assert second["auto_rejected"] is True
    assert second["matched_patterns"] == ["title_keyword:staffing"]


def test_reject_job_with_source_pattern(db):
    r = add(db, source="Dice")
    db.reject_job(r["id"], add_source_pattern=True)
    assert db.get_rejected_patterns()["patterns"][0]["pattern_value"] == "dice"

    # Exact match: "Dice Inc" is a different source and must not be filtered.
    inc = add(db, company="A", title="B", source="Dice Inc")
    assert inc["auto_rejected"] is False
    exact = add(db, company="C", title="D", source="DICE")
    assert exact["auto_rejected"] is True


def test_source_pattern_without_source_warns(db):
    r = add(db)
    out = db.reject_job(r["id"], add_source_pattern=True)
    assert out["patterns_added"] == []
    assert "no source" in out["message"]


def test_pattern_matching_is_case_insensitive(db):
    db.add_rejected_pattern("company", "INFOSYS")
    r = add(db, company="Infosys BPM Ltd")
    assert r["auto_rejected"] is True


def test_duplicate_pattern_is_not_an_error(db):
    first = db.add_rejected_pattern("company", "wipro")
    second = db.add_rejected_pattern("company", "Wipro ")
    assert first["result"] == "added"
    assert second["result"] == "exists"
    assert second["id"] == first["id"]
    assert db.get_rejected_patterns()["total"] == 1


def test_add_pattern_validates(db):
    assert "error" in db.add_rejected_pattern("bogus", "x")
    assert "error" in db.add_rejected_pattern("company", "   ")


def test_remove_missing_pattern_errors(db):
    assert "error" in db.remove_rejected_pattern(999)


def test_remove_pattern_does_not_unreject(db):
    p = db.add_rejected_pattern("company", "infosys")
    r = add(db, company="Infosys Ltd")
    assert r["auto_rejected"] is True
    db.remove_rejected_pattern(p["id"])
    assert db.get_job(r["id"])["status"] == "rejected"


def test_patterns_do_not_retroactively_reject(db):
    r = add(db, company="Infosys Ltd")
    assert r["status"] == "new"
    db.add_rejected_pattern("company", "infosys")
    assert db.get_job(r["id"])["status"] == "new"


# ---------------------------------------------------------------- update_job

def test_invalid_transition(db):
    r = add(db)
    db.mark_applied(r["id"])
    assert "error" in db.update_job(r["id"], status="new")

    other = add(db, company="B Co")
    db.reject_job(other["id"])
    assert db.update_job(other["id"], status="new")["result"] == "updated"
    assert db.get_job(other["id"])["status"] == "new"


def test_update_job_no_fields_errors(db):
    r = add(db)
    before = db.get_job(r["id"])["updated_at"]
    assert "error" in db.update_job(r["id"])
    assert db.get_job(r["id"])["updated_at"] == before


def test_update_job_applied_sets_date(db):
    r = add(db)
    out = db.update_job(r["id"], status="applied")
    assert "date_applied" in out["changed"]
    assert db.get_job(r["id"])["date_applied"] is not None


def test_update_job_notes_replace(db):
    r = add(db, notes="original")
    db.update_job(r["id"], notes="corrected")
    assert db.get_job(r["id"])["notes"] == "corrected"


def test_update_job_missing_id(db):
    assert "error" in db.update_job("nope", fit_score=5)


def test_fit_score_out_of_range(db):
    assert "error" in db.add_job(company="C", title="T", location="Remote", fit_score=11)
    assert db.get_stats()["total_jobs"] == 0
    r = add(db)
    assert "error" in db.update_job(r["id"], fit_score=0)
    assert db.get_job(r["id"])["fit_score"] is None


def test_expired_via_update_job(db):
    r = add(db)
    db.mark_applied(r["id"])
    assert db.update_job(r["id"], status="expired")["result"] == "updated"
    job = db.get_job(r["id"])
    assert job["status"] == "expired"
    # applied -> expired keeps the application date as history
    assert job["date_applied"] is not None


# ------------------------------------------------------------------ get/search

def test_invalid_status_filter(db):
    out = db.get_jobs(status="aplied")
    assert "error" in out
    assert "applied" in out["error"]


def test_get_jobs_pagination_total(db):
    for i in range(5):
        add(db, company=f"Co {i}")
    page1 = db.get_jobs(limit=2)
    page2 = db.get_jobs(limit=2, offset=2)
    assert page1["total"] == 5 and len(page1["jobs"]) == 2
    assert page2["total"] == 5
    assert not {j["id"] for j in page1["jobs"]} & {j["id"] for j in page2["jobs"]}


def test_get_jobs_limit_clamped(db):
    add(db)
    assert db.get_jobs(limit=10_000)["limit"] == 200


def test_get_jobs_ordering(db):
    add(db, company="Unscored Co")
    add(db, company="Low Co", fit_score=3)
    add(db, company="High Co", fit_score=9)
    companies = [j["company"] for j in db.get_jobs()["jobs"]]
    assert companies == ["High Co", "Low Co", "Unscored Co"]


def test_min_fit_score_excludes_unscored(db):
    add(db, company="Unscored Co")
    add(db, company="High Co", fit_score=9)
    jobs = db.get_jobs(min_fit_score=5)["jobs"]
    assert [j["company"] for j in jobs] == ["High Co"]


def test_search_escapes_wildcards(db):
    add(db, title="Engineer")
    add(db, company="B Co", title="100% Remote Engineer")
    out = db.search_jobs("100%")
    assert out["total"] == 1
    assert out["jobs"][0]["title"] == "100% Remote Engineer"


def test_search_underscore_is_literal(db):
    add(db, title="Engineer")
    add(db, company="B Co", title="data_pipeline lead")
    assert db.search_jobs("a_p")["total"] == 1


def test_search_matches_notes_and_subject(db):
    add(db, notes="referred by dana")
    add(db, company="B Co", email_subject="alert: platform roles")
    assert db.search_jobs("dana")["total"] == 1
    assert db.search_jobs("PLATFORM")["total"] == 1


def test_search_empty_query_errors(db):
    assert "error" in db.search_jobs("   ")


def test_get_job_missing(db):
    assert "error" in db.get_job("nope")


# ----------------------------------------------------------------- bulk/stats

def test_bulk_add_jobs_mixed(db):
    db.add_rejected_pattern("company", "infosys")
    add(db, company="Dup Co")
    out = db.bulk_add_jobs([
        {"company": "New Co", "title": "SRE", "location": "Remote"},
        {"company": "Dup Co", "title": "Staff Engineer", "location": "Remote"},
        {"company": "Infosys Ltd", "title": "Java Dev", "location": "Remote"},
        {"title": "no company", "location": "Remote"},
    ])
    assert (out["created"], out["duplicates"], out["auto_rejected"], out["errors"]) == (2, 1, 1, 1)
    assert len(out["results"]) == 4
    assert out["results"][1]["result"] == "duplicate"
    assert out["results"][3]["result"] == "error"


def test_bulk_add_jobs_limits(db):
    assert "error" in db.bulk_add_jobs([])
    assert "error" in db.bulk_add_jobs([{"company": "C", "title": "T", "location": "R"}] * 101)


def test_get_stats(db):
    stats = db.get_stats()
    assert stats["by_status"] == {"new": 0, "applied": 0, "rejected": 0, "expired": 0}
    assert stats["avg_fit_score_new"] is None

    a = add(db, company="A Co", fit_score=6, source="LinkedIn")
    add(db, company="B Co", fit_score=8, source="LinkedIn")
    add(db, company="C Co", source=None)
    db.mark_applied(a["id"])

    stats = db.get_stats()
    assert stats["total_jobs"] == 3
    assert stats["by_status"]["new"] == 2
    assert stats["by_status"]["applied"] == 1
    assert stats["by_source"]["LinkedIn"] == 2
    assert stats["by_source"]["(unknown)"] == 1
    assert stats["avg_fit_score_applied"] == 6.0
    assert stats["avg_fit_score_new"] == 8.0
    assert stats["unscored_new"] == 1


def test_schema_version_set(db):
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 3


# --------------------------------------------------- company / title keys

@pytest.mark.parametrize("raw,expected", [
    ("Acme Corp", "acme"),
    ("Acme Corporation", "acme"),
    ("Acme, Inc.", "acme"),
    ("Acme", "acme"),
    ("Acme Corp Inc", "acme"),
    ("Infosys BPM Ltd", "infosys bpm"),
    ("Infosys Limited", "infosys"),
    ("Limited", "limited"),        # a company named only a suffix survives
    ("Ag Solutions", "ag solutions"),  # suffixes strip from the tail only
])
def test_canonicalize_company(raw, expected):
    assert canonicalize_company(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Staff Engineer", "staff engineer"),
    ("Staff Engineer (Remote)", "staff engineer"),
    ("Staff Engineer [R-12345]", "staff engineer"),
    ("Staff Engineer - Remote - $180k-$220k", "staff engineer"),
    ("Staff Engineer - US", "staff engineer"),
    ("Staff Engineer | Remote", "staff engineer"),
    # Meaning-bearing text must survive.
    ("Senior Staff Engineer", "senior staff engineer"),
    ("Staff Engineer, Platform", "staff engineer, platform"),
    ("Staff Engineer II", "staff engineer ii"),
    ("Full-Stack Engineer", "full-stack engineer"),
    ("Front-end Developer", "front-end developer"),
    ("Engineer (Contract)", "engineer (contract)"),
])
def test_canonicalize_title(raw, expected):
    assert canonicalize_title(raw) == expected


def test_company_variants_dedupe(db):
    for c in ["Acme Corp", "Acme Corporation", "Acme, Inc.", "Acme"]:
        add(db, company=c)
    assert db.get_stats()["total_jobs"] == 1
    assert db.get_jobs()["jobs"][0]["company"] == "Acme Corp"  # first spelling wins


def test_title_decorations_dedupe(db):
    for t in ["Staff Engineer", "Staff Engineer (Remote)",
              "Staff Engineer - Remote - $180k-$220k", "Staff Engineer [R-12345]"]:
        add(db, title=t)
    assert db.get_stats()["total_jobs"] == 1
    assert db.get_job(db.get_jobs()["jobs"][0]["id"])["times_seen"] == 4


def test_distinct_reqs_stay_distinct(db):
    for t in ["Staff Engineer", "Senior Staff Engineer",
              "Staff Engineer, Platform", "Staff Engineer II"]:
        add(db, title=t)
    assert db.get_stats()["total_jobs"] == 4


def test_canonical_columns_stored(db):
    r = add(db, company="Acme, Inc.", title="Staff Engineer (Remote)", location="Remote, US")
    job = db.get_job(r["id"])
    assert (job["company_canonical"], job["title_canonical"], job["location_canonical"]) \
        == ("acme", "staff engineer", "remote")
    assert job["company"] == "Acme, Inc."  # raw preserved for display


# ------------------------------------------------------------ near-dup hints

def test_possible_duplicates_reported_not_merged(db):
    add(db, title="Staff Engineer")
    r = add(db, title="Staff Software Engineer")
    assert r["result"] == "created"
    assert len(r["possible_duplicates"]) == 1
    assert r["possible_duplicates"][0]["title"] == "Staff Engineer"
    assert r["possible_duplicates"][0]["score"] >= 0.6
    assert db.get_stats()["total_jobs"] == 2  # NOT auto-merged


@pytest.mark.parametrize("a,b,flagged", [
    ("Staff Engineer", "Staff Software Engineer", True),
    ("Staff Engineer", "Senior Staff Engineer", True),
    ("Staff Engineer", "Staff Engineer, Platform", True),
    # Character similarity would score these 0.79 and wrongly flag them.
    ("Backend Engineer", "Frontend Engineer", False),
    ("Data Engineer", "Data Scientist", False),
    ("Staff Engineer", "Director of Marketing", False),
])
def test_similarity_flags_meaning_not_spelling(db, a, b, flagged):
    add(db, title=a)
    r = add(db, title=b)
    assert ("possible_duplicates" in r) is flagged


def test_no_possible_duplicates_across_companies(db):
    add(db, company="Acme Corp", title="Staff Engineer")
    r = add(db, company="Globex", title="Staff Software Engineer")
    assert "possible_duplicates" not in r


def test_dissimilar_titles_not_flagged(db):
    add(db, title="Staff Engineer")
    r = add(db, title="Director of Marketing")
    assert "possible_duplicates" not in r


# ----------------------------------------------------------------- merging

def test_merge_hides_duplicate_and_redirects(db):
    keep = add(db, title="Staff Engineer")
    dup = add(db, title="Staff Software Engineer")
    out = db.merge_jobs(keep["id"], dup["id"])
    assert out["result"] == "merged"

    assert db.get_stats()["total_jobs"] == 1
    assert db.get_stats()["merged_duplicates"] == 1
    assert [j["id"] for j in db.get_jobs()["jobs"]] == [keep["id"]]
    assert db.search_jobs("Staff Software")["total"] == 0

    # A later sighting of the merged posting lands on the survivor.
    again = add(db, title="Staff Software Engineer")
    assert again["result"] == "duplicate"
    assert again["id"] == keep["id"]

    # The row still exists and is inspectable by id.
    assert db.get_job(dup["id"])["merged_into"] == keep["id"]


def test_merge_aggregates_sightings_and_notes(db):
    keep = add(db, title="Staff Engineer", notes="keep note")
    dup = add(db, title="Staff Software Engineer", notes="dup note")
    add(db, title="Staff Software Engineer")  # dup now times_seen=2
    db.merge_jobs(keep["id"], dup["id"])
    survivor = db.get_job(keep["id"])
    assert survivor["times_seen"] == 3
    assert "keep note" in survivor["notes"] and "dup note" in survivor["notes"]


def test_merge_never_loses_an_application(db):
    keep = add(db, title="Staff Engineer")
    dup = add(db, title="Staff Software Engineer")
    db.mark_applied(dup["id"], date_applied="2026-06-01")
    db.merge_jobs(keep["id"], dup["id"])
    survivor = db.get_job(keep["id"])
    assert survivor["status"] == "applied"
    assert survivor["date_applied"] == "2026-06-01"


def test_merge_backfills_nulls(db):
    keep = add(db, title="Staff Engineer")
    dup = add(db, title="Staff Software Engineer", url="https://x.example",
              source="Indeed", fit_score=7)
    db.merge_jobs(keep["id"], dup["id"])
    survivor = db.get_job(keep["id"])
    assert survivor["url"] == "https://x.example"
    assert survivor["source"] == "Indeed"
    assert survivor["fit_score"] == 7


def test_merge_validates(db):
    a = add(db, title="Staff Engineer")
    b = add(db, title="Staff Software Engineer")
    assert "error" in db.merge_jobs(a["id"], a["id"])
    assert "error" in db.merge_jobs(a["id"], "nope")
    assert "error" in db.merge_jobs("nope", b["id"])
    db.merge_jobs(a["id"], b["id"])
    assert "error" in db.merge_jobs(a["id"], b["id"])  # already merged


def test_merge_pointers_never_chain(db):
    a = add(db, title="Staff Engineer")
    b = add(db, title="Staff Software Engineer")
    c = add(db, title="Staff Engineer, Platform")
    db.merge_jobs(b["id"], c["id"])       # c -> b
    db.merge_jobs(a["id"], b["id"])       # b -> a, and c must be re-pointed to a
    assert db.get_job(c["id"])["merged_into"] == a["id"]
    assert db.get_job(b["id"])["merged_into"] == a["id"]
    # merging into a non-surviving row is refused
    assert "error" in db.merge_jobs(b["id"], a["id"])


def test_merged_rows_reject_mutation(db):
    keep = add(db, title="Staff Engineer")
    dup = add(db, title="Staff Software Engineer")
    db.merge_jobs(keep["id"], dup["id"])
    for out in (db.mark_applied(dup["id"]),
                db.reject_job(dup["id"]),
                db.update_job(dup["id"], fit_score=5)):
        assert "error" in out
        assert keep["id"] in out["error"]


def test_unmerge_restores(db):
    keep = add(db, title="Staff Engineer")
    dup = add(db, title="Staff Software Engineer", notes="dup note")
    db.merge_jobs(keep["id"], dup["id"])
    assert db.unmerge_job(dup["id"])["result"] == "unmerged"
    assert db.get_stats()["total_jobs"] == 2
    assert db.get_job(dup["id"])["merged_into"] is None
    assert db.get_job(dup["id"])["notes"] == "dup note"  # never modified
    assert "error" in db.unmerge_job(dup["id"])  # not merged any more


# ------------------------------------------------------- date_seen (first appearance)

def test_date_seen_defaults_to_today(db):
    from job_tracker_mcp.database import today_str
    r = add(db)
    assert db.get_job(r["id"])["date_seen"] == today_str()


def test_date_seen_records_when_the_posting_appeared(db):
    """The alert email's date, NOT the date triage ran."""
    r = add(db, date_seen="2026-06-01")
    job = db.get_job(r["id"])
    assert job["date_seen"] == "2026-06-01"
    assert job["last_seen"] == "2026-06-01"


def test_backlog_import_keeps_earliest_first_seen(db):
    """Emails do not arrive in date order, and a backlog is replayed in any order."""
    r = add(db, date_seen="2026-06-10")
    add(db, date_seen="2026-06-20")        # a later sighting
    add(db, date_seen="2026-06-01")        # an OLDER email, processed last
    job = db.get_job(r["id"])
    assert job["date_seen"] == "2026-06-01"   # first appearance never moves forward
    assert job["last_seen"] == "2026-06-20"   # last sighting never moves backward
    assert job["times_seen"] == 3


def test_date_seen_validated(db):
    assert "error" in db.add_job(company="C", title="T", location="R", date_seen="06/01/2026")
    assert db.get_stats()["total_jobs"] == 0


def test_bulk_add_jobs_carries_date_seen(db):
    out = db.bulk_add_jobs([
        {"company": "Acme", "title": "SRE", "location": "Remote", "date_seen": "2026-05-05"},
    ])
    assert out["created"] == 1
    assert db.get_jobs()["jobs"][0]["date_seen"] == "2026-05-05"
