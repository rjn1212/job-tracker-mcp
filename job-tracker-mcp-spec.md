# job-tracker-mcp — Complete Implementation Spec
## For Claude Code Implementation

---

## Overview

Build a local Python MCP (Model Context Protocol) server that wraps a SQLite database for tracking job opportunities surfaced from email job alerts. This server runs locally on macOS and is registered in Claude Desktop alongside the `apple-mail-mcp` server. Together they form a two-MCP job triage workflow where Claude Desktop reads job alert emails via `apple-mail-mcp` and reads/writes job tracking state via this server.

---

## Project Location

```
/Users/randyn/projects/git/job-tracker-mcp/
```

---

## Tech Stack

- **Language:** Python 3.10+
- **MCP Framework:** `fastmcp` 3.x
- **Database:** SQLite via Python's built-in `sqlite3` module
- **Database file location:** `${JOB_TRACKER_DB}`, defaulting to `<project_root>/data/jobs.db`
- **Virtual environment:** `.venv` inside the project root
- **Entry point:** `job_tracker_mcp/server.py`

---

## Project Structure

```
/Users/randyn/projects/git/job-tracker-mcp/
├── .venv/                        # Virtual environment (not committed)
├── data/
│   └── jobs.db                   # SQLite database (not committed, created on first run)
├── job_tracker_mcp/
│   ├── __init__.py
│   ├── server.py                 # MCP server entry point, tool registration
│   ├── database.py               # SQLite connection, schema init, all queries
│   └── models.py                 # Enums, transition table, row->dict helpers
├── tests/
│   ├── __init__.py
│   ├── conftest.py               # pytest fixture yielding a temp-file Database
│   └── test_database.py
├── .gitignore
├── README.md
├── requirements.txt
├── requirements-dev.txt
└── start.sh                      # Startup wrapper script for Claude Desktop
```

---

## .gitignore

```
.venv/
data/
__pycache__/
*.pyc
*.pyo
*.db
*.db-wal
*.db-shm
.pytest_cache/
.DS_Store
```

---

## requirements.txt

```
fastmcp>=3.0,<4
```

## requirements-dev.txt

```
-r requirements.txt
pytest>=8.0
```

No other external dependencies. Use Python's built-in `sqlite3`, `hashlib`, `datetime`, `dataclasses`, `enum`, `json`, `logging`, `os`, `re`, and `threading`.

> **Version note:** `fastmcp` 0.x/1.x were the original standalone releases; the current line is 3.x and the API differs. Pin to 3.x. Both `@mcp.tool` and `@mcp.tool()` are accepted as decorators in 3.x; pick one and use it consistently. `mcp.run()` defaults to the stdio transport, which is what Claude Desktop expects.

---

## CRITICAL: stdout is the MCP transport

Claude Desktop speaks MCP to this server over **stdio**. Every byte written to `stdout` that is not a JSON-RPC frame corrupts the protocol and the server will appear to Claude Desktop as crashed or hung.

Therefore:

- **Never** call `print()` anywhere in `job_tracker_mcp/`.
- Configure logging to stderr explicitly, at import time in `server.py`, before constructing the `Database`:

  ```python
  import logging, sys
  logging.basicConfig(
      stream=sys.stderr,
      level=logging.INFO,
      format="%(asctime)s %(levelname)s %(name)s: %(message)s",
  )
  ```
- Any library that might print to stdout must be silenced. Do not add such libraries.
- Debugging output goes to stderr or to a log file, never stdout.

---

## Database Schema

Schema is created inside a single transaction at startup. `PRAGMA user_version` carries the schema version so future migrations are possible.

### Pragmas (applied on every connection, in this order)

```sql
PRAGMA journal_mode = WAL;      -- concurrent reader while a writer holds the lock
PRAGMA busy_timeout = 5000;     -- ms; wait rather than raising SQLITE_BUSY
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;    -- safe under WAL, much faster than FULL
```

`journal_mode` is persistent (stored in the file), the others are per-connection. Set all four anyway; it is cheap and makes single-connection behavior explicit.

### Table: `jobs`

```sql
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,         -- 16-hex-char SHA256 prefix of the normalized natural key
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    location        TEXT NOT NULL,            -- as written in the posting, for display
    location_canonical TEXT NOT NULL,         -- canonicalize_location(location); part of the id
    url             TEXT,
    source          TEXT,                     -- e.g. "LinkedIn", "Indeed", "Built In", "Dice"
    date_seen       TEXT NOT NULL,            -- ISO 8601 local date first seen, e.g. "2026-07-09"
    last_seen       TEXT NOT NULL,            -- ISO 8601 local date most recently seen in an alert
    times_seen      INTEGER NOT NULL DEFAULT 1,
    date_applied    TEXT,                     -- ISO 8601 date, NULL if not applied
    status          TEXT NOT NULL DEFAULT 'new',
    fit_score       INTEGER,                  -- 1-10, set by Claude during triage
    notes           TEXT,
    email_subject   TEXT,                     -- original email subject line for reference
    created_at      TEXT NOT NULL,            -- ISO 8601 datetime with offset
    updated_at      TEXT NOT NULL,            -- ISO 8601 datetime with offset

    CHECK (status IN ('new', 'applied', 'rejected', 'expired')),
    CHECK (fit_score IS NULL OR (fit_score BETWEEN 1 AND 10)),
    CHECK (times_seen >= 1),
    CHECK (status != 'applied' OR date_applied IS NOT NULL),
    CHECK (length(location_canonical) > 0)
);
```

`location` preserves whatever the posting said (`"Remote, US"`), because that is what the user wants to read. `location_canonical` (`"remote"`) is what feeds the id. On a re-sighting the stored `location` is **not** overwritten — the first spelling seen wins, and by construction any later spelling that reaches the same row canonicalized to the same value anyway.

The `CHECK` constraints are the last line of defense. Validate in Python too, and return a clean error string rather than letting an `IntegrityError` surface — but keep the constraints so a bug cannot write nonsense.

The last constraint reads "applied implies a date," deliberately **not** "a date implies applied." A job you applied to and that later expired keeps its `date_applied` as history; the transition to `expired` must not clear it.

### Table: `rejected_patterns`

```sql
CREATE TABLE IF NOT EXISTS rejected_patterns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type    TEXT NOT NULL,            -- company | title_keyword | source
    pattern_value   TEXT NOT NULL,            -- stored lowercased and stripped
    reason          TEXT,
    created_at      TEXT NOT NULL,

    CHECK (pattern_type IN ('company', 'title_keyword', 'source')),
    CHECK (length(trim(pattern_value)) > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_patterns_unique
    ON rejected_patterns(pattern_type, pattern_value);
```

The `UNIQUE` index prevents the same pattern from being added twice (once via `reject_job`, once via `add_rejected_pattern`). Inserts use `INSERT OR IGNORE`; a no-op insert is reported as `result: "exists"`, not an error.

`pattern_value` is **always stored lowercased and stripped**. Matching is done against lowercased job fields. This is the only way to get case-insensitive matching without depending on SQLite collation rules.

### Indexes

```sql
CREATE INDEX IF NOT EXISTS idx_jobs_status     ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_company    ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_date_seen  ON jobs(date_seen);
CREATE INDEX IF NOT EXISTS idx_jobs_source     ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_jobs_triage     ON jobs(status, fit_score DESC, date_seen DESC);
```

The composite `idx_jobs_triage` index serves the dominant query: "show me new jobs, best fit first."

### Schema versioning

```python
SCHEMA_VERSION = 1

def _init_schema(self):
    with self._lock, self.conn:                 # conn as ctx mgr = transaction
        current = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if current == 0:
            # fresh database: create everything
            self.conn.executescript(SCHEMA_SQL)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif current < SCHEMA_VERSION:
            self._migrate(current, SCHEMA_VERSION)
        elif current > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema v{current} is newer than this code (v{SCHEMA_VERSION})"
            )
```

`_migrate` may be a stub that raises `NotImplementedError` for now. The point is that the version is written on day one, so a v2 migration is possible without guessing what shape the existing file is in.

---

## Time and Date Rules

Two distinct concepts, do not conflate them:

- **Dates** (`date_seen`, `last_seen`, `date_applied`) are *local calendar dates*, format `YYYY-MM-DD`. "Did I apply today?" is a local-calendar question.
- **Timestamps** (`created_at`, `updated_at`) are *instants*, stored as ISO 8601 **with a UTC offset**, e.g. `2026-07-09T16:29:04-07:00`.

```python
from datetime import datetime, date

def today_str() -> str:
    return date.today().isoformat()

def now_str() -> str:
    # .astimezone() with no arg attaches the local timezone, so the offset is preserved
    return datetime.now().astimezone().isoformat(timespec="seconds")
```

Never bind a Python `datetime` or `date` object directly into a SQLite statement. Always convert to `str` first.

---

## ID Generation Logic

The `id` is a natural dedup key derived from a **normalized** natural key. Normalization is the entire point — `"Acme  Corp."` and `"acme corp"` must collide, or the dedup guarantee is worthless.

```python
import hashlib
import re

_WS = re.compile(r"\s+")

def normalize_key_part(value: str | None) -> str:
    """Normalize one component of the natural key.

    Lowercase, collapse internal whitespace, strip surrounding whitespace.
    Corporate suffixes (Inc, Inc., LLC) are NOT stripped -- that is too
    aggressive and would collide distinct entities.
    """
    if value is None:
        return ""
    return _WS.sub(" ", value).strip().lower()
```

`normalize_key_part` must tolerate `None` — `check_job` and `add_job` both call it with values that arrive from an LLM, and a missing `location` must not raise `AttributeError`.

### Location canonicalization

Job boards render the same remote role as `Remote`, `Remote, US`, `United States (Remote)`, `100% Remote`, and `WFH`. These are one job and **must** produce one id. Canonicalization happens in code, not by asking the model to normalize — an instruction the model follows inconsistently, producing silent duplicate rows that no test catches.

```python
_SEP = re.compile(r"[,/|()\[\]\-–—:;]+")

_UNKNOWN = frozenset({"", "unknown", "n a", "na", "not specified", "unspecified", "tbd"})

# Phrases rewritten to the bare token "remote" before tokenizing.
_REMOTE_PHRASES = ("work from home", "working from home", "work at home")

# Presence of any of these means the role is NOT fully remote, even though the
# string contains the word "remote" (e.g. "Hybrid - Remote 2 days/week").
_NOT_REMOTE = ("hybrid", "onsite", "on site", "in office", "in person")

_REMOTE_TOKENS = frozenset({
    "remote", "anywhere", "wfh", "telecommute", "telecommuting",
    "virtual", "distributed",
})

# Tokens carrying no geographic information once "remote" is established.
# Note "emea", "canada", "apac" are deliberately absent: geography-restricted
# remote is a different job from open remote.
_REMOTE_QUALIFIERS = frozenset({
    "us", "usa", "u.s.", "u.s.a.", "united", "states", "america",
    "based", "only", "fully", "full", "time", "100%", "nationwide", "national",
    "anywhere", "work", "from", "home", "in", "the",
})

# Trailing country tokens dropped from non-remote locations, so that
# "Austin, TX, USA" and "Austin, TX" collide.
_COUNTRY_TOKENS = frozenset({"us", "usa", "u.s.", "u.s.a.", "united", "states", "america"})


def canonicalize_location(value: str | None) -> str:
    """Collapse the many spellings of a location into one canonical form.

    Returns "remote" for any fully-remote role with no geographic restriction,
    "unknown" for absent/placeholder values, and an otherwise-normalized token
    string ("austin tx") for physical locations.
    """
    s = normalize_key_part(value)
    if s in _UNKNOWN:
        return "unknown"

    s = _WS.sub(" ", _SEP.sub(" ", s)).strip()
    for phrase in _REMOTE_PHRASES:
        s = s.replace(phrase, "remote")

    if not any(marker in s for marker in _NOT_REMOTE):
        tokens = s.split()
        has_remote = any(t in _REMOTE_TOKENS for t in tokens)
        rest_is_noise = all(
            t in _REMOTE_TOKENS or t in _REMOTE_QUALIFIERS for t in tokens
        )
        if has_remote and rest_is_noise:
            return "remote"

    tokens = s.split()
    while tokens and tokens[-1] in _COUNTRY_TOKENS:
        tokens.pop()
    return " ".join(tokens) or "unknown"


def generate_job_id(company: str, title: str, location: str | None) -> str:
    raw = "|".join((
        normalize_key_part(company),
        normalize_key_part(title),
        canonicalize_location(location),
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
```

Worked examples, all of which the tests must assert:

| Input | Canonical | Note |
|---|---|---|
| `Remote` | `remote` | |
| `Remote, US` | `remote` | the case that motivated this |
| `United States (Remote)` | `remote` | |
| `100% Remote` | `remote` | |
| `Fully Remote` | `remote` | |
| `WFH` / `Work From Home` | `remote` | |
| `Remote, Anywhere` | `remote` | |
| `Remote - EMEA` | `remote emea` | **distinct** — restricted remote is a different job |
| `Hybrid - Remote 2 days` | `hybrid remote 2 days` | **distinct** — not fully remote |
| `Remote in Austin, TX` | `remote austin tx` | **distinct** — city-restricted |
| `Austin, TX, USA` | `austin tx` | trailing country dropped |
| `Austin, TX` | `austin tx` | collides with the above |
| `` / `Unknown` / `N/A` | `unknown` | |

Two limitations kept deliberately, because fixing them means shipping a gazetteer:

- `Austin, TX` and `Austin, Texas` do **not** collide. State-abbreviation expansion is a tarpit with real ambiguity (`IN`, `OR`, `ME` are both states and English words).
- `New York, NY` and `NYC` do not collide.

Both fail toward a duplicate row, which is visible and correctable, rather than toward a false merge, which silently destroys a record.

### The key is versioned

`canonicalize_location` is part of the persisted key: change it and every stored `id` becomes wrong, orphaning existing rows and reintroducing the duplicates it was meant to prevent. Therefore:

- The schema stores the canonical form alongside the raw one (`location_canonical`), so a migration can tell which rows were keyed under which rule and recompute.
- Any edit to `canonicalize_location`, `normalize_key_part`, or the `raw` join format **requires** bumping `SCHEMA_VERSION` and writing a `_migrate` step that recomputes `id` and `location_canonical` for every row, merging any rows that newly collide (keep the earliest `date_seen`, the max `times_seen`, and the non-`new` status).
- Do not "just tweak the token list." It is a data migration every time.

16 hex characters is 64 bits. At a few thousand jobs the birthday-collision probability is on the order of 1e-13. This is fine.

---

## Status Lifecycle — Single Source of Truth

This transition table is authoritative. `models.py` exports it and `update_job` is the only code that consults it.

```python
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "new":      {"new", "applied", "rejected", "expired"},
    "applied":  {"applied", "rejected", "expired"},
    "rejected": {"rejected", "expired", "new"},   # reconsideration is allowed
    "expired":  {"expired", "new", "applied"},    # a role can be reposted
}
```

Reading of the rules:

- **Self-transitions are always allowed** and are no-ops. This makes `update_job` idempotent, which matters because an LLM will retry.
- **`applied → new` is forbidden.** Once you have applied, "new" is a lie; the record has a `date_applied`. Use `expired` if the role is dead.
- **`rejected → new` is allowed.** The previous spec called rejection "terminal" while simultaneously providing `remove_rejected_pattern` for reconsidering a company — a direct contradiction. Reconsideration is a real workflow; it is permitted, and `update_job` is where it happens.
- **`expired → new` is allowed**, because a genuinely reposted role arriving in a fresh alert should re-enter triage.

Any transition not in the table returns:

```json
{"error": "Invalid status transition: applied -> new"}
```

Setting `status = "applied"` through `update_job` **must** also set `date_applied` (to today, if not supplied), to satisfy the `applied implies a date` constraint. Prefer `mark_applied` for this; `update_job` supports it for completeness.

Clearing a rejection via `update_job(status="new")` does **not** remove any `rejected_patterns` rows. If a matching company pattern still exists, the next `add_job` for that company auto-rejects again. `remove_rejected_pattern` is the tool for that; the two are deliberately independent, and the tool descriptions say so.

---

## Rejected-Pattern Matching Semantics

Exactly one function implements this. Ambiguity here is what makes auto-filtering feel haunted.

```python
def _check_rejected_patterns(
    self, company: str, title: str, source: str | None
) -> list[str]:
    """Return a list of matched pattern descriptors, e.g. ['company:infosys'].

    company:       substring match, pattern in normalized company
                   ("infosys" matches "Infosys BPM Ltd")
    title_keyword: substring match, pattern in normalized title
                   ("staffing" matches "Staffing Coordinator")
    source:        exact match on normalized source
                   ("dice" matches "Dice", not "Dice Inc")
    """
```

- Company and keyword use **substring** matching, because company names arrive with suffixes (`Infosys BPM Ltd`) and keywords are inherently fragments (`staffing`, `clearance`).
- Source uses **exact** matching, because sources come from a small closed set that the caller controls; substring matching there would surprise.
- All comparisons are between `normalize_key_part(field)` and the already-lowercased `pattern_value`.
- A `NULL` source matches no source pattern.
- The function is called by `add_job` and `bulk_add_jobs`. It never rejects an *existing* row; it only affects the status assigned at insert time.

---

## Concurrency and Transactions

FastMCP dispatches synchronous tool functions on a threadpool, so tools **can** be entered concurrently. A single shared `sqlite3.Connection` with `check_same_thread=False` is *not* thread-safe by itself; that flag only disables Python's guard rail, it does not add locking.

The design:

```python
import sqlite3, threading

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
```

Rules:

- **Every** method that touches `self.conn` holds `self._lock` for the duration.
- Writes go through `with self._lock, self.conn:` — using the connection as a context manager commits on success and rolls back on exception. Do not call `conn.commit()` by hand and do not rely on autocommit.
- Reads also take the lock (SQLite objects are not safe to share across threads even for reads when the connection is shared).
- `busy_timeout` handles the case where an external process (e.g. a `sqlite3` CLI session) holds the write lock.

`db_path` being a constructor parameter — with an env-var override and a project-relative default — is what makes the test suite possible. **The path must not be a module-level constant baked into `Database.__init__`.** Tests must never touch `data/jobs.db`.

---

## SQL Injection and LIKE Escaping

- All values are bound as parameters. **No f-strings or `%` formatting anywhere in a SQL string.**
- Column names and sort directions are never taken from user input; where a query needs a dynamic column, select it from a hardcoded allowlist dict.
- The `status` and `pattern_type` filters are validated against their enums *before* reaching SQL, and an invalid value returns `{"error": ...}` rather than silently matching nothing.
- `LIKE` patterns must escape the wildcards `%`, `_`, and the escape character itself. A search for `100%` must not match everything:

  ```python
  def like_escape(term: str) -> str:
      return (term.replace("\\", "\\\\")
                  .replace("%", "\\%")
                  .replace("_", "\\_"))

  # usage
  pattern = f"%{like_escape(query)}%"
  sql = """
      SELECT * FROM jobs
      WHERE (LOWER(company)       LIKE LOWER(?) ESCAPE '\\'
          OR LOWER(title)         LIKE LOWER(?) ESCAPE '\\'
          OR LOWER(COALESCE(notes, ''))         LIKE LOWER(?) ESCAPE '\\'
          OR LOWER(COALESCE(email_subject, '')) LIKE LOWER(?) ESCAPE '\\')
  """
  ```

  SQLite's built-in `LIKE` is case-insensitive for ASCII only, which is why `LOWER()` is applied to both sides rather than trusted implicitly.

---

## Shared Conventions for All Tools

Applied uniformly; the per-tool sections below do not repeat them.

**Return envelope.** Every tool returns a JSON-serializable `dict`. Success responses carry a `result` key (or the domain payload, for read tools). Failure responses are exactly `{"error": "<human-readable message>"}` and nothing else. No tool raises to the caller.

```python
def _tool_guard(fn):
    """Wrap a tool body: convert any exception into {'error': str}."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:                     # noqa: BLE001 -- deliberate
            log.exception("tool %s failed", fn.__name__)
            return {"error": f"{type(exc).__name__}: {exc}"}
    return wrapper
```

Apply `@_tool_guard` beneath `@mcp.tool()` on every tool. The `log.exception` call writes the traceback to **stderr**, where it is visible in Claude Desktop's MCP log without corrupting the transport.

**Optional parameters.** FastMCP derives the JSON schema from type hints. Optional parameters must be annotated `str | None = None`, never `str = None` (which types as required-and-nullable and confuses schema generation). Booleans default to `False`, not `None`.

**Docstrings are the tool description the model sees.** Every tool's docstring must state what it does, when to prefer it over a sibling tool, and any normalization the caller is expected to perform (e.g. `location="Remote"`). This is load-bearing prompt text, not developer commentary.

**Pagination.** `limit` is clamped to `1 <= limit <= 200`; a request for more returns 200 rather than an error. `offset` is clamped to `>= 0`. `total` always means **the number of rows matching the filter, ignoring `limit`/`offset`** — it requires a separate `SELECT COUNT(*)` with the same `WHERE` clause. The 200 cap exists to protect the model's context window, and the cap being hit is signaled by `total > offset + len(jobs)`.

**Default ordering.** Unless stated otherwise, jobs are returned `ORDER BY (fit_score IS NULL), fit_score DESC, date_seen DESC, id ASC`. The leading `(fit_score IS NULL)` term sorts unscored jobs last; SQLite has no `NULLS LAST`. The trailing `id ASC` makes the order total, so pagination cannot skip or repeat a row.

**Job object shape.** Every tool that returns a job returns this exact shape:

```json
{
  "id": "a1b2c3d4e5f60718",
  "company": "Acme Corp",
  "title": "Staff Engineer",
  "location": "Remote, US",
  "location_canonical": "remote",
  "url": "https://...",
  "source": "LinkedIn",
  "date_seen": "2026-07-09",
  "last_seen": "2026-07-09",
  "times_seen": 1,
  "date_applied": null,
  "status": "new",
  "fit_score": 8,
  "notes": null,
  "email_subject": "Your job alert for Staff Engineer",
  "created_at": "2026-07-09T16:29:04-07:00",
  "updated_at": "2026-07-09T16:29:04-07:00"
}
```

Produced by a single `models.row_to_job(row: sqlite3.Row) -> dict` helper. Never return a raw `sqlite3.Row`.

---

## MCP Tools — Complete Specification

Thirteen tools.

---

### Tool 1: `add_job`

**Purpose:** Add a single job. Idempotent on the natural key.

**Parameters:**
```
company       str   required
title         str   required
location      str   required  (verbatim from the posting; "Unknown" if absent)
url           str   optional
source        str   optional  ("LinkedIn", "Indeed", "Built In", ...)
fit_score     int   optional  (1-10)
notes         str   optional
email_subject str   optional
```

`location` is passed through verbatim and canonicalized in code. The caller must **not** rewrite `"Remote, US"` to `"Remote"`; doing so throws away the display string for no benefit, since both canonicalize to `remote` and hash to the same id.

**Returns:**
```json
{
  "result": "created",
  "id": "a1b2c3d4e5f60718",
  "status": "new",
  "auto_rejected": false,
  "matched_patterns": [],
  "message": "Job added: Staff Engineer at Acme Corp"
}
```

`result` is one of `created` | `duplicate`. On `duplicate` the response carries the **existing** record's `status`, `date_seen`, `date_applied`, and the post-increment `times_seen`.

**Logic:**
1. Validate `fit_score` is `None` or in `1..10`. Reject otherwise.
2. Compute `location_canonical = canonicalize_location(location)` and generate the id from `(normalized company, normalized title, location_canonical)`.
3. `SELECT` the row. If it exists:
   - Set `last_seen = today`, `times_seen = times_seen + 1`, `updated_at = now`.
   - **Backfill only:** if the stored `url` or `source` is `NULL` and the call supplies one, write it. Never overwrite a non-null value, and never touch `status`, `fit_score`, `notes`, or `location` — a re-sighting must not clobber triage decisions or the display string.
   - Return `result: "duplicate"`.
4. If it does not exist, call `_check_rejected_patterns(company, title, source)`.
   - If any pattern matched, insert with `status = "rejected"` and seed `notes` with `Auto-rejected: matched company:infosys`.
   - Otherwise insert with `status = "new"`.
   - Store both `location` (verbatim) and `location_canonical`.
   - Set `date_seen = last_seen = today`, `times_seen = 1`, `created_at = updated_at = now`.
   - Return `result: "created"` with `auto_rejected` and `matched_patterns` populated.

The `auto_rejected` and `status` fields are the fix for a real gap: the previous return shape made it impossible for the caller to distinguish a job that was filed for triage from one that was silently auto-rejected on insert.

Steps 3 and 4 run inside **one** transaction; the `SELECT`-then-`INSERT` is otherwise a race between two concurrent tool calls. Alternatively use `INSERT ... ON CONFLICT(id) DO UPDATE` and inspect the result — either is acceptable, the transaction is not optional.

---

### Tool 2: `bulk_add_jobs`

**Purpose:** Add many jobs in one call. **This is the tool to use when triaging a job alert email**, which typically contains 10–25 postings.

**Parameters:**
```
jobs   list[dict]   required   (each dict has the same keys as add_job's parameters)
```

**Returns:**
```json
{
  "created": 14,
  "duplicates": 6,
  "auto_rejected": 3,
  "errors": 0,
  "results": [
    {"id": "a1b2...", "company": "Acme Corp", "title": "Staff Engineer",
     "result": "created", "status": "new", "auto_rejected": false},
    {"id": "f7e6...", "company": "Infosys BPM", "title": "Java Developer",
     "result": "created", "status": "rejected", "auto_rejected": true,
     "matched_patterns": ["company:infosys"]}
  ]
}
```

**Logic:**
- Cap the input at 100 entries; beyond that return `{"error": "..."}` asking the caller to batch.
- Process each entry with the same logic as `add_job`, **all within a single transaction**, so a partial failure leaves the database untouched.
- A malformed entry (missing `company`) does not abort the batch: it contributes `{"result": "error", "message": "..."}` to `results` and increments `errors`. Only an infrastructure failure rolls the transaction back.
- `results` preserves input order and always has the same length as `jobs`.

Without this tool, triaging one alert email costs 25 sequential tool round-trips. This is the single highest-leverage addition to the original spec.

---

### Tool 3: `check_job`

**Purpose:** Check whether a job already exists before adding it. Use during triage to avoid re-processing.

> **Note for the tool docstring:** `add_job` is already idempotent, so `check_job` is *not* required before `add_job`. Use `check_job` only when the decision of whether to even look at a posting depends on its current status. Prefer `bulk_add_jobs` for triage.

**Parameters:**
```
company   str   required
title     str   required
location  str   required
```

**Returns:**
```json
{
  "exists": true,
  "id": "a1b2c3d4e5f60718",
  "status": "applied",
  "date_seen": "2026-06-15",
  "last_seen": "2026-07-01",
  "times_seen": 3,
  "date_applied": "2026-06-16"
}
```
or
```json
{"exists": false, "id": "a1b2c3d4e5f60718"}
```

Returning the computed `id` even when the job does not exist lets the caller pass it straight to a follow-up tool. `check_job` is a pure read and never increments `times_seen`.

---

### Tool 4: `mark_applied`

**Purpose:** Mark a job as applied to.

**Parameters:**
```
id            str   required
date_applied  str   optional  (ISO date "YYYY-MM-DD"; defaults to today)
notes         str   optional  (appended to existing notes)
```

**Returns:**
```json
{"result": "updated", "id": "a1b2...", "message": "Marked as applied: Staff Engineer at Acme Corp"}
```

**Logic:**
- Look up by id; if absent return `{"error": "No job with id <id>"}`.
- Validate the current status permits `-> applied` per `ALLOWED_TRANSITIONS`. A `new` or `expired` job may be applied to; a `rejected` job may not (un-reject it first, deliberately).
- Validate `date_applied` parses as `YYYY-MM-DD` if supplied.
- Set `status = "applied"`, `date_applied = date_applied or today`, `updated_at = now`.
- If `notes` is supplied, **append** with a newline separator, preserving existing notes. If existing notes are `NULL`, the result is just the new note.

`mark_applied` appends notes; `update_job` replaces them. This asymmetry is intentional (an application note is an addition to the record's history, an edit is a correction) and both docstrings must say so explicitly, because it is exactly the kind of thing a model gets wrong.

---

### Tool 5: `reject_job`

**Purpose:** Reject a specific job, optionally learning a pattern so similar future jobs auto-reject.

**Parameters:**
```
id                  str   required
reason              str   optional
add_company_pattern bool  optional  default False
add_keyword_pattern str   optional  (a title keyword)
add_source_pattern  bool  optional  default False
```

**Returns:**
```json
{
  "result": "rejected",
  "id": "f7e6...",
  "patterns_added": ["company:infosys"],
  "patterns_existing": ["title_keyword:staffing"],
  "message": "Job rejected. 1 new pattern added; future matches will be auto-filtered."
}
```

**Logic:**
- Set `status = "rejected"` (validate the transition), append `reason` to notes, `updated_at = now`.
- `add_company_pattern` inserts the job's `company` as a `company` pattern.
- `add_keyword_pattern` inserts the given string as a `title_keyword` pattern.
- `add_source_pattern` inserts the job's `source` as a `source` pattern; a no-op with a warning in `message` if the job's source is `NULL`.
- All inserts are `INSERT OR IGNORE` against the unique index. Newly inserted patterns land in `patterns_added`; already-present ones land in `patterns_existing`. Neither is an error.
- Adding a pattern does **not** retroactively reject already-stored jobs. Patterns apply at insert time only. Say this in the docstring.

`add_source_pattern` closes a real gap: the schema has always allowed `pattern_type = 'source'`, but no tool could create one and `_check_rejected_patterns` never consulted it, so source patterns were dead code.

---

### Tool 6: `get_jobs`

**Purpose:** Retrieve jobs with optional filtering. The primary tool for surfacing the current list.

**Parameters:**
```
status        str   optional  (new | applied | rejected | expired)
source        str   optional  (exact match, case-insensitive)
min_fit_score int   optional  (>= this score; excludes unscored jobs)
limit         int   optional  default 50, clamped to [1, 200]
offset        int   optional  default 0
```

**Returns:**
```json
{
  "jobs": [ /* job objects, default ordering */ ],
  "total": 87,
  "offset": 0,
  "limit": 50
}
```

`total` is the count of rows matching the filters, **not** `len(jobs)`. An invalid `status` returns `{"error": "Invalid status 'aplied'. Must be one of: new, applied, rejected, expired"}` — never an empty result set, which would read to the model as "no such jobs."

`min_fit_score` excludes rows where `fit_score IS NULL`, since an unscored job cannot be said to meet a threshold.

---

### Tool 7: `get_job`

**Purpose:** Retrieve a single job by id.

**Parameters:** `id: str` (required)

**Returns:** A job object, or `{"error": "No job with id <id>"}`.

---

### Tool 8: `update_job`

**Purpose:** Update mutable fields on an existing job — revise `fit_score`, correct `notes`, add a `url`, or change `status`.

**Parameters:**
```
id          str   required
fit_score   int   optional  (1-10)
notes       str   optional  (REPLACES existing notes entirely)
url         str   optional
status      str   optional  (validated against ALLOWED_TRANSITIONS)
```

**Returns:**
```json
{"result": "updated", "id": "a1b2...", "changed": ["fit_score", "status"], "status": "applied"}
```

**Logic:**
- Only fields explicitly provided (not `None`) are written. `changed` lists them, so the caller can confirm the update landed.
- If **no** field is provided, return `{"error": "No fields to update"}` rather than silently bumping `updated_at`.
- `fit_score` outside `1..10` is an error.
- `status` is checked against `ALLOWED_TRANSITIONS[current_status]`. See the lifecycle section.
- Setting `status = "applied"` also sets `date_applied = today` if it is currently `NULL`, satisfying the `applied implies a date` constraint. Prefer `mark_applied`.
- Always sets `updated_at = now` when at least one field changed.

There is no distinct `mark_expired` tool; `update_job(id, status="expired")` is the way, and the `get_jobs`/`update_job` docstrings say so.

---

### Tool 9: `get_rejected_patterns`

**Purpose:** Show all active rejection patterns, so the user and Claude both know what is being auto-filtered.

**Parameters:** none

**Returns:**
```json
{
  "patterns": [
    {"id": 1, "pattern_type": "company", "pattern_value": "infosys",
     "reason": "body shop / staff aug", "created_at": "2026-07-09T16:29:04-07:00"}
  ],
  "total": 4
}
```

Ordered by `pattern_type`, then `pattern_value`. Values are returned in their stored (lowercased) form; this is what actually gets matched, and showing the original casing would misrepresent the rule.

---

### Tool 10: `add_rejected_pattern`

**Purpose:** Add a rejection pattern directly, not tied to rejecting a specific job.

**Parameters:**
```
pattern_type   str   required  (company | title_keyword | source)
pattern_value  str   required
reason         str   optional
```

**Returns:**
```json
{"result": "added", "id": 5, "message": "Pattern added: company = wipro"}
```

`result` is `added` | `exists`. Adding a duplicate is **not** an error; it returns `exists` with the existing row's `id`. `pattern_value` is normalized (stripped, lowercased) before insert; an empty or whitespace-only value is an error. An invalid `pattern_type` is an error listing the valid values.

Does not retroactively reject stored jobs.

---

### Tool 11: `remove_rejected_pattern`

**Purpose:** Remove a rejection pattern (e.g. a company previously rejected but now worth reconsidering).

**Parameters:** `id: int` (required, from `get_rejected_patterns`)

**Returns:**
```json
{"result": "removed", "id": 5, "message": "Pattern removed: company = wipro"}
```

A missing id returns `{"error": "No pattern with id 5"}` rather than a silent success — `cursor.rowcount == 0` must be checked.

Removing a pattern does not un-reject jobs that were auto-rejected by it. Those must be revived individually with `update_job(id, status="new")`. The docstring says so, and `message` reminds the caller.

---

### Tool 12: `get_stats`

**Purpose:** Summary statistics for dashboard-style reporting.

**Parameters:** none

**Returns:**
```json
{
  "total_jobs": 87,
  "by_status": {"new": 23, "applied": 31, "rejected": 28, "expired": 5},
  "by_source": {"LinkedIn": 42, "Indeed": 28, "Built In": 17, "(unknown)": 0},
  "rejected_patterns": 6,
  "avg_fit_score_new": 6.4,
  "avg_fit_score_applied": 7.8,
  "unscored_new": 2
}
```

**Logic:**
- `by_status` always contains **all four** keys, zero-filled. A `GROUP BY` alone omits statuses with no rows, and a missing key reads to the model as missing data rather than as zero.
- `by_source` buckets `NULL` source under the literal key `"(unknown)"`. Omit the key entirely if the count is zero.
- `avg_fit_score_*` is `ROUND(AVG(fit_score), 1)`. SQL `AVG` ignores `NULL`s. If **no** rows in that status have a score, the value is `null`, not `0` — a distinction that matters.
- `unscored_new` counts `status='new' AND fit_score IS NULL`: the triage backlog, and the number the user actually wants.

---

### Tool 13: `search_jobs`

**Purpose:** Substring search across `company`, `title`, `notes`, and `email_subject`.

**Parameters:**
```
query    str   required  (non-empty after stripping)
status   str   optional  (validated, as in get_jobs)
limit    int   optional  default 20, clamped to [1, 200]
offset   int   optional  default 0
```

**Returns:** Same envelope as `get_jobs` (`jobs`, `total`, `offset`, `limit`).

**Logic:** `LIKE '%query%'` with `OR` across the four columns, wildcards escaped per the LIKE-escaping section, both sides `LOWER()`ed, `NULL` columns `COALESCE`'d to `''`. An empty or whitespace-only `query` is an error, not a full table scan.

`offset` is present here — the original spec promised "same shape as `get_jobs`," which includes `offset`, while providing no way to set it.

---

## models.py

Small and boring on purpose. No ORM.

```python
from enum import StrEnum

class Status(StrEnum):
    NEW = "new"
    APPLIED = "applied"
    REJECTED = "rejected"
    EXPIRED = "expired"

class PatternType(StrEnum):
    COMPANY = "company"
    TITLE_KEYWORD = "title_keyword"
    SOURCE = "source"

ALLOWED_TRANSITIONS: dict[str, set[str]] = { ... }   # as specified above

JOB_FIELDS = (
    "id", "company", "title", "location", "location_canonical", "url", "source",
    "date_seen", "last_seen", "times_seen", "date_applied",
    "status", "fit_score", "notes", "email_subject",
    "created_at", "updated_at",
)

def row_to_job(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in JOB_FIELDS}
```

`StrEnum` requires Python 3.11+. On 3.10, use `class Status(str, Enum)`. The stated floor is 3.10, so use the `(str, Enum)` form unless the venv is confirmed to be 3.11+.

The original spec listed `models.py` in the file tree and then never specified it, which invites an implementer to invent a `Job` dataclass that duplicates the schema and immediately drifts from it. Do not add one. `sqlite3.Row` plus `row_to_job` is sufficient and has one source of truth.

---

## server.py — Structure

```python
import functools, logging, sys
from fastmcp import FastMCP
from job_tracker_mcp.database import Database

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="...")
log = logging.getLogger("job_tracker_mcp")

mcp = FastMCP("job-tracker")
db = Database()          # honors JOB_TRACKER_DB, else <project>/data/jobs.db


def _tool_guard(fn):     # as specified in Shared Conventions
    ...


@mcp.tool()
@_tool_guard
def add_job(
    company: str,
    title: str,
    location: str,
    url: str | None = None,
    source: str | None = None,
    fit_score: int | None = None,
    notes: str | None = None,
    email_subject: str | None = None,
) -> dict:
    """Add a job posting to the tracker.

    Idempotent: adding the same (company, title, location) twice records a
    re-sighting rather than a second row, and never overwrites an existing
    fit_score, status, or notes.

    Pass `location` exactly as the posting writes it -- "Remote, US",
    "United States (Remote)", "Austin, TX, USA". Do NOT rewrite it into some
    canonical form: the server already collapses every spelling of a remote
    role onto the same job, and the raw string is preserved for display. Pass
    "Unknown" only when the posting genuinely does not state a location.

    For a job alert email containing several postings, prefer bulk_add_jobs.
    """
    return db.add_job(...)


# ... 12 more tools, each @mcp.tool() + @_tool_guard, each with a real docstring

if __name__ == "__main__":
    mcp.run()            # stdio transport
```

`server.py` contains no SQL and no business logic. It validates nothing that `database.py` does not also validate; it is a thin, well-documented adapter. Every rule in this spec is enforced in `database.py`, which is what the tests exercise.

---

## database.py — Structure

```python
class Database:
    def __init__(self, db_path: str | os.PathLike | None = None): ...
    def close(self) -> None: ...

    def _apply_pragmas(self) -> None: ...
    def _init_schema(self) -> None: ...
    def _migrate(self, frm: int, to: int) -> None: ...

    @staticmethod
    def generate_job_id(company: str, title: str, location: str) -> str: ...

    def _check_rejected_patterns(
        self, company: str, title: str, source: str | None
    ) -> list[str]: ...

    def add_job(self, ...) -> dict: ...
    def bulk_add_jobs(self, jobs: list[dict]) -> dict: ...
    def check_job(self, ...) -> dict: ...
    def mark_applied(self, ...) -> dict: ...
    def reject_job(self, ...) -> dict: ...
    def get_jobs(self, ...) -> dict: ...
    def get_job(self, id: str) -> dict: ...
    def update_job(self, ...) -> dict: ...
    def get_rejected_patterns(self) -> dict: ...
    def add_rejected_pattern(self, ...) -> dict: ...
    def remove_rejected_pattern(self, id: int) -> dict: ...
    def get_stats(self) -> dict: ...
    def search_jobs(self, ...) -> dict: ...
```

---

## start.sh

```bash
#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

# Invoke the venv interpreter directly. `source .venv/bin/activate` only mutates
# PATH, which `exec python` then reads -- one more moving part for no benefit,
# and it breaks if the venv is relocated.
exec .venv/bin/python -m job_tracker_mcp.server
```

Make executable: `chmod +x start.sh`

Notes:
- `cd "$(dirname "$0")"` rather than a hardcoded path, so the checkout can move.
- `set -euo pipefail` so a missing venv fails loudly at launch instead of producing a server that answers nothing.
- Do **not** set `PYTHONUNBUFFERED` expecting it to help; stdout must carry only JSON-RPC regardless. It is harmless but pointless here.
- Claude Desktop launches this with a minimal environment (no shell profile, possibly no `PATH` beyond a system default). Nothing in this script depends on `PATH`.

---

## Claude Desktop Config Entry

```json
{
  "mcpServers": {
    "job-tracker": {
      "command": "/Users/randyn/projects/git/job-tracker-mcp/start.sh",
      "args": []
    }
  }
}
```

Merge into `~/Library/Application Support/Claude/claude_desktop_config.json`. The `command` must be an absolute path; Claude Desktop does not resolve against `PATH` or the user's shell.

**Claude Desktop must be fully quit and relaunched** (⌘Q, not just closing the window) for a config change to take effect.

MCP server logs are at `~/Library/Logs/Claude/mcp-server-job-tracker.log`. That is where `_tool_guard`'s stderr tracebacks land — check there first when a tool misbehaves.

---

## Setup Steps Claude Code Must Execute

1. Create the project directory and `job_tracker_mcp/`, `tests/`, `data/`.
2. Create the venv: `python3 -m venv .venv`. Confirm the version: `.venv/bin/python --version` — it must be ≥ 3.10, and record whether it is ≥ 3.11 (decides `StrEnum` vs `(str, Enum)` in `models.py`).
3. `.venv/bin/pip install -r requirements-dev.txt`
4. Record the resolved fastmcp version: `.venv/bin/pip show fastmcp`. If it is not 3.x, stop and report rather than adapting the code to an older API.
5. Write all source files per this spec.
6. `chmod +x start.sh`
7. **Run the tests:** `.venv/bin/python -m pytest tests/ -q`. All must pass.
8. **Smoke test the database layer** (does *not* start the server — a stdio server blocks forever waiting on stdin, so `./start.sh` is not a smoke test):

   ```bash
   JOB_TRACKER_DB=/tmp/jt_smoke.db .venv/bin/python - <<'PY'
   from job_tracker_mcp.database import Database
   db = Database()
   print(db.add_job(company="Acme", title="Staff Eng", location="Remote"), file=__import__("sys").stderr)
   print(db.get_stats(), file=__import__("sys").stderr)
   db.close()
   PY
   rm -f /tmp/jt_smoke.db*
   ```

9. **Smoke test the MCP layer** by listing tools without attaching a client, confirming all 13 registered and that importing `server` writes nothing to stdout:

   ```bash
   .venv/bin/python -c "
   import io, contextlib, sys
   buf = io.StringIO()
   with contextlib.redirect_stdout(buf):
       from job_tracker_mcp import server
   assert buf.getvalue() == '', f'server import polluted stdout: {buf.getvalue()!r}'
   print(len(server.mcp._tool_manager._tools), 'tools registered', file=sys.stderr)
   " 
   ```

   (The private `_tool_manager` attribute may move between fastmcp versions. If it does, fall back to `asyncio.run(server.mcp.get_tools())`. The stdout-purity assertion is the part that matters and must not be skipped.)
10. Back up the Claude Desktop config (`cp claude_desktop_config.json claude_desktop_config.json.bak`), merge the `job-tracker` entry with `json.load`/`json.dump` (never a hand-edited string), and re-parse the result to confirm it is valid JSON before writing.
11. Report the final config and tell Randy to ⌘Q and relaunch Claude Desktop.

---

## Error Handling Requirements

- Every tool is wrapped in `@_tool_guard`; failures return `{"error": "<message>"}` and never propagate.
- All logging goes to **stderr**. Nothing anywhere writes to stdout. See the "stdout is the MCP transport" section.
- All datetimes are stored as ISO 8601 strings; never bind a `datetime`/`date` object into SQLite.
- Validation errors (bad status, out-of-range `fit_score`, unknown id, invalid transition) return a **specific, actionable** message naming the offending value and the valid alternatives. `{"error": "invalid input"}` is not acceptable — the model reads these strings and needs to know how to retry.
- Constraint violations (`sqlite3.IntegrityError`) should be unreachable because Python validates first. If one surfaces, `_tool_guard` catches it and the traceback lands in the log; treat any occurrence as a bug.

---

## Testing

`tests/conftest.py` provides the isolation that makes everything else safe:

```python
import pytest
from job_tracker_mcp.database import Database

@pytest.fixture
def db(tmp_path):
    """A Database backed by a throwaway file. Never touches data/jobs.db."""
    database = Database(db_path=tmp_path / "test.db")
    yield database
    database.close()
```

Use a `tmp_path` file rather than `:memory:` — WAL mode and the `PRAGMA journal_mode` assertion behave differently in memory, and the tests should exercise the real configuration.

`tests/test_database.py`, at minimum:

- `test_add_job_new` — adds a job, verifies it exists with `status="new"`, `times_seen=1`.
- `test_add_job_duplicate` — adds the same job twice; second returns `result="duplicate"`, `times_seen=2`, `last_seen` updated, `date_seen` unchanged.
- `test_add_job_duplicate_preserves_triage` — set `fit_score=9`, re-add with `fit_score=2`; the stored score is still `9`.
- `test_add_job_duplicate_backfills_url` — first add with no url, second with a url; the url is stored.
- `test_id_normalization` — `("Acme  Corp", "Staff Engineer ", "REMOTE")` and `("acme corp", "staff engineer", "remote")` yield the same id and one row.
- `test_id_handles_none_location` — `generate_job_id("A", "B", None)` does not raise, and canonicalizes to `unknown`.
- `test_canonicalize_location` — parametrized over **every row of the worked-examples table** in the ID section, asserting the exact canonical output. This table is the specification; if a row and the code disagree, the code is wrong.
- `test_remote_variants_dedupe` — add the same company+title with `"Remote"`, then `"Remote, US"`, then `"United States (Remote)"`, then `"100% Remote"`, then `"WFH"`. Assert exactly one row exists, `times_seen == 5`, and the stored `location` is still `"Remote"` (first spelling wins).
- `test_restricted_remote_is_distinct` — `"Remote, US"` and `"Remote - EMEA"` for the same company+title produce **two** rows.
- `test_hybrid_is_not_remote` — `"Hybrid - Remote 2 days"` does not collide with `"Remote"`.
- `test_country_suffix_dropped` — `"Austin, TX, USA"` and `"Austin, TX"` produce one row; `"Austin, TX"` and `"Austin, Texas"` produce two (the documented limitation, pinned so a future change to it is deliberate).
- `test_location_canonical_stored` — the column is populated and equals `canonicalize_location(location)`.
- `test_mark_applied` — status becomes `applied`, `date_applied` is set.
- `test_mark_applied_appends_notes` — existing notes are preserved, new note appended.
- `test_mark_applied_rejects_rejected_job` — applying to a rejected job returns an `error`.
- `test_reject_job_with_company_pattern` — reject with `add_company_pattern=True`, add a second job from the same company, assert it lands as `status="rejected"` with `auto_rejected=True`.
- `test_reject_job_with_keyword_pattern` — same, via `title_keyword`; assert substring matching (`"staffing"` matches `"Staffing Coordinator"`).
- `test_reject_job_with_source_pattern` — assert **exact** source matching, and that `"Dice"` does not match a job with source `"Dice Inc"`.
- `test_pattern_matching_is_case_insensitive` — pattern `"infosys"` matches company `"Infosys BPM Ltd"`.
- `test_duplicate_pattern_is_not_an_error` — adding the same pattern twice returns `result="exists"`, and `get_rejected_patterns` shows one row.
- `test_remove_missing_pattern_errors` — removing id 999 returns an `error`.
- `test_remove_pattern_does_not_unreject` — an auto-rejected job stays rejected after its pattern is removed.
- `test_invalid_transition` — `update_job(status="new")` on an applied job returns an `error`; on a rejected job it succeeds.
- `test_update_job_no_fields_errors` — returns an `error`, and `updated_at` is unchanged.
- `test_update_job_applied_sets_date` — `update_job(status="applied")` populates `date_applied`.
- `test_fit_score_out_of_range` — `fit_score=11` returns an `error` and writes nothing.
- `test_invalid_status_filter` — `get_jobs(status="aplied")` returns an `error`, not an empty list.
- `test_get_jobs_pagination_total` — with 5 jobs and `limit=2`, `total==5` and `len(jobs)==2`; page 2 (`offset=2`) returns disjoint ids.
- `test_get_jobs_ordering` — unscored jobs sort after scored ones.
- `test_search_escapes_wildcards` — a job titled `"Engineer"` and one titled `"100% Remote"`; `search_jobs("100%")` returns exactly one row.
- `test_search_empty_query_errors`.
- `test_get_stats` — all four `by_status` keys present and zero-filled; `avg_fit_score_new` is `None` when nothing is scored; `unscored_new` correct.
- `test_bulk_add_jobs_mixed` — a batch with a new job, a duplicate, an auto-reject, and a malformed entry; verify the four counters and that `results` has the same length and order as the input.

Run with: `.venv/bin/python -m pytest tests/ -q`

---

## README.md

Must cover, briefly: what the server does, the two-MCP workflow with `apple-mail-mcp`, install/venv steps, how to run the tests, the `JOB_TRACKER_DB` env var, the Claude Desktop config snippet plus the ⌘Q-restart requirement, the log file path, and the status lifecycle diagram.

---

## Notes for Claude Code

- No email credentials anywhere in this project. This server never touches email; `apple-mail-mcp` does.
- `data/` and `*.db` are gitignored. Never commit them.
- The server is intentionally read-write; Claude Desktop controls access, so there is no read-only mode.
- All tools return JSON-serializable dicts. Never return a `sqlite3.Row`.
- The database file is the only durable state. It has no backup story. Mention this in the README; a periodic `cp data/jobs.db data/jobs.db.bak` is the user's call, not this server's job.
- Effort belongs in `database.py`. `server.py` is a thin adapter and `models.py` is nearly empty. The tests target `database.py` directly, without an MCP client in the loop.
- When finished, report to stderr/console: the resolved Python and fastmcp versions, the 13 registered tool names, the passing test count, and the final Claude Desktop config.
```
