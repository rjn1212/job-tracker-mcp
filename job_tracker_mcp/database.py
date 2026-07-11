"""SQLite persistence for job-tracker-mcp.

All business logic lives here; server.py is a thin MCP adapter. Every public
method returns a JSON-serializable dict and never raises for an expected
condition (missing id, bad status, invalid transition) -- those return
{"error": "..."} with an actionable message.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

from job_tracker_mcp.models import (
    ALLOWED_TRANSITIONS,
    parse_salary,
    JOB_FIELDS,
    PATTERN_TYPES,
    PROFILE_RULE_TYPES,
    STATUSES,
    evaluate_profile,
    canonicalize_company,
    canonicalize_location,
    canonicalize_title,
    generate_job_id,
    normalize_key_part,
    row_to_job,
)
# store.py depends on nothing heavier than sqlite3. Importing it here does NOT
# pull msal or httpx into the server's startup path -- see onenote/__init__.py.
from job_tracker_mcp.onenote.store import ONENOTE_SCHEMA_SQL

log = logging.getLogger(__name__)

SCHEMA_VERSION = 11
MAX_LIMIT = 200
MAX_BULK = 100

# Titles at the same company whose word sets overlap at least this much are
# surfaced as possible duplicates. Never auto-merged: a false split is a
# visible duplicate, a false merge silently destroys a job you applied to.
#
# Jaccard over words, not character similarity. "staff engineer" vs "staff
# software engineer" is one inserted word (0.67 here) but only 0.76 by
# character ratio, while "backend engineer" vs "frontend engineer" -- utterly
# different roles -- scores 0.79 by characters and 0.33 here. Characters
# measure spelling; we want to measure meaning.
DUP_THRESHOLD = 0.6
MAX_DUP_HINTS = 5

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id                 TEXT PRIMARY KEY,
    company            TEXT NOT NULL,
    company_canonical  TEXT NOT NULL,
    title              TEXT NOT NULL,
    title_canonical    TEXT NOT NULL,
    location           TEXT NOT NULL,
    location_canonical TEXT NOT NULL,
    url                TEXT,
    source             TEXT,
    date_seen          TEXT NOT NULL,
    last_seen          TEXT NOT NULL,
    times_seen         INTEGER NOT NULL DEFAULT 1,
    date_applied       TEXT,
    status             TEXT NOT NULL DEFAULT 'new',
    fit_score          INTEGER,
    -- Salary, when the posting states one. Not part of the id: it can change
    -- between sightings, and a re-sighting must never create a second row.
    salary_min         INTEGER,
    salary_max         INTEGER,
    salary_period      TEXT,          -- 'year' | 'hour'
    salary_annual_min  INTEGER,       -- hourly normalized at 2080 h/yr, for sorting
    salary_annual_max  INTEGER,
    salary_raw         TEXT,          -- exactly as the alert wrote it
    -- The full posting text, fetched lazily from `url` by the reviewing agent
    -- and written back. NULL until fetched; the alert email never carries it.
    description        TEXT,
    description_fetched_at TEXT,       -- local ISO; when the JD was cached
    -- Stamped when an agent updates fit_score on a job that has a description:
    -- i.e. the score now reflects the actual posting, not just the title. Lets
    -- a review resume, and separates JD-scored jobs from title-only ones.
    jd_reviewed_at     TEXT,
    notes              TEXT,
    email_subject      TEXT,
    merged_into        TEXT REFERENCES jobs(id),
    -- Derived from profile_rules, NOT a decision. Recomputed whenever the rules
    -- change, so a rule edit never leaves stale flags behind.
    off_profile        INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,

    CHECK (off_profile IN (0, 1)),
    CHECK (status IN ('new', 'applied', 'rejected', 'expired')),
    CHECK (fit_score IS NULL OR (fit_score BETWEEN 1 AND 10)),
    CHECK (times_seen >= 1),
    CHECK (status != 'applied' OR date_applied IS NOT NULL),
    CHECK (length(location_canonical) > 0),
    CHECK (merged_into IS NULL OR merged_into != id),
    CHECK (salary_period IS NULL OR salary_period IN ('year', 'hour')),
    CHECK (salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max)
);

CREATE TABLE IF NOT EXISTS rejected_patterns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type  TEXT NOT NULL,
    pattern_value TEXT NOT NULL,
    reason        TEXT,
    created_at    TEXT NOT NULL,

    CHECK (pattern_type IN ('company', 'title_keyword', 'source')),
    CHECK (length(trim(pattern_value)) > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_patterns_unique
    ON rejected_patterns(pattern_type, pattern_value);

CREATE INDEX IF NOT EXISTS idx_jobs_status    ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_company   ON jobs(company_canonical);
CREATE INDEX IF NOT EXISTS idx_jobs_date_seen ON jobs(date_seen);
CREATE INDEX IF NOT EXISTS idx_jobs_source    ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_jobs_merged    ON jobs(merged_into);
CREATE INDEX IF NOT EXISTS idx_jobs_triage    ON jobs(status, fit_score DESC, date_seen DESC);

-- Positive rules. Unlike rejected_patterns these DEMOTE rather than reject, and
-- because off_profile is derived they can be re-evaluated against existing rows.
CREATE TABLE IF NOT EXISTS profile_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_type  TEXT NOT NULL,
    value      TEXT NOT NULL,        -- lowercased and stripped
    reason     TEXT,
    created_at TEXT NOT NULL,

    CHECK (rule_type IN ('title_include', 'title_exclude')),
    CHECK (length(trim(value)) > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_rules_unique
    ON profile_rules(rule_type, value);

-- Small key/value store for the server's own bookkeeping. Currently: the
-- high-water mark of the last triage run, per mailbox.
CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL
);
"""

# Unscored jobs sort last: SQLite has no NULLS LAST. The trailing id makes the
# order total so pagination cannot skip or repeat a row.
ORDER_BY = "ORDER BY (fit_score IS NULL), fit_score DESC, date_seen DESC, id ASC"

# Merged duplicates are hidden from every listing. get_job(id) still returns
# them, so a stale id remains inspectable rather than 404ing.
LIVE = "merged_into IS NULL"

JOB_COLUMNS = ", ".join(JOB_FIELDS)


def today_str() -> str:
    return date.today().isoformat()


def now_str() -> str:
    # .astimezone() with no argument attaches the local zone, preserving offset.
    return datetime.now().astimezone().isoformat(timespec="seconds")


def like_escape(term: str) -> str:
    """Escape LIKE wildcards so a search for '100%' does not match everything."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _err(msg: str) -> dict:
    return {"error": msg}


class Database:
    def __init__(self, db_path: str | os.PathLike | None = None):
        self.path = Path(
            db_path
            or os.environ.get("JOB_TRACKER_DB")
            or Path(__file__).resolve().parent.parent / "data" / "jobs.db"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    generate_job_id = staticmethod(generate_job_id)

    # ---------------------------------------------------------------- setup

    def _apply_pragmas(self) -> None:
        with self._lock:
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA busy_timeout = 5000")
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA synchronous = NORMAL")

    def _init_schema(self) -> None:
        with self._lock, self.conn:
            current = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if current == 0:
                self.conn.executescript(SCHEMA_SQL)
                self.conn.executescript(ONENOTE_SCHEMA_SQL)
                self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif current < SCHEMA_VERSION:
                self._migrate(current, SCHEMA_VERSION)
            elif current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema v{current} is newer than this code "
                    f"(v{SCHEMA_VERSION}). Refusing to open {self.path}."
                )

    def _has_column(self, table: str, column: str) -> bool:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r[1] == column for r in rows)

    def _migrate(self, frm: int, to: int) -> None:
        """Called from inside _init_schema's lock and transaction. Do not re-enter either.

        NOT ATOMIC. sqlite3.executescript() issues an implicit COMMIT before it
        runs, so DDL lands before the user_version bump. A crash in that gap
        leaves the new shape with the old version, and the next open re-runs the
        step. Safety therefore rests on EVERY STEP BEING IDEMPOTENT, not on
        transactions -- which is why the v3 step guards its ALTER by hand:
        `CREATE TABLE IF NOT EXISTS` is re-runnable, `ADD COLUMN` is not.
        """
        steps = {1: self._migrate_1_to_2, 2: self._migrate_2_to_3,
                 3: self._migrate_3_to_4, 4: self._migrate_4_to_5,
                 5: self._rebuild_ids, 6: self._rebuild_ids,
                 7: self._rebuild_ids, 8: self._migrate_8_to_9,
                 9: self._migrate_9_to_10, 10: self._migrate_10_to_11}
        for version in range(frm, to):
            step = steps.get(version)
            if step is None:
                raise NotImplementedError(f"No migration path from v{version} to v{version + 1}")
            step()
            self.conn.execute(f"PRAGMA user_version = {version + 1}")
            log.info("migrated database %s from schema v%d to v%d",
                     self.path, version, version + 1)

    def _migrate_1_to_2(self) -> None:
        self.conn.executescript(ONENOTE_SCHEMA_SQL)     # purely additive tables

    def _migrate_2_to_3(self) -> None:
        # Graph returns neither `level` nor `order` on pages, so the section's
        # visual sequence (from $orderby=order) is the only way to recover the
        # page hierarchy. Persist it.
        if not self._has_column("onenote_pages", "page_order"):
            self.conn.execute("ALTER TABLE onenote_pages ADD COLUMN page_order INTEGER")

    def _migrate_3_to_4(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS app_state ("
            "  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT NOT NULL)"
        )

    def _migrate_4_to_5(self) -> None:
        if not self._has_column("jobs", "off_profile"):
            # NOT NULL with a constant default is legal for ADD COLUMN; existing
            # rows become on-profile, which is right: there are no rules yet.
            self.conn.execute(
                "ALTER TABLE jobs ADD COLUMN off_profile INTEGER NOT NULL DEFAULT 0")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS profile_rules ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  rule_type TEXT NOT NULL, value TEXT NOT NULL,"
            "  reason TEXT, created_at TEXT NOT NULL,"
            "  CHECK (rule_type IN ('title_include', 'title_exclude')),"
            "  CHECK (length(trim(value)) > 0))"
        )
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_rules_unique"
            "  ON profile_rules(rule_type, value)")

    def _migrate_8_to_9(self) -> None:
        """Salary columns. Purely additive; no id changes, since salary is not
        part of the key -- a posting can restate its range between sightings."""
        for col, typ in (("salary_min", "INTEGER"), ("salary_max", "INTEGER"),
                         ("salary_period", "TEXT"), ("salary_annual_min", "INTEGER"),
                         ("salary_annual_max", "INTEGER"), ("salary_raw", "TEXT")):
            if not self._has_column("jobs", col):
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")

    def _migrate_9_to_10(self) -> None:
        """Job-description cache columns. Purely additive; not part of the key."""
        for col in ("description TEXT", "description_fetched_at TEXT"):
            name = col.split()[0]
            if not self._has_column("jobs", name):
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col}")

    def _migrate_10_to_11(self) -> None:
        if not self._has_column("jobs", "jd_reviewed_at"):
            self.conn.execute("ALTER TABLE jobs ADD COLUMN jd_reviewed_at TEXT")

    def _rebuild_ids(self) -> None:
        """Recompute every id from the current canonicalizers.

        Run whenever canonicalize_company/title/location changes -- the id is a
        hash of all three, and models.py warns that editing them is a data
        migration every time. Idempotent: a row whose id already matches is
        skipped, so a crash between the DDL and the version bump is harmless.
        """
        rows = self.conn.execute(
            "SELECT id, company, title, location FROM jobs").fetchall()
        renames = {}
        for r in rows:
            new = generate_job_id(r["company"], r["title"], r["location"])
            if new != r["id"]:
                renames[r["id"]] = new
        if not renames:
            return

        existing = {r["id"] for r in rows}
        survivors = existing - set(renames)
        for old, new in renames.items():
            if new in survivors or list(renames.values()).count(new) > 1:
                raise RuntimeError(
                    f"Rebuilding ids would collide on {new} (from {old}). Two rows "
                    f"now canonicalize identically. Resolve by hand with merge_jobs "
                    f"before upgrading; refusing to guess which row survives."
                )

        # PRAGMA foreign_keys is a no-op inside a transaction; defer_foreign_keys
        # is not. FKs are then checked once, at COMMIT.
        self.conn.execute("PRAGMA defer_foreign_keys = ON")
        for old, new in renames.items():
            self.conn.execute("UPDATE onenote_links SET job_id = ? WHERE job_id = ?", (new, old))
            self.conn.execute("UPDATE jobs SET merged_into = ? WHERE merged_into = ?", (new, old))
            self.conn.execute("UPDATE jobs SET id = ? WHERE id = ?", (new, old))
        log.info("rebuilt %d job id(s) after a canonicalization change", len(renames))

    # ----------------------------------------------------------- validation

    @staticmethod
    def _bad_fit_score(fit_score) -> str | None:
        if fit_score is None:
            return None
        if not isinstance(fit_score, int) or isinstance(fit_score, bool):
            return f"fit_score must be an integer 1-10, got {fit_score!r}"
        if not 1 <= fit_score <= 10:
            return f"fit_score must be between 1 and 10, got {fit_score}"
        return None

    @staticmethod
    def _bad_status(status: str) -> str | None:
        if status not in STATUSES:
            return f"Invalid status {status!r}. Must be one of: {', '.join(STATUSES)}"
        return None

    @staticmethod
    def _bad_date(value: str) -> str | None:
        try:
            date.fromisoformat(value)
        except (ValueError, TypeError):
            return f"Invalid date {value!r}. Expected ISO format YYYY-MM-DD."
        return None

    @staticmethod
    def _clamp(limit: int | None, default: int) -> int:
        if limit is None:
            return default
        return max(1, min(int(limit), MAX_LIMIT))

    @staticmethod
    def _merged_err(row) -> str:
        return (
            f"Job {row['id']} was merged into {row['merged_into']}. "
            f"Operate on {row['merged_into']} instead, or call "
            f"unmerge_job('{row['id']}') first."
        )

    # ------------------------------------------------------------- patterns

    def _load_patterns(self, cur) -> list[tuple[str, str]]:
        rows = cur.execute(
            "SELECT pattern_type, pattern_value FROM rejected_patterns"
        ).fetchall()
        return [(r["pattern_type"], r["pattern_value"]) for r in rows]

    @staticmethod
    def _match_patterns(
        patterns: list[tuple[str, str]], company: str, title: str, source: str | None
    ) -> list[str]:
        """Company/keyword match on substring; source matches exactly.

        Company and title are compared in canonical form, so a pattern learned
        from "Acme, Inc." also filters "Acme Corporation". Sources come from a
        small closed set where substring matching would surprise: the pattern
        "dice" must not match the source "Dice Inc".
        """
        c = canonicalize_company(company)
        t = canonicalize_title(title)
        s = normalize_key_part(source)
        matched: list[str] = []
        for ptype, pval in patterns:
            if ptype == "company" and pval in c:
                matched.append(f"company:{pval}")
            elif ptype == "title_keyword" and pval in t:
                matched.append(f"title_keyword:{pval}")
            elif ptype == "source" and s and pval == s:
                matched.append(f"source:{pval}")
        return matched

    # ------------------------------------------------------- profile rules

    def _load_profile_rules(self, cur) -> tuple[list[str], list[str]]:
        rows = cur.execute("SELECT rule_type, value FROM profile_rules").fetchall()
        includes = [r["value"] for r in rows if r["rule_type"] == "title_include"]
        excludes = [r["value"] for r in rows if r["rule_type"] == "title_exclude"]
        return includes, excludes

    def rebuild_ids(self) -> dict:
        """Recompute every id from the current canonicalizers. Used by migrations
        and available after a deliberate canonicalization change."""
        with self._lock, self.conn:
            before = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            self._rebuild_ids()
            after = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        return {"result": "rebuilt", "jobs": after, "lost": before - after}

    def _recompute_profiles(self, cur) -> int:
        """Re-derive off_profile for every live job. Never touches status.

        off_profile is DERIVED data, not a decision, which is exactly why this
        sweep is safe and why rejected_patterns has no equivalent: rejecting is
        a decision, and a retroactive one could bury a job you applied to.
        """
        includes, excludes = self._load_profile_rules(cur)
        changed = 0
        for row in cur.execute(
                f"SELECT id, title_canonical, off_profile FROM jobs WHERE {LIVE}").fetchall():
            off, _ = evaluate_profile(row["title_canonical"], includes, excludes)
            if bool(row["off_profile"]) != off:
                cur.execute("UPDATE jobs SET off_profile = ? WHERE id = ?",
                            (1 if off else 0, row["id"]))
                changed += 1
        return changed

    def get_profile_rules(self) -> dict:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, rule_type, value, reason, created_at FROM profile_rules "
                "ORDER BY rule_type, value").fetchall()
            off = self.conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE off_profile = 1 AND {LIVE}").fetchone()[0]
        return {"rules": [dict(r) for r in rows], "total": len(rows),
                "off_profile_jobs": off,
                "message": ("No rules: every job is on-profile."
                            if not rows else
                            f"{off} live job(s) are currently off-profile.")}

    @staticmethod
    def _unmatchable(value: str, canonicalizer) -> str | None:
        """A rule that the canonicalizer would rewrite can never match.

        Rules are compared against the CANONICAL company/title, which has already
        had legal suffixes, "(Remote)", salary ranges and req ids stripped. A rule
        written as "director (remote)" installs cleanly, reports success, and
        matches zero jobs forever. Refuse it, and say what to write instead.
        """
        canon = canonicalizer(value)
        if canon != value:
            return (f"The rule {value!r} can never match: titles and companies are "
                    f"compared in canonical form, where it becomes {canon!r}. "
                    f"Decorations like '(Remote)', '[R-1234]', salary ranges and "
                    f"legal suffixes (Inc, LLC, Corp) are stripped before matching. "
                    f"Use {canon!r} instead.")
        return None

    def add_profile_rule(self, rule_type: str, value: str,
                         reason: str | None = None) -> dict:
        if rule_type not in PROFILE_RULE_TYPES:
            return _err(f"Invalid rule_type {rule_type!r}. "
                        f"Must be one of: {', '.join(PROFILE_RULE_TYPES)}")
        v = normalize_key_part(value)
        if not v:
            return _err("value must not be empty")
        if msg := self._unmatchable(v, canonicalize_title):
            return _err(msg)

        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO profile_rules (rule_type, value, reason, created_at) "
                "VALUES (?, ?, ?, ?)", (rule_type, v, reason, now_str()))
            added = bool(cur.rowcount)
            rid = cur.lastrowid if added else self.conn.execute(
                "SELECT id FROM profile_rules WHERE rule_type = ? AND value = ?",
                (rule_type, v)).fetchone()["id"]
            changed = self._recompute_profiles(self.conn)

        return {"result": "added" if added else "exists", "id": rid,
                "reclassified": changed,
                "message": (f"Rule {rule_type} = {v}. {changed} existing job(s) "
                            f"reclassified. Nothing was rejected -- off-profile jobs "
                            f"are hidden from the default list, not removed.")}

    def remove_profile_rule(self, id: int) -> dict:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT rule_type, value FROM profile_rules WHERE id = ?", (id,)).fetchone()
            if row is None:
                return _err(f"No profile rule with id {id}")
            self.conn.execute("DELETE FROM profile_rules WHERE id = ?", (id,))
            changed = self._recompute_profiles(self.conn)
        return {"result": "removed", "id": id, "reclassified": changed,
                "message": f"Rule removed: {row['rule_type']} = {row['value']}. "
                           f"{changed} job(s) reclassified."}

    def recompute_profiles(self) -> dict:
        with self._lock, self.conn:
            changed = self._recompute_profiles(self.conn)
            off = self.conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE off_profile = 1 AND {LIVE}").fetchone()[0]
        return {"result": "recomputed", "reclassified": changed, "off_profile_jobs": off}

    def _check_rejected_patterns(
        self, company: str, title: str, source: str | None
    ) -> list[str]:
        with self._lock:
            return self._match_patterns(
                self._load_patterns(self.conn), company, title, source
            )

    # ------------------------------------------------------------ near-dups

    @staticmethod
    def _title_similarity(a: str, b: str) -> float:
        """Jaccard overlap of the word sets, punctuation ignored.

        Used by BOTH Part 1's duplicate hints and Part 2's reconciliation ladder.
        Changing it changes which OneNote pages match which jobs. Do not swap in
        a character-similarity metric: SequenceMatcher scores "staff engineer" vs
        "senior staff engineer" (0.80, different level) above "staff engineer" vs
        "staff software engineer" (0.76, same job). It inverts the answer.
        """
        ta = {w.strip(".,()[]/") for w in a.split()} - {""}
        tb = {w.strip(".,()[]/") for w in b.split()} - {""}
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / len(ta | tb)

    def _find_similar(self, cur, company_canonical: str, title_canonical: str,
                      exclude_id: str) -> list[dict]:
        """Titles at the same employer that are close but not identical.

        A hint for the caller to adjudicate with merge_jobs. Never acted on
        automatically. Scoped to one employer, so this stays cheap.
        """
        rows = cur.execute(
            f"SELECT id, company, title, title_canonical, status FROM jobs "
            f"WHERE company_canonical = ? AND id != ? AND {LIVE}",
            (company_canonical, exclude_id),
        ).fetchall()
        out = []
        for r in rows:
            score = self._title_similarity(title_canonical, r["title_canonical"])
            if score >= DUP_THRESHOLD:
                out.append({"id": r["id"], "company": r["company"],
                            "title": r["title"], "status": r["status"],
                            "score": round(score, 2)})
        return sorted(out, key=lambda d: -d["score"])[:MAX_DUP_HINTS]

    # ------------------------------------------------------------- add jobs

    def _resolve(self, cur, job_id: str):
        """Fetch a row, following a merge pointer to the surviving row."""
        row = cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is not None and row["merged_into"]:
            row = cur.execute(
                "SELECT * FROM jobs WHERE id = ?", (row["merged_into"],)
            ).fetchone()
        return row

    def _add_one(self, cur, patterns, profile_rules, entry: dict) -> dict:
        """Insert or record a re-sighting. Assumes an open transaction."""
        company = (entry.get("company") or "").strip()
        title = (entry.get("title") or "").strip()
        if not company:
            return {"result": "error", "message": "company is required"}
        if not title:
            return {"result": "error", "message": "title is required"}

        location = (entry.get("location") or "").strip() or "Unknown"
        fit_score = entry.get("fit_score")
        if msg := self._bad_fit_score(fit_score):
            return {"result": "error", "message": msg}

        # When the posting appeared, NOT when triage happened. Importing three
        # weeks of alert emails must not stamp every job with today's date.
        seen = entry.get("date_seen")
        if seen is not None and (msg := self._bad_date(seen)):
            return {"result": "error", "message": msg}
        seen = seen or today_str()

        url = entry.get("url")
        source = entry.get("source")
        notes = entry.get("notes")
        email_subject = entry.get("email_subject")
        sal = parse_salary(entry.get("salary"))

        c_canon = canonicalize_company(company)
        t_canon = canonicalize_title(title)
        l_canon = canonicalize_location(location)
        job_id = generate_job_id(company, title, location)
        today, now = today_str(), now_str()

        row = self._resolve(cur, job_id)
        if row is not None:
            # Backfill nulls only. A re-sighting must never clobber a triage
            # decision (status, fit_score, notes) or the display strings.
            #
            # "First appeared" must never move forward, and "last seen" must
            # never move backward -- alert emails do not arrive in date order,
            # and a backlog import replays them in whatever order you read them.
            first = min(row["date_seen"], seen)
            last = max(row["last_seen"], seen)
            # Backfill only: a later sighting may carry a url or a salary the
            # first one lacked, but must never overwrite what is already known.
            cur.execute(
                "UPDATE jobs SET date_seen = ?, last_seen = ?, "
                "times_seen = times_seen + 1, url = ?, source = ?, "
                "salary_min = COALESCE(salary_min, ?), "
                "salary_max = COALESCE(salary_max, ?), "
                "salary_period = COALESCE(salary_period, ?), "
                "salary_annual_min = COALESCE(salary_annual_min, ?), "
                "salary_annual_max = COALESCE(salary_annual_max, ?), "
                "salary_raw = COALESCE(salary_raw, ?), "
                "updated_at = ? WHERE id = ?",
                (first, last, row["url"] or url, row["source"] or source,
                 sal and sal["min"], sal and sal["max"], sal and sal["period"],
                 sal and sal["annual_min"], sal and sal["annual_max"],
                 sal and sal["raw"], now, row["id"]),
            )
            return {
                "result": "duplicate",
                "id": row["id"],
                "company": company,
                "title": title,
                "status": row["status"],
                "date_seen": first,
                "date_applied": row["date_applied"],
                "times_seen": row["times_seen"] + 1,
                "message": f"Already tracked: {row['title']} at {row['company']}",
            }

        matched = self._match_patterns(patterns, company, title, source)
        status = "rejected" if matched else "new"
        if matched:
            auto = f"Auto-rejected: matched {', '.join(matched)}"
            notes = f"{notes}\n{auto}" if notes else auto

        # Positive rules DEMOTE. A non-match is hidden from the default list,
        # never rejected: the good jobs are the ones with unusual titles.
        includes, excludes = profile_rules
        off_profile, profile_reasons = evaluate_profile(t_canon, includes, excludes)

        cur.execute(
            "INSERT INTO jobs (id, company, company_canonical, title, title_canonical, "
            "location, location_canonical, url, source, date_seen, last_seen, "
            "times_seen, date_applied, status, fit_score, salary_min, salary_max, "
            "salary_period, salary_annual_min, salary_annual_max, salary_raw, "
            "notes, email_subject, merged_into, off_profile, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (job_id, company, c_canon, title, t_canon, location, l_canon, url, source,
             seen, seen, status, fit_score,
             sal and sal["min"], sal and sal["max"], sal and sal["period"],
             sal and sal["annual_min"], sal and sal["annual_max"], sal and sal["raw"],
             notes, email_subject, 1 if off_profile else 0, now, now),
        )
        result = {
            "result": "created",
            "id": job_id,
            "company": company,
            "title": title,
            "status": status,
            "auto_rejected": bool(matched),
            "matched_patterns": matched,
            "off_profile": off_profile,
            "profile_reasons": profile_reasons,
            "message": f"Job added: {title} at {company}",
        }
        similar = self._find_similar(cur, c_canon, t_canon, job_id)
        if similar:
            result["possible_duplicates"] = similar
            result["message"] += (
                f" -- {len(similar)} similar job(s) at this company; "
                f"call merge_jobs if any is the same requisition."
            )
        return result

    def add_job(
        self,
        company: str,
        title: str,
        location: str,
        url: str | None = None,
        source: str | None = None,
        fit_score: int | None = None,
        notes: str | None = None,
        email_subject: str | None = None,
        date_seen: str | None = None,
        salary: str | None = None,
    ) -> dict:
        entry = dict(
            company=company, title=title, location=location, url=url,
            source=source, fit_score=fit_score, notes=notes,
            email_subject=email_subject, date_seen=date_seen, salary=salary,
        )
        with self._lock, self.conn:
            cur = self.conn.cursor()
            result = self._add_one(cur, self._load_patterns(cur),
                                   self._load_profile_rules(cur), entry)
        if result["result"] == "error":
            return _err(result["message"])
        return result

    def bulk_add_jobs(self, jobs: list[dict]) -> dict:
        if not isinstance(jobs, list):
            return _err("jobs must be a list of job objects")
        if not jobs:
            return _err("jobs must not be empty")
        if len(jobs) > MAX_BULK:
            return _err(f"Too many jobs ({len(jobs)}); send at most {MAX_BULK} per call")

        results: list[dict] = []
        with self._lock, self.conn:
            cur = self.conn.cursor()
            patterns = self._load_patterns(cur)
            profile_rules = self._load_profile_rules(cur)
            for entry in jobs:
                if not isinstance(entry, dict):
                    results.append({"result": "error", "message": f"not an object: {entry!r}"})
                    continue
                results.append(self._add_one(cur, patterns, profile_rules, entry))

        return {
            "created": sum(r["result"] == "created" for r in results),
            "duplicates": sum(r["result"] == "duplicate" for r in results),
            "auto_rejected": sum(r.get("auto_rejected", False) for r in results),
            "off_profile": sum(r.get("off_profile", False) for r in results),
            "errors": sum(r["result"] == "error" for r in results),
            "possible_duplicates": sum("possible_duplicates" in r for r in results),
            "results": results,
        }

    # ----------------------------------------------------------------- read

    def check_job(self, company: str, title: str, location: str) -> dict:
        job_id = generate_job_id(company, title, location)
        with self._lock:
            row = self._resolve(self.conn, job_id)
        if row is None:
            return {"exists": False, "id": job_id}
        return {"exists": True, "id": row["id"], "status": row["status"],
                "date_seen": row["date_seen"], "last_seen": row["last_seen"],
                "times_seen": row["times_seen"], "date_applied": row["date_applied"]}

    def get_job(self, id: str) -> dict:
        with self._lock:
            row = self.conn.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = ?", (id,)
            ).fetchone()
        if row is None:
            return _err(f"No job with id {id}")
        return row_to_job(row)

    def get_jobs(
        self,
        status: str | None = None,
        source: str | None = None,
        min_fit_score: int | None = None,
        limit: int | None = 50,
        offset: int | None = 0,
        include_off_profile: bool = False,
        min_salary: int | None = None,
    ) -> dict:
        where, params = [LIVE], []
        if not include_off_profile:
            # Hide off-profile jobs from the TRIAGE list only. A job you applied
            # to is history, not a candidate, and must never vanish because you
            # later narrowed your profile rules.
            where.append("(off_profile = 0 OR status != 'new')")
        if status is not None:
            if msg := self._bad_status(status):
                return _err(msg)
            where.append("status = ?")
            params.append(status)
        if source is not None:
            where.append("LOWER(source) = LOWER(?)")
            params.append(source)
        if min_fit_score is not None:
            if msg := self._bad_fit_score(min_fit_score):
                return _err(msg)
            # An unscored job cannot be said to meet a threshold.
            where.append("fit_score IS NOT NULL AND fit_score >= ?")
            params.append(min_fit_score)
        if min_salary is not None:
            # Compare on the annual figure so an hourly posting is comparable.
            # A job with no stated salary cannot meet a threshold.
            where.append("salary_annual_max IS NOT NULL AND salary_annual_max >= ?")
            params.append(int(min_salary))

        clause = f"WHERE {' AND '.join(where)}"
        limit = self._clamp(limit, 50)
        offset = max(0, int(offset or 0))

        with self._lock:
            total = self.conn.execute(
                f"SELECT COUNT(*) FROM jobs {clause}", params
            ).fetchone()[0]
            rows = self.conn.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs {clause} {ORDER_BY} LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()

        return {"jobs": [row_to_job(r) for r in rows], "total": total,
                "offset": offset, "limit": limit,
                "include_off_profile": include_off_profile}

    def search_jobs(
        self,
        query: str,
        status: str | None = None,
        limit: int | None = 20,
        offset: int | None = 0,
    ) -> dict:
        if not query or not query.strip():
            return _err("query must not be empty")
        if status is not None and (msg := self._bad_status(status)):
            return _err(msg)

        pattern = f"%{like_escape(query.strip())}%"
        where = [
            LIVE,
            "(LOWER(company) LIKE LOWER(?) ESCAPE '\\'"
            " OR LOWER(title) LIKE LOWER(?) ESCAPE '\\'"
            " OR LOWER(COALESCE(notes, '')) LIKE LOWER(?) ESCAPE '\\'"
            " OR LOWER(COALESCE(email_subject, '')) LIKE LOWER(?) ESCAPE '\\')",
        ]
        params: list = [pattern] * 4
        if status is not None:
            where.append("status = ?")
            params.append(status)

        clause = f"WHERE {' AND '.join(where)}"
        limit = self._clamp(limit, 20)
        offset = max(0, int(offset or 0))

        with self._lock:
            total = self.conn.execute(
                f"SELECT COUNT(*) FROM jobs {clause}", params
            ).fetchone()[0]
            rows = self.conn.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs {clause} {ORDER_BY} LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()

        return {"jobs": [row_to_job(r) for r in rows], "total": total,
                "offset": offset, "limit": limit}

    # ------------------------------------------------------ triage watermark

    @staticmethod
    def _watermark_key(account: str, mailbox: str) -> str:
        return f"triage:{normalize_key_part(account)}:{normalize_key_part(mailbox)}"

    @staticmethod
    def _bad_datetime(value: str) -> str | None:
        try:
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return (f"Invalid datetime {value!r}. Expected ISO 8601, e.g. "
                    f"'2026-07-09T17:33:43-05:00'.")
        return None

    def get_triage_state(self, account: str, mailbox: str) -> dict:
        """Where the last triage run stopped, for this mailbox."""
        with self._lock:
            row = self.conn.execute(
                "SELECT value, updated_at FROM app_state WHERE key = ?",
                (self._watermark_key(account, mailbox),),
            ).fetchone()
        if row is None:
            return {"account": account, "mailbox": mailbox,
                    "last_message_dt": None, "has_run": False,
                    "message": "No triage run recorded for this mailbox. Process "
                               "every message; there is no watermark yet."}
        state = json.loads(row["value"])
        return {"account": account, "mailbox": mailbox, "has_run": True,
                **state, "recorded_at": row["updated_at"]}

    def record_triage_run(self, account: str, mailbox: str, last_message_dt: str,
                          emails_processed: int = 0, postings_seen: int = 0) -> dict:
        """Advance the watermark to the newest message ACTUALLY processed.

        Two invariants:

        - It never moves backward. Re-running an older slice of mail must not
          erase progress.
        - It is the newest message *processed*, never "now". Mail arriving
          mid-run has a later date_received and must be caught next time, not
          skipped because the clock moved past it.

        Overlap is safe, gaps are not: the next run re-reads the watermark
        message inclusively. add_job is idempotent and a re-sighting keeps the
        earliest date_seen, so reprocessing costs nothing.
        """
        if msg := self._bad_datetime(last_message_dt):
            return _err(msg)

        def inst(s):
            # ISO 8601 with offsets does not sort lexically across zones.
            return datetime.fromisoformat(s.replace("Z", "+00:00"))

        key = self._watermark_key(account, mailbox)
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT value FROM app_state WHERE key = ?", (key,)
            ).fetchone()
            prev = json.loads(row["value"]) if row else {}
            previous_dt = prev.get("last_message_dt")

            newest = last_message_dt
            if previous_dt and inst(previous_dt) > inst(last_message_dt):
                newest = previous_dt

            state = {
                "last_message_dt": newest,
                "last_run_at": now_str(),
                "emails_processed": emails_processed,
                "postings_seen": postings_seen,
                "total_runs": prev.get("total_runs", 0) + 1,
            }
            self.conn.execute(
                "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, json.dumps(state), now_str()),
            )

        advanced = newest != previous_dt
        return {"result": "recorded", **state,
                "message": (f"Watermark {'advanced to' if advanced else 'held at'} "
                            f"{newest}. Next run should read messages with "
                            f"date_received >= {newest} (inclusive; duplicates are free).")}

    # ------------------------------------------------ reconciliation reads

    def live_jobs(self) -> list[dict]:
        """Every non-merged job, for the reconciler. Merged rows are hidden.

        Matching a OneNote page to a merged job would produce a proposed_action
        that mark_applied then refuses.
        """
        with self._lock:
            rows = self.conn.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs WHERE {LIVE}"
            ).fetchall()
        return [row_to_job(r) for r in rows]

    def merged_into(self, job_id: str) -> str | None:
        """The surviving job id, or None if this job is live (or absent)."""
        with self._lock:
            row = self.conn.execute(
                "SELECT merged_into FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return row["merged_into"] if row else None

    # --------------------------------------------------------------- merges

    def merge_jobs(self, keep_id: str, duplicate_id: str) -> dict:
        """Point duplicate_id at keep_id. Nothing is deleted."""
        if keep_id == duplicate_id:
            return _err("keep_id and duplicate_id are the same job")

        with self._lock, self.conn:
            keep = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (keep_id,)).fetchone()
            dup = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (duplicate_id,)).fetchone()
            if keep is None:
                return _err(f"No job with id {keep_id}")
            if dup is None:
                return _err(f"No job with id {duplicate_id}")
            if keep["merged_into"]:
                return _err(
                    f"keep_id {keep_id} is itself merged into {keep['merged_into']}; "
                    f"merge into the surviving job instead"
                )
            if dup["merged_into"]:
                return _err(f"Job {duplicate_id} is already merged into {dup['merged_into']}")

            # Never lose an application. If only the duplicate was applied to,
            # the survivor adopts that fact.
            status, date_applied = keep["status"], keep["date_applied"]
            if dup["status"] == "applied" and keep["date_applied"] is None:
                if "applied" in ALLOWED_TRANSITIONS[keep["status"]]:
                    status, date_applied = "applied", dup["date_applied"]

            notes = keep["notes"]
            if dup["notes"]:
                merged_note = f"[merged from {duplicate_id}] {dup['notes']}"
                notes = f"{notes}\n{merged_note}" if notes else merged_note

            self.conn.execute(
                "UPDATE jobs SET date_seen = MIN(date_seen, ?), "
                "last_seen = MAX(last_seen, ?), times_seen = times_seen + ?, "
                "url = COALESCE(url, ?), source = COALESCE(source, ?), "
                "fit_score = COALESCE(fit_score, ?), status = ?, date_applied = ?, "
                "notes = ?, updated_at = ? WHERE id = ?",
                (dup["date_seen"], dup["last_seen"], dup["times_seen"], dup["url"],
                 dup["source"], dup["fit_score"], status, date_applied, notes,
                 now_str(), keep_id),
            )
            # Re-point anything that had been merged into the duplicate, so
            # merge pointers never form a chain.
            self.conn.execute(
                "UPDATE jobs SET merged_into = ? WHERE merged_into = ?",
                (keep_id, duplicate_id),
            )
            # Carry OneNote evidence across. Without this, a page linked to the
            # duplicate hangs off a row that no listing will ever return, and
            # reconciliation proposes a mark_applied that Part 1 then refuses.
            # OR IGNORE handles a page already linked to BOTH rows, which would
            # otherwise violate PRIMARY KEY(page_id, job_id); the DELETE reaps
            # exactly those skipped rows, losing no evidence.
            self.conn.execute(
                "UPDATE OR IGNORE onenote_links SET job_id = ? WHERE job_id = ?",
                (keep_id, duplicate_id),
            )
            self.conn.execute(
                "DELETE FROM onenote_links WHERE job_id = ?", (duplicate_id,)
            )
            self.conn.execute(
                "UPDATE jobs SET merged_into = ?, updated_at = ? WHERE id = ?",
                (keep_id, now_str(), duplicate_id),
            )

        return {
            "result": "merged", "keep_id": keep_id, "duplicate_id": duplicate_id,
            "message": (
                f"Merged {dup['title']} at {dup['company']} into {keep['title']} "
                f"at {keep['company']}. The duplicate row is hidden, not deleted; "
                f"unmerge_job('{duplicate_id}') restores it."
            ),
        }

    def unmerge_job(self, id: str) -> dict:
        """Restore a merged duplicate to its own row."""
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT merged_into FROM jobs WHERE id = ?", (id,)
            ).fetchone()
            if row is None:
                return _err(f"No job with id {id}")
            if not row["merged_into"]:
                return _err(f"Job {id} is not merged")
            self.conn.execute(
                "UPDATE jobs SET merged_into = NULL, updated_at = ? WHERE id = ?",
                (now_str(), id),
            )
        return {
            "result": "unmerged", "id": id,
            "message": (
                f"Job {id} restored. Its own fields were never modified, but the "
                f"survivor {row['merged_into']} keeps the times_seen and notes it "
                f"absorbed at merge time, and keeps any OneNote links that moved "
                f"across -- those evidence pages describe the surviving job."
            ),
        }

    # --------------------------------------------------------------- mutate

    def mark_applied(
        self, id: str, date_applied: str | None = None, notes: str | None = None
    ) -> dict:
        if date_applied is not None and (msg := self._bad_date(date_applied)):
            return _err(msg)

        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT id, company, title, status, notes, merged_into "
                "FROM jobs WHERE id = ?", (id,)
            ).fetchone()
            if row is None:
                return _err(f"No job with id {id}")
            if row["merged_into"]:
                return _err(self._merged_err(row))
            if "applied" not in ALLOWED_TRANSITIONS[row["status"]]:
                return _err(
                    f"Invalid status transition: {row['status']} -> applied. "
                    f"Reopen it first with update_job(id, status='new')."
                )

            new_notes = row["notes"]
            if notes:
                new_notes = f"{new_notes}\n{notes}" if new_notes else notes

            self.conn.execute(
                "UPDATE jobs SET status = 'applied', date_applied = ?, notes = ?, "
                "updated_at = ? WHERE id = ?",
                (date_applied or today_str(), new_notes, now_str(), id),
            )
        return {"result": "updated", "id": id,
                "message": f"Marked as applied: {row['title']} at {row['company']}"}

    def reject_job(
        self,
        id: str,
        reason: str | None = None,
        add_company_pattern: bool = False,
        add_keyword_pattern: str | None = None,
        add_source_pattern: bool = False,
    ) -> dict:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT id, company, company_canonical, title, source, status, "
                "notes, merged_into FROM jobs WHERE id = ?", (id,),
            ).fetchone()
            if row is None:
                return _err(f"No job with id {id}")
            if row["merged_into"]:
                return _err(self._merged_err(row))
            if "rejected" not in ALLOWED_TRANSITIONS[row["status"]]:
                return _err(f"Invalid status transition: {row['status']} -> rejected")

            new_notes = row["notes"]
            if reason:
                new_notes = f"{new_notes}\n{reason}" if new_notes else reason

            self.conn.execute(
                "UPDATE jobs SET status = 'rejected', notes = ?, updated_at = ? "
                "WHERE id = ?",
                (new_notes, now_str(), id),
            )

            added, existing, warnings = [], [], []
            wanted: list[tuple[str, str | None]] = []
            if add_company_pattern:
                wanted.append(("company", row["company_canonical"]))
            if add_keyword_pattern:
                wanted.append(("title_keyword", add_keyword_pattern))
            if add_source_pattern:
                if row["source"]:
                    wanted.append(("source", row["source"]))
                else:
                    warnings.append("job has no source, so no source pattern was added")

            for ptype, pval in wanted:
                value = normalize_key_part(pval)
                if not value:
                    continue
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO rejected_patterns "
                    "(pattern_type, pattern_value, reason, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (ptype, value, reason, now_str()),
                )
                (added if cur.rowcount else existing).append(f"{ptype}:{value}")

        parts = [f"Job rejected: {row['title']} at {row['company']}."]
        if added:
            parts.append(f"{len(added)} new pattern(s) added; future matches auto-filter.")
        if existing:
            parts.append(f"{len(existing)} pattern(s) already present.")
        parts.extend(warnings)
        return {"result": "rejected", "id": id, "patterns_added": added,
                "patterns_existing": existing, "message": " ".join(parts)}

    # Fields a caller may patch through update_job / bulk_update_jobs. Company,
    # title and location are excluded on purpose: they are the natural key, and
    # changing one silently orphans the row's id.
    _PATCHABLE = ("fit_score", "notes", "url", "status", "date_applied",
                  "salary", "description")

    def _update_one(self, cur, id: str, patch: dict) -> dict:
        """Apply a partial patch to one job. Assumes an open transaction.

        Only the keys present in `patch` are written; everything else is left
        untouched. Unknown keys are an error, so a typo does not silently no-op.
        """
        unknown = [k for k in patch if k not in self._PATCHABLE]
        if unknown:
            return {"result": "error", "id": id,
                    "message": f"Cannot patch {unknown}; allowed: {', '.join(self._PATCHABLE)}"}
        if not patch:
            return {"result": "error", "id": id, "message": "No fields to update"}
        if "fit_score" in patch and (msg := self._bad_fit_score(patch["fit_score"])):
            return {"result": "error", "id": id, "message": msg}
        if "status" in patch and (msg := self._bad_status(patch["status"])):
            return {"result": "error", "id": id, "message": msg}
        if "date_applied" in patch and patch["date_applied"] is not None \
                and (msg := self._bad_date(patch["date_applied"])):
            return {"result": "error", "id": id, "message": msg}

        row = cur.execute(
            "SELECT id, status, date_applied, merged_into, description "
            "FROM jobs WHERE id = ?", (id,)
        ).fetchone()
        if row is None:
            return {"result": "error", "id": id, "message": f"No job with id {id}"}
        if row["merged_into"]:
            return {"result": "error", "id": id, "message": self._merged_err(row)}

        sets, params, changed = [], [], []
        for col in ("fit_score", "notes", "url"):
            if col in patch:
                sets.append(f"{col} = ?"); params.append(patch[col]); changed.append(col)

        if "salary" in patch:
            sal = parse_salary(patch["salary"])
            for col, val in (("salary_min", sal and sal["min"]),
                             ("salary_max", sal and sal["max"]),
                             ("salary_period", sal and sal["period"]),
                             ("salary_annual_min", sal and sal["annual_min"]),
                             ("salary_annual_max", sal and sal["annual_max"]),
                             ("salary_raw", sal and sal["raw"])):
                sets.append(f"{col} = ?"); params.append(val)
            changed.append("salary")

        if "description" in patch:
            sets.append("description = ?"); params.append(patch["description"])
            sets.append("description_fetched_at = ?"); params.append(now_str())
            changed.append("description")

        new_status = row["status"]
        if "status" in patch:
            s = patch["status"]
            if s not in ALLOWED_TRANSITIONS[row["status"]]:
                return {"result": "error", "id": id,
                        "message": f"Invalid status transition: {row['status']} -> {s}"}
            sets.append("status = ?"); params.append(s); changed.append("status")
            new_status = s
            if s == "applied" and row["date_applied"] is None and "date_applied" not in patch:
                sets.append("date_applied = ?"); params.append(today_str())
                changed.append("date_applied")

        if "date_applied" in patch:
            sets.append("date_applied = ?"); params.append(patch["date_applied"])
            changed.append("date_applied")

        # Scoring a job that has its description IS the JD review. Stamp it, so
        # the review is resumable and JD-scored jobs are distinguishable from
        # title-only ones. A score change without a description (e.g. the initial
        # bootstrap) does not count.
        if "fit_score" in patch and row["description"] and \
                not row["description"].startswith("[posting expired"):
            sets.append("jd_reviewed_at = ?"); params.append(now_str())
            changed.append("jd_reviewed_at")

        sets.append("updated_at = ?"); params.append(now_str())
        params.append(id)
        cur.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)
        return {"result": "updated", "id": id, "changed": changed, "status": new_status}

    def update_job(
        self,
        id: str,
        fit_score: int | None = None,
        notes: str | None = None,
        url: str | None = None,
        status: str | None = None,
        salary: str | None = None,
        date_applied: str | None = None,
        description: str | None = None,
    ) -> dict:
        patch = {k: v for k, v in dict(
            fit_score=fit_score, notes=notes, url=url, status=status,
            salary=salary, date_applied=date_applied, description=description,
        ).items() if v is not None}
        with self._lock, self.conn:
            out = self._update_one(self.conn, id, patch)
        return _err(out["message"]) if out["result"] == "error" else out

    def bulk_update_jobs(self, updates: list[dict]) -> dict:
        """Patch many jobs in ONE call. Each update is {id, ...fields}.

        Exists because Desktop rescoring 84 jobs one update_job at a time is 84
        round-trips, which trips its client-side timeout. A malformed item drops
        to an error in `results` without aborting the rest.
        """
        if not isinstance(updates, list):
            return _err("updates must be a list of {id, ...fields} objects")
        if not updates:
            return _err("updates must not be empty")
        if len(updates) > MAX_BULK:
            return _err(f"Too many updates ({len(updates)}); send at most {MAX_BULK}")

        results = []
        with self._lock, self.conn:
            cur = self.conn.cursor()
            for u in updates:
                if not isinstance(u, dict) or "id" not in u:
                    results.append({"result": "error", "message": f"missing id: {u!r}"})
                    continue
                patch = {k: v for k, v in u.items() if k != "id"}
                results.append(self._update_one(cur, u["id"], patch))
        return {
            "updated": sum(r["result"] == "updated" for r in results),
            "errors": sum(r["result"] == "error" for r in results),
            "results": results,
        }

    def jobs_to_review(self, within_days: int = 14, limit: int = 25) -> dict:
        """Jobs with a real description that have NOT yet been JD-scored.

        The reviewer's worklist: call this, read each description, set a fresh
        fit_score with update_job. Updating the score auto-stamps jd_reviewed_at,
        so the job drops off this list and the review resumes cleanly next call.
        """
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=max(0, int(within_days)))).isoformat()
        limit = self._clamp(limit, 25)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs WHERE {LIVE} AND status = 'new' "
                f"AND description IS NOT NULL AND description NOT LIKE '[posting expired%' "
                f"AND jd_reviewed_at IS NULL AND date_seen >= ? "
                f"ORDER BY fit_score DESC, date_seen DESC LIMIT ?",
                (cutoff, limit)
            ).fetchall()
            total = self.conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {LIVE} AND status='new' "
                f"AND description IS NOT NULL AND description NOT LIKE '[posting expired%' "
                f"AND jd_reviewed_at IS NULL AND date_seen >= ?", (cutoff,)
            ).fetchone()[0]
            reviewed = self.conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE jd_reviewed_at IS NOT NULL "
                "AND merged_into IS NULL"
            ).fetchone()[0]
        return {"jobs": [row_to_job(r) for r in rows], "returned": len(rows),
                "total_unreviewed": total, "already_reviewed": reviewed,
                "message": (f"{total} described job(s) still need a JD-informed score; "
                            f"{reviewed} already reviewed. Read each description and set "
                            f"fit_score with update_job -- that marks it reviewed. Re-run "
                            f"for the next slice.")}

    # ----------------------------------------------------- job descriptions

    def set_job_description(self, id: str, text: str) -> dict:
        """Cache the full posting text an agent fetched from the job's url.

        Written once and read offline thereafter -- the alert email never
        carries the description, and re-fetching a live posting is what this
        cache exists to avoid.
        """
        if not text or not text.strip():
            return _err("text must not be empty")
        with self._lock, self.conn:
            row = self.conn.execute("SELECT merged_into FROM jobs WHERE id = ?", (id,)).fetchone()
            if row is None:
                return _err(f"No job with id {id}")
            if row["merged_into"]:
                return _err(self._merged_err(dict(id=id, merged_into=row["merged_into"])))
            self.conn.execute(
                "UPDATE jobs SET description = ?, description_fetched_at = ?, updated_at = ? "
                "WHERE id = ?", (text, now_str(), now_str(), id))
        return {"result": "stored", "id": id, "chars": len(text)}

    def jobs_needing_description(self, within_days: int = 14, min_fit: int = 7,
                                limit: int = 25) -> dict:
        """The fetch worklist: recent, promising, fetchable, not-yet-cached jobs.

        Bounded to the last `within_days` because a posting older than that is
        likely expired and is kept only for dedup. Ordered ROUND-ROBIN across
        job boards so an agent fetching the list never hits the same host twice
        in a row -- the throttle is baked into the order, no sleeping required.
        """
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=max(0, int(within_days)))).isoformat()
        limit = self._clamp(limit, 25)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT id, company, title, location, source, url, fit_score, date_seen, "
                f"salary_raw FROM jobs WHERE {LIVE} AND status = 'new' "
                f"AND description IS NULL AND url IS NOT NULL "
                f"AND date_seen >= ? AND (fit_score IS NULL OR fit_score >= ?) "
                f"ORDER BY fit_score DESC, date_seen DESC",
                (cutoff, int(min_fit))
            ).fetchall()

        # Round-robin by host, so consecutive items target different sites.
        def host(u):
            m = re.search(r"https?://([^/]+)", u or "")
            return (m.group(1) if m else "").replace("www.", "")
        from collections import defaultdict, deque
        queues = defaultdict(deque)
        for r in rows:
            queues[host(r["url"])].append(dict(r))
        interleaved, qs = [], list(queues.values())
        while qs and len(interleaved) < limit:
            for q in qs:
                if q:
                    interleaved.append(q.popleft())
                    if len(interleaved) >= limit:
                        break
            qs = [q for q in qs if q]

        total = self.conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE {LIVE} AND status='new' "
            f"AND description IS NULL AND url IS NOT NULL AND date_seen >= ? "
            f"AND (fit_score IS NULL OR fit_score >= ?)", (cutoff, int(min_fit))
        ).fetchone()[0]
        return {"jobs": interleaved, "returned": len(interleaved),
                "total_needing": total, "within_days": within_days,
                "cutoff": cutoff,
                "message": (f"{total} job(s) within {within_days} days need a description; "
                            f"returning {len(interleaved)} interleaved by site. Fetch each "
                            f"url, then set_job_description(id, text). Re-run for more.")}

    # ------------------------------------------------------ pattern tooling

    def get_rejected_patterns(self) -> dict:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, pattern_type, pattern_value, reason, created_at "
                "FROM rejected_patterns ORDER BY pattern_type, pattern_value"
            ).fetchall()
        return {"patterns": [dict(r) for r in rows], "total": len(rows)}

    def add_rejected_pattern(
        self, pattern_type: str, pattern_value: str, reason: str | None = None
    ) -> dict:
        if pattern_type not in PATTERN_TYPES:
            return _err(
                f"Invalid pattern_type {pattern_type!r}. "
                f"Must be one of: {', '.join(PATTERN_TYPES)}"
            )
        value = normalize_key_part(pattern_value)
        if not value:
            return _err("pattern_value must not be empty")
        # source is compared with normalize_key_part, so no canonicalizer applies.
        canonicalizer = {"company": canonicalize_company,
                         "title_keyword": canonicalize_title}.get(pattern_type)
        if canonicalizer and (msg := self._unmatchable(value, canonicalizer)):
            return _err(msg)

        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO rejected_patterns "
                "(pattern_type, pattern_value, reason, created_at) VALUES (?, ?, ?, ?)",
                (pattern_type, value, reason, now_str()),
            )
            if cur.rowcount:
                return {"result": "added", "id": cur.lastrowid,
                        "message": f"Pattern added: {pattern_type} = {value}"}
            existing = self.conn.execute(
                "SELECT id FROM rejected_patterns WHERE pattern_type = ? AND pattern_value = ?",
                (pattern_type, value),
            ).fetchone()
        return {"result": "exists", "id": existing["id"],
                "message": f"Pattern already present: {pattern_type} = {value}"}

    def remove_rejected_pattern(self, id: int) -> dict:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT pattern_type, pattern_value FROM rejected_patterns WHERE id = ?",
                (id,),
            ).fetchone()
            if row is None:
                return _err(f"No pattern with id {id}")
            self.conn.execute("DELETE FROM rejected_patterns WHERE id = ?", (id,))
        return {
            "result": "removed", "id": id,
            "message": (
                f"Pattern removed: {row['pattern_type']} = {row['pattern_value']}. "
                f"Jobs already auto-rejected by it stay rejected; reopen them "
                f"individually with update_job(id, status='new')."
            ),
        }

    # ---------------------------------------------------------------- stats

    def get_stats(self) -> dict:
        with self._lock:
            total = self.conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {LIVE}").fetchone()[0]

            # Zero-fill: a GROUP BY omits empty statuses, and a missing key
            # reads as missing data rather than as zero.
            by_status = {s: 0 for s in STATUSES}
            for r in self.conn.execute(
                f"SELECT status, COUNT(*) c FROM jobs WHERE {LIVE} GROUP BY status"
            ):
                by_status[r["status"]] = r["c"]

            by_source = {}
            for r in self.conn.execute(
                f"SELECT COALESCE(source, '(unknown)') s, COUNT(*) c "
                f"FROM jobs WHERE {LIVE} GROUP BY s ORDER BY c DESC"
            ):
                by_source[r["s"]] = r["c"]

            patterns = self.conn.execute(
                "SELECT COUNT(*) FROM rejected_patterns"
            ).fetchone()[0]

            def avg(status: str):
                # AVG ignores NULLs; None (not 0) when nothing in that status is scored.
                return self.conn.execute(
                    f"SELECT ROUND(AVG(fit_score), 1) FROM jobs "
                    f"WHERE status = ? AND {LIVE}", (status,)
                ).fetchone()[0]

            unscored_new = self.conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE status = 'new' "
                f"AND fit_score IS NULL AND off_profile = 0 AND {LIVE}"
            ).fetchone()[0]

            off_profile_new = self.conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE status = 'new' "
                f"AND off_profile = 1 AND {LIVE}"
            ).fetchone()[0]

            merged = self.conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE merged_into IS NOT NULL"
            ).fetchone()[0]

            return {
                "total_jobs": total,
                "by_status": by_status,
                "by_source": by_source,
                "rejected_patterns": patterns,
                "avg_fit_score_new": avg("new"),
                "avg_fit_score_applied": avg("applied"),
                "unscored_new": unscored_new,
                "off_profile_new": off_profile_new,
                "profile_rules": self.conn.execute(
                    "SELECT COUNT(*) FROM profile_rules").fetchone()[0],
                "merged_duplicates": merged,
            }
