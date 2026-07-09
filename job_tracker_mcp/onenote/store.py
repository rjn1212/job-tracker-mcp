"""Persistence for the OneNote mirror: the three v2 tables and all their SQL.

Imports nothing heavier than the standard library. `database.py` imports
ONENOTE_SCHEMA_SQL from here, so a dependency on msal/httpx at this level would
put a network stack in the MCP server's import path. Keep it clean.
"""

from __future__ import annotations

import json
import sqlite3

# Every statement is CREATE ... IF NOT EXISTS. The 1->2 migration relies on that
# idempotency for its safety -- see the migration note in database.py.
ONENOTE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS onenote_pages (
    page_id           TEXT PRIMARY KEY,
    section_id        TEXT NOT NULL,
    title             TEXT NOT NULL,
    created_dt        TEXT NOT NULL,
    last_modified_dt  TEXT NOT NULL,
    content_html      TEXT,
    content_text      TEXT,
    content_synced_dt TEXT,
    web_url           TEXT,
    client_url        TEXT,
    is_ignored        INTEGER NOT NULL DEFAULT 0,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    deleted_at        TEXT,

    CHECK (is_ignored IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_onp_section  ON onenote_pages(section_id);
CREATE INDEX IF NOT EXISTS idx_onp_modified ON onenote_pages(last_modified_dt DESC);
CREATE INDEX IF NOT EXISTS idx_onp_live     ON onenote_pages(deleted_at, is_ignored);

CREATE TABLE IF NOT EXISTS onenote_links (
    page_id      TEXT NOT NULL REFERENCES onenote_pages(page_id) ON DELETE CASCADE,
    job_id       TEXT NOT NULL REFERENCES jobs(id)               ON DELETE CASCADE,
    match_kind   TEXT NOT NULL,
    confidence   REAL NOT NULL,
    evidence     TEXT,
    date_applied TEXT,
    extracted    TEXT,
    created_at   TEXT NOT NULL,

    PRIMARY KEY (page_id, job_id),
    CHECK (match_kind IN ('marker','exact','fuzzy','manual','created_by_tool')),
    CHECK (confidence >= 0.0 AND confidence <= 1.0)
);

CREATE INDEX IF NOT EXISTS idx_onl_job ON onenote_links(job_id);

CREATE TABLE IF NOT EXISTS onenote_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL
);
"""

# Merged duplicates and tombstoned pages are hidden from every listing.
LIVE_PAGE = "deleted_at IS NULL"

PAGE_FIELDS = (
    "page_id", "section_id", "title", "created_dt", "last_modified_dt",
    "web_url", "client_url", "is_ignored", "first_seen_at", "last_seen_at",
    "deleted_at",
)


def page_to_dict(row: sqlite3.Row) -> dict:
    d = {k: row[k] for k in PAGE_FIELDS}
    d["is_ignored"] = bool(d["is_ignored"])
    d["has_content"] = row["content_text"] is not None
    d["stale"] = row["content_synced_dt"] != row["last_modified_dt"]
    return d


class OneNoteStore:
    """All OneNote SQL. Borrows the Database's connection and lock.

    Sharing Part 1's lock is deliberate: one connection, one serialization
    point. No method here performs I/O beyond SQLite -- network work happens in
    client.py and completes BEFORE any of these writes begin.
    """

    def __init__(self, db):
        self.db = db
        self.conn = db.conn
        self._lock = db._lock

    # ------------------------------------------------------------- state

    def get_state(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM onenote_state WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str | None) -> None:
        from job_tracker_mcp.database import now_str
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO onenote_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, now_str()),
            )

    def clear_state(self, *keys: str) -> None:
        with self._lock, self.conn:
            for k in keys:
                self.conn.execute("DELETE FROM onenote_state WHERE key = ?", (k,))

    # -------------------------------------------------- sync: read phase

    def sync_state(self, section_id: str) -> dict[str, dict]:
        """page_id -> {last_modified_dt, content_synced_dt}. Phase R of a sync."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT page_id, last_modified_dt, content_synced_dt, deleted_at "
                "FROM onenote_pages WHERE section_id = ?", (section_id,)
            ).fetchall()
        return {r["page_id"]: dict(r) for r in rows}

    # ------------------------------------------------- sync: write phase

    def write_sync(self, section_id: str, pages: list[dict],
                   contents: dict[str, tuple[str, str]]) -> dict:
        """Phase W. One short transaction. No network anywhere below this line.

        `pages` is the full enumeration from Graph; `contents` maps page_id ->
        (html, text) for the subset that was refetched.
        """
        from job_tracker_mcp.database import now_str
        now = now_str()
        seen = {p["page_id"] for p in pages}
        counts = {"pages_new": 0, "pages_updated": 0, "pages_unchanged": 0,
                  "pages_tombstoned": 0}

        with self._lock, self.conn:
            existing = {
                r["page_id"]: r for r in self.conn.execute(
                    "SELECT page_id, last_modified_dt, deleted_at FROM onenote_pages "
                    "WHERE section_id = ?", (section_id,)
                ).fetchall()
            }

            for p in pages:
                pid = p["page_id"]
                prev = existing.get(pid)
                if prev is None:
                    counts["pages_new"] += 1
                elif prev["last_modified_dt"] != p["last_modified_dt"]:
                    counts["pages_updated"] += 1
                else:
                    counts["pages_unchanged"] += 1

                html, text = contents.get(pid, (None, None))
                synced = p["last_modified_dt"] if pid in contents else None

                self.conn.execute(
                    "INSERT INTO onenote_pages (page_id, section_id, title, created_dt, "
                    "last_modified_dt, content_html, content_text, content_synced_dt, "
                    "web_url, client_url, is_ignored, first_seen_at, last_seen_at, deleted_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL) "
                    "ON CONFLICT(page_id) DO UPDATE SET "
                    "  title = excluded.title, "
                    "  last_modified_dt = excluded.last_modified_dt, "
                    "  web_url = COALESCE(excluded.web_url, onenote_pages.web_url), "
                    "  client_url = COALESCE(excluded.client_url, onenote_pages.client_url), "
                    "  last_seen_at = excluded.last_seen_at, "
                    "  deleted_at = NULL, "
                    # Only overwrite cached content when this sync actually fetched it.
                    "  content_html = COALESCE(excluded.content_html, onenote_pages.content_html), "
                    "  content_text = COALESCE(excluded.content_text, onenote_pages.content_text), "
                    "  content_synced_dt = COALESCE(excluded.content_synced_dt, "
                    "                               onenote_pages.content_synced_dt)",
                    (pid, section_id, p["title"], p["created_dt"], p["last_modified_dt"],
                     html, text, synced, p.get("web_url"), p.get("client_url"), now, now),
                )

            for pid, row in existing.items():
                if pid not in seen and row["deleted_at"] is None:
                    self.conn.execute(
                        "UPDATE onenote_pages SET deleted_at = ? WHERE page_id = ?",
                        (now, pid),
                    )
                    counts["pages_tombstoned"] += 1

        return counts

    def clear_content_sync(self, section_id: str) -> None:
        """full=True: forget what we have, so Phase N refetches everything."""
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE onenote_pages SET content_synced_dt = NULL WHERE section_id = ?",
                (section_id,),
            )

    def upsert_page(self, page: dict, section_id: str) -> None:
        """Insert a page this server just created, so the next sync sees it unchanged."""
        from job_tracker_mcp.database import now_str
        now = now_str()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO onenote_pages (page_id, section_id, title, "
                "created_dt, last_modified_dt, content_html, content_text, "
                "content_synced_dt, web_url, client_url, is_ignored, first_seen_at, "
                "last_seen_at, deleted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL)",
                (page["page_id"], section_id, page["title"], page["created_dt"],
                 page["last_modified_dt"], page.get("content_html"),
                 page.get("content_text"), page["last_modified_dt"],
                 page.get("web_url"), page.get("client_url"), now, now),
            )

    # ------------------------------------------------------------- reads

    def list_pages(self, include_ignored=False, include_deleted=False,
                   modified_since: str | None = None, limit=50, offset=0) -> dict:
        where, params = [], []
        if not include_deleted:
            where.append(LIVE_PAGE)
        if not include_ignored:
            where.append("is_ignored = 0")
        if modified_since:
            where.append("last_modified_dt >= ?")
            params.append(modified_since)
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        with self._lock:
            total = self.conn.execute(
                f"SELECT COUNT(*) FROM onenote_pages {clause}", params
            ).fetchone()[0]
            rows = self.conn.execute(
                f"SELECT * FROM onenote_pages {clause} "
                f"ORDER BY last_modified_dt DESC, page_id ASC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            out = []
            for r in rows:
                d = page_to_dict(r)
                d["linked_job_ids"] = [
                    x["job_id"] for x in self.conn.execute(
                        "SELECT job_id FROM onenote_links WHERE page_id = ?", (r["page_id"],)
                    )
                ]
                out.append(d)
        return {"pages": out, "total": total, "offset": offset, "limit": limit}

    def get_page_row(self, page_id: str):
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM onenote_pages WHERE page_id = ?", (page_id,)
            ).fetchone()

    def search_pages(self, query: str, limit=20, offset=0) -> dict:
        from job_tracker_mcp.database import like_escape
        pattern = f"%{like_escape(query.strip())}%"
        clause = (
            f"WHERE {LIVE_PAGE} AND ("
            "LOWER(title) LIKE LOWER(?) ESCAPE '\\' OR "
            "LOWER(COALESCE(content_text, '')) LIKE LOWER(?) ESCAPE '\\')"
        )
        with self._lock:
            total = self.conn.execute(
                f"SELECT COUNT(*) FROM onenote_pages {clause}", (pattern, pattern)
            ).fetchone()[0]
            rows = self.conn.execute(
                f"SELECT * FROM onenote_pages {clause} "
                f"ORDER BY last_modified_dt DESC, page_id ASC LIMIT ? OFFSET ?",
                (pattern, pattern, limit, offset),
            ).fetchall()

        needle = query.strip().lower()
        pages = []
        for r in rows:
            d = page_to_dict(r)
            d["snippets"] = _snippets(r["content_text"] or "", needle)
            pages.append(d)
        return {"pages": pages, "total": total, "offset": offset, "limit": limit}

    def set_ignored(self, page_id: str, ignore: bool) -> bool:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE onenote_pages SET is_ignored = ? WHERE page_id = ?",
                (1 if ignore else 0, page_id),
            )
        return cur.rowcount > 0

    # ------------------------------------------------------------- links

    def add_link(self, page_id: str, job_id: str, match_kind: str, confidence: float,
                 evidence: str | None = None, date_applied: str | None = None,
                 extracted: dict | None = None, overwrite: bool = False) -> str:
        """Returns 'linked' or 'exists'.

        Reconciliation passes overwrite=False, so a manual link is never clobbered
        by a later fuzzy match.
        """
        from job_tracker_mcp.database import now_str
        blob = json.dumps(extracted) if extracted is not None else None
        verb = "INSERT OR REPLACE INTO" if overwrite else "INSERT OR IGNORE INTO"
        with self._lock, self.conn:
            cur = self.conn.execute(
                f"{verb} onenote_links (page_id, job_id, match_kind, confidence, "
                "evidence, date_applied, extracted, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (page_id, job_id, match_kind, confidence, evidence, date_applied,
                 blob, now_str()),
            )
        return "linked" if cur.rowcount else "exists"

    def remove_link(self, page_id: str, job_id: str) -> bool:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "DELETE FROM onenote_links WHERE page_id = ? AND job_id = ?",
                (page_id, job_id),
            )
        return cur.rowcount > 0

    def get_link(self, page_id: str, job_id: str):
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM onenote_links WHERE page_id = ? AND job_id = ?",
                (page_id, job_id),
            ).fetchone()

    def links(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM onenote_links").fetchall()
        return [dict(r) for r in rows]

    def created_by_tool_link(self, job_id: str):
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM onenote_links WHERE job_id = ? AND match_kind = 'created_by_tool'",
                (job_id,),
            ).fetchone()

    def count_pages(self) -> int:
        with self._lock:
            return self.conn.execute(
                f"SELECT COUNT(*) FROM onenote_pages WHERE {LIVE_PAGE}"
            ).fetchone()[0]


def _snippets(text: str, needle: str, limit: int = 3, pad: int = 100) -> list[str]:
    """Up to `limit` matched lines with surrounding context.

    Snippets, not page bodies: a search over 47 pages that returns 47 page
    bodies is not a search, it is a denial-of-service on the context window.
    """
    out = []
    for line in text.splitlines():
        if needle in line.lower():
            idx = line.lower().index(needle)
            start, end = max(0, idx - pad), min(len(line), idx + len(needle) + pad)
            snippet = line[start:end].strip()
            out.append(("..." if start else "") + snippet + ("..." if end < len(line) else ""))
            if len(out) >= limit:
                break
    return out
