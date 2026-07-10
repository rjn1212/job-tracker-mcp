import sqlite3

import pytest

from job_tracker_mcp.database import SCHEMA_SQL, SCHEMA_VERSION, Database
from job_tracker_mcp.onenote.store import OneNoteStore


def make_v1_db(path):
    """A database exactly as Part 1 shipped it: v1 schema, user_version=1."""
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA_SQL)
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "INSERT INTO jobs (id, company, company_canonical, title, title_canonical, "
        "location, location_canonical, date_seen, last_seen, times_seen, status, "
        "date_applied, notes, created_at, updated_at) VALUES "
        "('abc123', 'Acme Corp', 'acme', 'Staff Engineer', 'staff engineer', "
        "'Remote', 'remote', '2026-06-01', '2026-06-01', 3, 'applied', "
        "'2026-06-02', 'keep me', "
        "'2026-06-01T00:00:00-07:00', '2026-06-01T00:00:00-07:00')"
    )
    conn.commit()
    conn.close()


def test_fresh_db_is_current(db):
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    names = {r[0] for r in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"jobs", "rejected_patterns", "onenote_pages",
            "onenote_links", "onenote_state"} <= names


def test_migration_v1_to_current(tmp_path):
    p = tmp_path / "v1.db"
    make_v1_db(p)
    c = sqlite3.connect(str(p)); c.row_factory = sqlite3.Row
    before = c.execute("SELECT * FROM jobs").fetchone()
    c.close()

    d = Database(db_path=p)
    assert d.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    after = d.conn.execute("SELECT * FROM jobs").fetchone()

    # Every field survives. The id is REBUILT, because the v1 fixture used a
    # placeholder id and v5->v6 recomputes ids from the current canonicalizers.
    from job_tracker_mcp.models import generate_job_id
    assert after["id"] == generate_job_id(after["company"], after["title"], after["location"])
    for col in ("company", "title", "location", "status", "date_applied", "notes",
                "date_seen", "times_seen", "created_at"):
        assert after[col] == before[col], col

    names = {r[0] for r in d.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"onenote_pages", "onenote_links", "onenote_state"} <= names
    d.close()


def test_migration_is_idempotent(tmp_path):
    """The crash gap: executescript() commits before the user_version bump lands."""
    p = tmp_path / "v1.db"
    make_v1_db(p)
    d = Database(db_path=p)
    d.close()

    # Simulate a crash after the tables were created but before the version bumped.
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    d = Database(db_path=p)                        # must not raise
    assert d.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert d.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert d._has_column("onenote_pages", "page_order")
    d.close()


def test_migration_v2_to_v3_add_column_is_guarded(tmp_path):
    """ALTER TABLE ADD COLUMN is NOT idempotent -- unlike the v1->v2 CREATEs.

    Re-running the step after a crash between the DDL and the version bump must
    not raise "duplicate column name".
    """
    p = tmp_path / "v1.db"
    make_v1_db(p)
    d = Database(db_path=p)
    d._migrate_2_to_3()                            # run the step a second time
    assert d._has_column("onenote_pages", "page_order")
    d.close()


def test_future_database_refuses(tmp_path):
    p = tmp_path / "v9.db"
    d = Database(db_path=p)
    d.conn.execute("PRAGMA user_version = 99")
    d.conn.commit()
    d.close()
    with pytest.raises(RuntimeError, match="newer than this code"):
        Database(db_path=p)


# ------------------------------------------------------------ foreign keys

def _page(store, page_id="p1", section="s1", title="T"):
    store.write_sync(section, [{"page_id": page_id, "title": title,
                                "created_dt": "2026-07-01T00:00:00Z",
                                "last_modified_dt": "2026-07-01T00:00:00Z"}], {})


def test_link_cascade_on_page_delete(db):
    """Proves PRAGMA foreign_keys=ON is live. This is the cascade that fires."""
    store = OneNoteStore(db)
    db.add_job(company="Acme", title="Staff Engineer", location="Remote")
    job_id = db.get_jobs()["jobs"][0]["id"]
    _page(store)
    store.add_link("p1", job_id, "manual", 1.0)

    db.conn.execute("DELETE FROM onenote_pages WHERE page_id = 'p1'")
    db.conn.commit()
    assert db.conn.execute("SELECT COUNT(*) FROM onenote_links").fetchone()[0] == 0


def test_link_fk_rejects_unknown_job(db):
    store = OneNoteStore(db)
    _page(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.add_link("p1", "no-such-job", "manual", 1.0)


def test_tombstone_not_delete(db):
    store = OneNoteStore(db)
    db.add_job(company="Acme", title="Staff Engineer", location="Remote")
    job_id = db.get_jobs()["jobs"][0]["id"]
    _page(store)
    store.add_link("p1", job_id, "manual", 1.0)

    store.write_sync("s1", [], {})                 # page vanished from Graph
    row = store.get_page_row("p1")
    assert row is not None and row["deleted_at"] is not None
    assert db.conn.execute("SELECT COUNT(*) FROM onenote_links").fetchone()[0] == 1
    assert store.list_pages()["total"] == 0        # hidden from listings


def test_content_cache_key(db):
    store = OneNoteStore(db)
    _page(store)
    state = store.sync_state("s1")
    assert state["p1"]["content_synced_dt"] is None      # never fetched

    store.write_sync("s1", [{"page_id": "p1", "title": "T",
                             "created_dt": "2026-07-01T00:00:00Z",
                             "last_modified_dt": "2026-07-01T00:00:00Z"}],
                     {"p1": ("<html/>", "body")})
    state = store.sync_state("s1")
    assert state["p1"]["content_synced_dt"] == "2026-07-01T00:00:00Z"  # cached

    # Page edited upstream -> cache key no longer matches -> refetch required.
    store.write_sync("s1", [{"page_id": "p1", "title": "T",
                             "created_dt": "2026-07-01T00:00:00Z",
                             "last_modified_dt": "2026-07-09T00:00:00Z"}], {})
    state = store.sync_state("s1")
    assert state["p1"]["content_synced_dt"] != state["p1"]["last_modified_dt"]
    assert store.get_page_row("p1")["content_text"] == "body"   # stale text retained


def test_write_sync_preserves_content_when_not_refetched(db):
    store = OneNoteStore(db)
    _page(store)
    store.write_sync("s1", [{"page_id": "p1", "title": "T",
                             "created_dt": "2026-07-01T00:00:00Z",
                             "last_modified_dt": "2026-07-01T00:00:00Z"}],
                     {"p1": ("<html/>", "cached body")})
    store.write_sync("s1", [{"page_id": "p1", "title": "T2",
                             "created_dt": "2026-07-01T00:00:00Z",
                             "last_modified_dt": "2026-07-01T00:00:00Z"}], {})
    row = store.get_page_row("p1")
    assert row["content_text"] == "cached body"
    assert row["title"] == "T2"


# ------------------------------------------------------ merge integration

def _two_jobs(db):
    a = db.add_job(company="Acme Corp", title="Staff Engineer", location="Remote")
    b = db.add_job(company="Acme Corp", title="Staff Software Engineer", location="Remote")
    return a["id"], b["id"]


def test_merge_jobs_repoints_links(db):
    store = OneNoteStore(db)
    a, b = _two_jobs(db)
    _page(store)
    store.add_link("p1", b, "manual", 1.0, date_applied="2026-06-01")

    db.merge_jobs(a, b)

    links = store.links()
    assert len(links) == 1
    assert links[0]["job_id"] == a                 # evidence followed the job
    assert links[0]["date_applied"] == "2026-06-01"
    assert not [x for x in links if x["job_id"] == b]


def test_merge_jobs_link_collision(db):
    """A page linked to BOTH rows must not violate PRIMARY KEY(page_id, job_id)."""
    store = OneNoteStore(db)
    a, b = _two_jobs(db)
    _page(store)
    store.add_link("p1", a, "marker", 1.0)
    store.add_link("p1", b, "manual", 1.0)

    db.merge_jobs(a, b)                            # must not raise

    links = store.links()
    assert len(links) == 1
    assert links[0]["job_id"] == a


def test_live_jobs_excludes_merged(db):
    a, b = _two_jobs(db)
    assert len(db.live_jobs()) == 2
    db.merge_jobs(a, b)
    live = db.live_jobs()
    assert [j["id"] for j in live] == [a]
    assert db.merged_into(b) == a
    assert db.merged_into(a) is None


def test_state_roundtrip(db):
    store = OneNoteStore(db)
    assert store.get_state("section_id") is None
    store.set_state("section_id", "1-abc")
    assert store.get_state("section_id") == "1-abc"
    store.set_state("section_id", "1-def")         # upsert, not duplicate
    assert store.get_state("section_id") == "1-def"
    store.clear_state("section_id")
    assert store.get_state("section_id") is None


def test_search_snippets_not_bodies(db):
    store = OneNoteStore(db)
    long_body = "\n".join(["filler"] * 50 + ["I applied to Acme on 7/2"] + ["filler"] * 50)
    store.write_sync("s1", [{"page_id": "p1", "title": "Log",
                             "created_dt": "2026-07-01T00:00:00Z",
                             "last_modified_dt": "2026-07-01T00:00:00Z"}],
                     {"p1": ("<html/>", long_body)})
    out = store.search_pages("acme")
    assert out["total"] == 1
    assert out["pages"][0]["snippets"] == ["I applied to Acme on 7/2"]
    assert "content_text" not in out["pages"][0]


# ------------------------------------------- id rebuild after a canon change

def test_id_rebuild_carries_links_and_merge_pointers(db):
    """Changing canonicalize_title changes ids. Links and merged_into must follow."""
    from job_tracker_mcp.models import generate_job_id
    store = OneNoteStore(db)
    r = db.add_job(company="Acme", title="Director, Software Engineering", location="Remote")
    real_id = r["id"]
    _page(store)
    store.add_link("p1", real_id, "manual", 1.0)

    # Simulate a row keyed under an older canonicalizer.
    stale = "0000000000000000"
    db.conn.execute("PRAGMA defer_foreign_keys = ON")
    db.conn.execute("UPDATE onenote_links SET job_id = ? WHERE job_id = ?", (stale, real_id))
    db.conn.execute("UPDATE jobs SET id = ? WHERE id = ?", (stale, real_id))
    db.conn.commit()

    out = db.rebuild_ids()
    assert out["lost"] == 0
    assert db.get_job(real_id)["title"] == "Director, Software Engineering"
    assert store.links()[0]["job_id"] == real_id          # evidence followed the id


def test_id_rebuild_is_idempotent(db):
    db.add_job(company="Acme", title="Staff Engineer", location="Remote")
    assert db.rebuild_ids()["lost"] == 0
    assert db.rebuild_ids()["lost"] == 0


def test_id_rebuild_refuses_to_guess_on_collision(db):
    """Two rows that now canonicalize identically must not be silently merged."""
    import pytest
    a = db.add_job(company="Acme", title="Director, Software Engineering", location="Remote")
    db.conn.execute("PRAGMA defer_foreign_keys = ON")
    db.conn.execute(
        "UPDATE jobs SET id = ?, title = ? WHERE id = ?",
        ("1111111111111111", "Director, Software Engineering (1139652)", a["id"]))
    db.conn.commit()
    db.add_job(company="Acme", title="Director, Software Engineering", location="Remote")
    with pytest.raises(RuntimeError, match="collide"):
        db.rebuild_ids()
