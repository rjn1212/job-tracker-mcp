# job-tracker-mcp — Part 2: OneNote Integration Spec
## For Claude Code Implementation (do not start until Part 1 is green)

---

## Relationship to Part 1

This is an **additive** phase on the server built by `job-tracker-mcp-spec.md`. It adds a
second data source — the `Job Searching` section of the OneNote notebook `Randy's Notebook`
— and a reconciliation layer that answers one question:

> **Which jobs have I already applied to?**

Today that answer lives in two places that do not know about each other: the `jobs` table
(`status='applied'`, `date_applied`) and Randy's OneNote pages. Part 2 makes them agree.

**Constraints inherited from Part 1, non-negotiable:**

- `stdout` is the MCP transport. Never `print()`. All logging to `stderr`. This constraint
  now extends to **MSAL and httpx**, neither of which may be allowed to write to stdout.
- Every tool returns a JSON-serializable `dict`; failures are exactly `{"error": "..."}`.
- Every tool wrapped in `@_tool_guard`.
- `database.py` holds the logic; `server.py` is a thin adapter.
- Tests never touch `data/jobs.db`.
- **Fail toward a visible discrepancy, never a silent false match.** Part 1's governing
  principle, inherited verbatim. A duplicate row or an `ambiguous` entry is annoying and
  correctable; a wrong match quietly destroys the record of a job you applied to.

### Part 1 is shipped; this phase integrates with it

An earlier draft of this document declared that *"changes to Part 1 code are limited to
exactly three lines of `database.py`."* That was **scaffolding to keep this plan from
disturbing Part 1 while Part 1 was still being built.** Part 1 has shipped. The constraint has
expired, and honoring it now would produce a worse system: reconciliation would reimplement
normalization that already exists, and OneNote links would silently orphan themselves the
first time two jobs are merged.

Part 2 therefore modifies Part 1 where integration demands it. The changes are enumerated in
[Changes to Part 1](#changes-to-part-1), and each is small.

### What Part 1 actually shipped (this document was drafted against an older version)

| Assumption in the first draft | Reality |
|---|---|
| Part 1 has **13** tools; Part 2 adds 14–23 for 23 total | Part 1 has **15** tools. Part 2 adds **16–25**, for **25** total. |
| `jobs` has no normalized columns | `jobs` has indexed `company_canonical`, `title_canonical`, `location_canonical` |
| Matching should use `normalize_key_part()` | Use `canonicalize_company()` / `canonicalize_title()` — they strip legal suffixes and board decorations |
| Fuzzy title matching = `difflib.SequenceMatcher` | Part 1 removed that metric as **provably wrong**; use its `_title_similarity()` (Jaccard) |
| Jobs are deleted (`ON DELETE CASCADE` fires) | Jobs are **never** deleted. They are merged: `merged_into` points at the survivor. |
| `_migrate` is an unimplemented stub | Still true, and Part 2 implements 1→2 |

Every row of that table is a live defect if ignored. They are corrected in place below.

---

## Findings: How OneNote Is Actually Reachable On This Machine

Probed on 2026-07-09. These are measurements, not assumptions, and they eliminate three of
the four candidate approaches.

### 1. AppleScript / app scripting — **not possible**

```
/Applications/Microsoft OneNote.app          exists
Info.plist :NSAppleScriptEnabled             Does Not Exist
Info.plist :OSAScriptingDefinition           Does Not Exist
Contents/Resources/*.sdef                    no matches
```

OneNote for Mac ships **no scripting dictionary**. Unlike Microsoft Word or Excel, it is not
AppleScript-addressable. The only "scripting" left is UI automation through System Events
and the accessibility API: driving menus and reading `AXValue` off a WebView-backed canvas.
That approach cannot reliably enumerate pages, cannot read page content structurally,
breaks on every OneNote update, requires the app to be frontmost, and requires granting
Accessibility permission. **Rejected.**

### 2. Reading the local files — **not possible for this use case**

The notebook *is* on disk, and the target section does exist:

```
~/Library/Containers/com.microsoft.onenote.mac/Data/Library/Application Support/
    Microsoft User Data/OneNote/15.0/
    ├── Backup/Randy's Notebook/
    │     Job Searching (On 6-26-26).one      3,185,128 bytes
    │     Job Searching (On 7-3-26).one       3,307,040 bytes   <- newest
    │     Jobs (On 12-16-25).one              (older, renamed section)
    │     ...
    └── cache/                                430 opaque *.bin chunks
```

Four independent disqualifiers:

- **Stale.** Those are *backups*, written weekly. The newest is `7-3-26`; today is `7-9-26`.
  A six-day-old view of "what have I applied to" is worse than useless — it will tell you a
  job is un-applied when you applied to it on Monday.
- **No live files.** `find ... -name "*.one" -not -path "*/Backup/*"` returns nothing. Live
  content lives only in the 430-file `cache/*.bin` OneStore chunk store, an undocumented
  internal format keyed by revision GUIDs.
- **Binary and proprietary.** The `.one` header is
  `e4 52 5c 7b 8c d8 a7 4d ae b1 53 78 d0 29 96 d3` — GUID
  `{7B5C52E4-D88C-4DA7-AEB1-5378D02996D3}`, the MS-ONE section-file signature. Parsing it
  means implementing the `[MS-ONESTORE]` revision-store spec: a multi-week project to get a
  read-only, still-stale view.
- **Read-only by nature.** Even a perfect parser gives you *reading*. You asked to **create
  pages**. Writing into a sandboxed proprietary cache that a running OneNote process owns
  would corrupt the notebook and lose data. There is no write path here. **Rejected.**

### 3. Microsoft Graph API — **the answer**

`https://graph.microsoft.com/v1.0/me/onenote/...` reads and writes the *live* notebook,
supports personal Microsoft accounts, is a documented stable v1.0 surface, and supports both
required operations: enumerate/read pages in a section, and POST a new page into that
section.

**Verified against current Microsoft Learn docs (2026-07-09):**

| Fact | Value |
|---|---|
| Notebook/section name property (v1.0) | `displayName` — *not* `name` |
| Page name property | `title` |
| List pages | `GET /me/onenote/sections/{id}/pages` |
| Default page size | `$top=20`, ordered `lastModifiedTime desc` |
| Max page size | `$top=100`; beyond that follow `@odata.nextLink` |
| Read page content | `GET /me/onenote/pages/{id}/content` → HTML |
| Create page in a section | `POST /me/onenote/sections/{id}/pages`, `Content-Type: text/html` |
| Least-privilege scope, read | `Notes.Read` |
| Least-privilege scope, create | `Notes.Create` |
| Scope we will request | `Notes.ReadWrite` (covers both; `.All` is **not** needed) |
| **App-only auth** | **Removed from the OneNote API on 2025-03-31.** Delegated auth is mandatory. |

That last row is the single most important operational fact in this document, and it
dictates the entire auth design below: **there is no client-secret / daemon path.** A human
must sign in interactively, at least once.

### 4. `sectionName` query parameter — **forbidden**

Graph offers `POST /me/onenote/pages?sectionName=Job%20Searching` as a convenience. The docs
state plainly: *"If the section doesn't exist (or was renamed), the API will create a new
section."*

A typo, a rename, or a Unicode apostrophe would silently create a **second** section called
`Job Searching` and start filing pages into it. This tool must **always** POST to an explicit
resolved `sections/{id}`. Never use `sectionName`. This is a hard rule.

---

## Architecture

```
                       ┌──────────────────────────────────────┐
                       │            server.py                  │
                       │  (thin adapter, +10 tools, 25 total)  │
                       └───────────┬──────────────┬───────────┘
                                   │              │
                     ┌─────────────▼──┐      ┌────▼───────────────┐
                     │  database.py   │      │  onenote/client.py │
                     │  (Part 1)      │      │  (Part 2)          │
                     └─────────┬──────┘      └────┬───────────────┘
                               │                  │
                               │        ┌─────────▼──────────┐
                               │        │  onenote/graph.py  │  retry, paging, 429
                               │        └─────────┬──────────┘
                               │                  │
                               │        ┌─────────▼──────────┐
                               │        │  onenote/auth.py   │  MSAL, Keychain
                               │        └─────────┬──────────┘
                               │                  │
                               │                  ▼
                               │        Microsoft Graph /me/onenote
                               │
                     ┌─────────▼────────────────────────┐
                     │        data/jobs.db (v2)          │
                     │  jobs, rejected_patterns          │  ← Part 1, schema untouched
                     │  onenote_pages   (content cache)  │  ← Part 2, new
                     │  onenote_links   (page ↔ job)     │  ← Part 2, new
                     │  onenote_state   (resolved ids)   │  ← Part 2, new
                     └──────────────────┬───────────────┘
                                        │
                              ┌─────────▼──────────┐
                              │ onenote/reconcile  │  jobs × pages → truth
                              └────────────────────┘
```

Two rules the diagram encodes:

1. **The Graph layer is downstream of nothing.** Importing `server.py` must not construct a
   Graph client, read a token, or open a socket. A user with no network, no Azure app, and
   no token still gets all 15 Part-1 tools working perfectly. OneNote tools return a clean
   `{"error": "Not authenticated. Run ./onenote-auth.sh"}`.
2. **OneNote content is cached locally in `jobs.db`.** Reconciliation runs against the cache,
   not against the network. `onenote_sync` is the only tool that must be online.

### New file layout (additive)

```
job_tracker_mcp/
├── __init__.py
├── server.py                    # +10 tools
├── database.py                  # +3 lines (SCHEMA_VERSION, _migrate, fresh-create)
├── models.py                    # unchanged
└── onenote/                     # NEW package
    ├── __init__.py
    ├── auth.py                  # MSAL public client, token cache in Keychain
    ├── graph.py                 # httpx transport: retry, backoff, paging, throttling
    ├── client.py                # OneNoteClient: resolve/list/read/create
    ├── htmlio.py                # HTML -> text (stdlib), text -> OneNote page HTML
    ├── store.py                 # OneNoteStore: the 3 new tables, all SQL
    ├── service.py               # orchestration: env, resolution, sync phases, reconcile
    └── reconcile.py             # matching + diff, pure functions

onenote-auth.sh                  # NEW: interactive one-time sign-in wrapper
tests/
├── test_onenote_htmlio.py       # NEW
├── test_onenote_store.py        # NEW
├── test_onenote_client.py       # NEW (httpx.MockTransport, zero network)
└── test_reconcile.py            # NEW
```

---

## Phase 0 — One-Time Manual Prerequisites (Randy does these, not Claude Code)

Claude Code **cannot** do these; they require a browser and a Microsoft sign-in. Claude Code
should print these instructions and stop if `JOB_TRACKER_MS_CLIENT_ID` is unset.

1. Go to <https://entra.microsoft.com> → **App registrations** → **New registration**.
   - Name: `job-tracker-mcp`
   - Supported account types: **"Accounts in any organizational directory and personal
     Microsoft accounts"** — this is required. `Randy's Notebook` is on a personal MSA
     (evidenced by `UserInfoCache/aba4492a4792d1b9_LiveId.db`), but an
     `..._ADAL.db` is also present, so a work account may be signed in too. Choosing "any"
     means the authority is `common` and either account can be selected at sign-in.
   - Redirect URI: leave blank for now.
2. **Authentication** → **Add a platform** → **Mobile and desktop applications** →
   check `https://login.microsoftonline.com/common/oauth2/nativeclient`, and set
   **"Allow public client flows" = Yes**. This enables the device-code flow.
   **Do not create a client secret.** A public client must not have one; a secret in a
   config file on a laptop is a liability with no upside.
3. **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated
   permissions** → `Notes.ReadWrite`. Not `Notes.ReadWrite.All` — that is tenant-wide and
   grants access to notebooks this tool has no business seeing.
4. Copy the **Application (client) ID**.
5. `export JOB_TRACKER_MS_CLIENT_ID=<that-guid>` in your shell profile **and** add it to the
   Claude Desktop config `env` block (Claude Desktop does not read your shell profile).

---

## Authentication Design

### The stdio problem, and why auth is a separate binary

Device-code flow requires showing the user a code and a URL, then polling while they sign in
a browser. That is an interactive terminal conversation.

**The MCP server can never do this.** It is a stdio JSON-RPC process launched by Claude
Desktop with no TTY; its stdout is the protocol. Prompting there would corrupt the transport
and hang forever waiting on a stdin that belongs to Claude Desktop.

Therefore auth is **strictly out-of-band**:

```
./onenote-auth.sh          # run once, in a real terminal, by a human
```

It performs the device-code flow, writes a refresh token into the macOS Keychain, and exits.
From then on the MCP server silently acquires access tokens from the cached refresh token.
When the refresh token eventually expires (~90 days of inactivity, or on password change),
every OneNote tool returns:

```json
{"error": "OneNote is not authenticated, or the saved sign-in expired. Run /Users/randyn/projects/git/job-tracker-mcp/onenote-auth.sh in a terminal, then retry."}
```

The message names the exact command. The model reads these strings; a vague error means a
useless retry loop.

### `onenote/auth.py`

```python
SCOPES = ["Notes.ReadWrite"]           # NOT Notes.ReadWrite.All
AUTHORITY = "https://login.microsoftonline.com/common"
_KEYRING_SERVICE = "job-tracker-mcp"
_KEYRING_USER = "msal-token-cache"
```

- `msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache)`.
- **Token cache persistence:** `msal.SerializableTokenCache`, serialized to the **macOS
  Keychain** via `keyring` (confirmed present: `keyring 25.7.0`, arriving transitively via
  `fastmcp` → `fastmcp-slim`; declare it explicitly in `requirements.txt` regardless, because
  depending on a transitive dep is how builds break).
  Fallback if `keyring` raises (headless / no Keychain): a file at `~/.job-tracker-mcp/
  msal_cache.json` created with `os.open(..., O_CREAT|O_EXCL|O_WRONLY, 0o600)`. Never inside
  the repo. Add `.job-tracker-mcp/` to `.gitignore` anyway, belt and braces.

> ### A Keychain prompt will hang the server, and it does not raise
>
> The fallback above triggers "if `keyring` raises." **A macOS *"allow access"* dialog does
> not raise — it blocks.** Indefinitely. In a process Claude Desktop launched with no TTY, no
> window, and no one to click the button.
>
> This is not hypothetical. The Keychain item's ACL is bound to the binary that created it.
> `onenote-auth.sh` writes it with `.venv/bin/python`. Delete and recreate `.venv`, upgrade
> Python, or move the checkout, and the reading binary no longer matches — macOS then prompts,
> and the MCP server hangs on startup of the first OneNote tool call.
>
> Mitigations, all three:
> - Read the cache **on a worker thread with a hard timeout** (5s). On timeout, do not wait:
>   return the standard *"run `onenote-auth.sh`"* error. A hung tool is strictly worse than a
>   failed one, because the model cannot see it.
> - `onenote_auth_status` performs the same timed read and reports
>   `{"authenticated": false, "reason": "keychain_timeout"}` — a *diagnosis*, not an error.
> - The README states plainly: **rebuilding `.venv` requires re-running `onenote-auth.sh`.**
- `acquire_token_silent(SCOPES, account=accounts[0])` on every call. MSAL handles refresh and
  the 5-minute-early renewal internally. Do not cache the access token yourself.
- **Multi-account:** `get_accounts()` may return more than one (personal + work). Persist the
  chosen `home_account_id` into `onenote_state` at auth time and select by it thereafter.
  Silently taking `accounts[0]` is how this tool ends up reading the wrong notebook.
- **stdout purity:** MSAL logs via the `msal` logger. In `server.py`, before any import of
  the onenote package, add `logging.getLogger("msal").setLevel(logging.WARNING)` and confirm
  no handler writes to stdout. `httpx` and `httpcore` likewise. The Part-1 smoke test that
  asserts `buf.getvalue() == ''` on import must be extended to cover `onenote` imports.
- **Never log a token.** `graph.py` must redact `Authorization` headers before any
  `log.debug` of a request. Write a `_redact()` helper and use it; do not rely on discipline.

### `onenote-auth.sh`

```bash
#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python -m job_tracker_mcp.onenote.auth
```

`auth.py`'s `__main__` block is the **only** place in this project permitted to call
`print()`, because it is not the MCP server. Guard it: the module-level code must not print,
only `if __name__ == "__main__":` may.

---

## Configuration

All via environment variables, all read at **first use**, not at import.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `JOB_TRACKER_MS_CLIENT_ID` | yes (for OneNote tools) | — | Azure app registration client id |
| `JOB_TRACKER_ONENOTE_NOTEBOOK` | no | `Randy's Notebook` | Notebook `displayName` |
| `JOB_TRACKER_ONENOTE_SECTION` | no | `Job Searching` | Section `displayName` |
| `JOB_TRACKER_ONENOTE_WRITE` | no | `0` | `1` enables `onenote_create_page` |
| `JOB_TRACKER_GRAPH_TIMEOUT` | no | `30` | httpx timeout, seconds |
| `JOB_TRACKER_DB` | no | `<root>/data/jobs.db` | inherited from Part 1 |

**`JOB_TRACKER_ONENOTE_WRITE` defaults to off.** Writing into a personal notebook is the one
irreversible thing this server can do — Graph has no "undo page creation," and there is no
delete tool here by design. Creating a page must be an act the user opted into by editing
their config, not something a model can do on its first day. When it is off,
`onenote_create_page` returns an error that explains how to enable it.

---

## Resolving the Notebook and Section

The section id is the load-bearing value. Resolve it once, cache it, and validate it.

```
GET /me/onenote/notebooks?$select=id,displayName
GET /me/onenote/notebooks/{nb_id}/sections?$select=id,displayName
GET /me/onenote/notebooks/{nb_id}/sectionGroups/{sg_id}/sections   (only if needed)
```

**Do not use `$filter=displayName eq 'Randy''s Notebook'`.** OneNote's OData `$filter`
support is uneven, and — more to the point — the notebook name contains an apostrophe, which
must be escaped by **doubling** it inside an OData string literal. That is a bug waiting in
ambush. Fetch the (small) list and match in Python using `normalize_key_part()` from
`models.py`, which already does casefolding and whitespace collapsing. Reuse, don't reinvent.

**Ambiguity is an error, not a coin flip.** If two notebooks or two sections normalize to the
same name, return
`{"error": "Ambiguous: 2 sections named 'Job Searching' (ids: X, Y). Set JOB_TRACKER_ONENOTE_SECTION_ID to disambiguate."}`
and support a `JOB_TRACKER_ONENOTE_SECTION_ID` override that skips resolution entirely.

**Sections may live in section groups.** The backup evidence
(`Backup/Randy's Notebook/Job Searching (On 7-3-26).one` — one `.one` file per section, sitting
directly under the notebook folder) says `Job Searching` is a **top-level section**, not a
section group. Resolve top-level sections first; only recurse into `sectionGroups` if no
match is found, and cap recursion depth at 3.

Persist `notebook_id`, `section_id`, `home_account_id`, and the `displayName`s they resolved
from into `onenote_state`. On every subsequent run, re-validate cheaply with
`GET /me/onenote/sections/{id}?$select=id,displayName`. If the name changed, **do not
silently follow the id** — a renamed section may mean the user reorganized. Report it and let
them confirm. (Note the `Jobs (On 12-16-25).one` backup: this section was already renamed once,
from `Jobs` to `Job Searching`. This will happen again.)

---

## `onenote/graph.py` — The HTTP Layer

A thin, testable wrapper. **No Microsoft SDK** (`msgraph-sdk` drags in a large async stack and
a Kiota runtime for what is four HTTP calls). `httpx` is already installed and is the only
transport.

```python
class GraphClient:
    def __init__(self, token_provider: Callable[[], str],
                 transport: httpx.BaseTransport | None = None,
                 timeout: float = 30.0): ...
    def get_json(self, path: str, params: dict | None = None) -> dict: ...
    def get_all(self, path: str, params: dict | None = None) -> Iterator[dict]: ...  # follows @odata.nextLink
    def get_text(self, path: str, params: dict | None = None) -> str: ...            # page content HTML
    def post_html(self, path: str, html: str) -> dict: ...
```

`transport` being injectable is the entire test strategy: `httpx.MockTransport` gives
hermetic tests with **zero network**, and needs no new dependency (no `responses`, no
`respx`, no VCR).

**Retry policy** — a `_request` helper wraps every verb:

- `429 Too Many Requests` → honor the `Retry-After` header **exactly**. Graph means it. If the
  header is absent, back off `2**attempt` seconds.
- `503`, `504`, and `httpx.TransportError` → exponential backoff with full jitter,
  `min(2**attempt + random.uniform(0, 1), 60)`.
- Max 5 attempts, then return a structured error naming the status and the Graph
  `error.code`/`error.message` from the response body. Graph's error bodies are genuinely
  informative; surface them rather than swallowing them into "request failed".
- `401` → attempt exactly **one** silent token refresh, retry once, then surface the
  "run onenote-auth.sh" message. Never loop on 401.
- `404` on a cached `section_id` → the section was deleted or moved. Clear
  `onenote_state`, return an actionable error. Do not auto-re-resolve; that can silently
  retarget a different section.

**Paging:** `get_all` follows `@odata.nextLink` until absent. It must **not** re-append
`$top`/`$select` to the nextLink URL — Graph embeds the full query there, and appending
duplicates causes a 400. Request the nextLink verbatim.

**Page size:** request `$top=100` (the documented maximum) with an explicit
`$select=id,title,createdDateTime,lastModifiedDateTime,links`. Relying on the default
`$top=20` triples the round-trips; relying on the default `$select` needlessly expands
`parentSection` on every row.

---

## Changes to Part 1

Small, enumerated, and each one required by integration rather than convenience.

| File | Change | Why |
|---|---|---|
| `database.py` | `SCHEMA_VERSION = 2` | new tables |
| `database.py` | `_init_schema` also runs `ONENOTE_SCHEMA_SQL` on fresh create | new tables |
| `database.py` | `_migrate` implements 1→2 (was `NotImplementedError`) | Randy's live db is v1 and populated |
| `database.py` | `merge_jobs` re-points `onenote_links` from duplicate to survivor | otherwise a merge orphans a page's evidence |
| `database.py` | `unmerge_job`'s message states that links stay with the survivor | honesty about a non-inverse |
| `database.py` | `_title_similarity` gains a docstring noting Part 2 depends on it | changing it now changes reconciliation |
| `server.py` | `logging.getLogger("msal").setLevel(WARNING)` before any onenote import | stdout purity |
| `.gitignore` | `.job-tracker-mcp/`, `*.pre-v2` | token-cache fallback, migration backup |

`jobs` and `rejected_patterns` gain **no new columns** — that part of the original goal
survives, and for the original reason: Part 1's persisted natural key is versioned, and adding
a column that participates in matching would drag Part 2 into that versioning hazard.
Reconciliation reads the canonical columns Part 1 already maintains.

The three new tables live in the **same** `jobs.db`, because `onenote_links.job_id` must be a
real foreign key into `jobs(id)`. A second database file cannot do that.

### Schema v2 and the migration

```python
SCHEMA_VERSION = 2                                   # was 1

def _init_schema(self):
    with self._lock, self.conn:
        current = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if current == 0:
            self.conn.executescript(SCHEMA_SQL)
            self.conn.executescript(ONENOTE_SCHEMA_SQL)      # <- added
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif current < SCHEMA_VERSION:
            self._migrate(current, SCHEMA_VERSION)           # called INSIDE this txn
        ...

def _migrate(self, frm: int, to: int) -> None:
    # No `with self._lock, self.conn:` here -- _init_schema already holds both.
    # RLock is reentrant, but re-entering `with self.conn` commits early and obscures
    # the control flow.
    if frm == 1 and to == 2:
        self.conn.executescript(ONENOTE_SCHEMA_SQL)          # additive only
        self.conn.execute("PRAGMA user_version = 2")
        return
    raise NotImplementedError(f"No migration path from v{frm} to v{to}")
```

> ### The migration is not atomic, and that is fine — for a reason worth writing down
>
> The first draft implied the migration was transactional. **It is not.** Python's
> `sqlite3.executescript()` **issues an implicit `COMMIT` before it runs**, discarding any
> pending transaction. Verified on this machine's Python 3.11.11:
>
> ```
> in_transaction before executescript: True
> in_transaction after  executescript: False
> ```
>
> So `executescript(ONENOTE_SCHEMA_SQL)` commits the new tables, and `PRAGMA user_version = 2`
> lands afterward. A crash in the gap leaves the v2 tables present with `user_version` still 1.
>
> This is safe **because the migration is idempotent, not because it is atomic.** Every
> statement is `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`, so the next open
> re-runs it harmlessly and sets the version. Any future migration that is *not* idempotent —
> one that `ALTER`s, back-fills, or drops — cannot rely on this and must checkpoint
> differently. Do not copy this pattern without re-reading this paragraph.

The 1→2 migration touches no existing row, so it is safe to run on Randy's live `data/jobs.db`.
Take a backup anyway: `cp data/jobs.db data/jobs.db.pre-v2`. Part 1's README notes there is no
backup story; this is the moment it matters.

**`PRAGMA foreign_keys = ON` is already set per-connection by Part 1** — the `ON DELETE
CASCADE` below is live, not decorative. Verify it in a test; a FK that silently does nothing is
worse than no FK. (`onenote_links` → `onenote_pages` is the cascade that will actually fire;
the one into `jobs` will not, because Part 1 never deletes a job.)

### `ONENOTE_SCHEMA_SQL`

```sql
-- Local mirror of pages in the target section. Content is fetched lazily.
CREATE TABLE IF NOT EXISTS onenote_pages (
    page_id           TEXT PRIMARY KEY,      -- Graph page id, opaque
    section_id        TEXT NOT NULL,
    title             TEXT NOT NULL,
    created_dt        TEXT NOT NULL,         -- Graph UTC, e.g. "2026-07-03T17:54:27Z"
    last_modified_dt  TEXT NOT NULL,         -- Graph UTC; the cache-invalidation key
    content_html      TEXT,                  -- NULL until first content fetch
    content_text      TEXT,                  -- NULL until first content fetch
    content_synced_dt TEXT,                  -- the last_modified_dt the content matches
    web_url           TEXT,
    client_url        TEXT,
    is_ignored        INTEGER NOT NULL DEFAULT 0,   -- not an application record
    first_seen_at     TEXT NOT NULL,         -- local ISO w/ offset
    last_seen_at      TEXT NOT NULL,
    deleted_at        TEXT,                  -- tombstone: vanished from Graph

    CHECK (is_ignored IN (0, 1)),
    CHECK (length(title) >= 0)
);

CREATE INDEX IF NOT EXISTS idx_onp_section  ON onenote_pages(section_id);
CREATE INDEX IF NOT EXISTS idx_onp_modified ON onenote_pages(last_modified_dt DESC);
CREATE INDEX IF NOT EXISTS idx_onp_live     ON onenote_pages(deleted_at, is_ignored);

-- The join. A page may describe several jobs; a job may appear on several pages.
CREATE TABLE IF NOT EXISTS onenote_links (
    page_id      TEXT NOT NULL REFERENCES onenote_pages(page_id) ON DELETE CASCADE,
    job_id       TEXT NOT NULL REFERENCES jobs(id)               ON DELETE CASCADE,
    match_kind   TEXT NOT NULL,   -- marker | exact | fuzzy | manual | created_by_tool
    confidence   REAL NOT NULL,   -- 1.0 for marker/manual/created_by_tool
    evidence     TEXT,            -- the snippet or reasoning that produced the match
    date_applied TEXT,            -- ISO date the PAGE says the application happened
    extracted    TEXT,            -- JSON blob of whatever was parsed/read off the page
    created_at   TEXT NOT NULL,

    PRIMARY KEY (page_id, job_id),
    CHECK (match_kind IN ('marker','exact','fuzzy','manual','created_by_tool')),
    CHECK (confidence >= 0.0 AND confidence <= 1.0)
);

CREATE INDEX IF NOT EXISTS idx_onl_job ON onenote_links(job_id);

-- Resolved ids, account selection, sync watermarks. Small key/value.
CREATE TABLE IF NOT EXISTS onenote_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL
);
```

Notes on shape:

- `content_synced_dt` vs `last_modified_dt` is the whole incremental-sync mechanism. Content
  is refetched **only when** `content_synced_dt IS NULL OR content_synced_dt != last_modified_dt`.
  A `GET /pages/{id}/content` is the expensive call and the one that gets you throttled.
- `deleted_at` is a **tombstone, not a `DELETE`**. If a page disappears from Graph, its links
  and any reconciliation history remain inspectable. Cascading a real delete would silently
  destroy the record of why a job was marked applied.
- `is_ignored` exists because a section called "Job Searching" contains more than
  applications: resume drafts, interview prep, salary research, a list of recruiters. The
  reconciler must be told to skip those, once, permanently.
- `onenote_links.job_id` is `NOT NULL`. An *unmatched* page is not a link with a null job —
  it is simply the absence of a row. Modelling "no match" as a row invites `LEFT JOIN` bugs.

- **`date_applied` on the link is what makes Tier-B extraction work.** Tier B is declared the
  *primary* path: the model reads an unparsed page and calls `onenote_link_page`. Without a
  date on the link row, reconciliation has nothing to pass to `mark_applied` and would record
  **today** as the day you applied — permanently, and wrongly, for an application you made in
  June. The date the page asserts belongs on the link, alongside the `extracted` JSON that
  justifies it.

- **`ON DELETE CASCADE` into `jobs(id)` is correct but will never fire.** Part 1 never deletes
  a job; it merges them (`merged_into`). Keep the FK — it costs nothing and documents intent —
  but the live hazard is a *merged* job, not a deleted one. See below.

### `merged_into` is the hazard the first draft did not know about

Part 1 shipped after this document was first drafted, and it can now merge two jobs: the
duplicate keeps its row, gains `merged_into = <survivor>`, and disappears from every listing.

Link a page to job **B**, then merge **B** into **A**, and the page's evidence now hangs off a
row that `get_jobs` will never return. Reconciliation would surface it, propose
`mark_applied(B)`, and Part 1 would refuse with *"job B was merged into A."* The link is
orphaned in place, silently.

**`merge_jobs` must therefore carry the links across.** Inside its existing transaction, after
re-pointing merge pointers:

```sql
-- A page already linked to BOTH rows must not violate PRIMARY KEY(page_id, job_id).
UPDATE OR IGNORE onenote_links SET job_id = :keep WHERE job_id = :dup;
DELETE FROM onenote_links WHERE job_id = :dup;      -- whatever OR IGNORE skipped
```

`UPDATE OR IGNORE` moves every link it can; the `DELETE` reaps the collisions it skipped
(those pages were already linked to the survivor, so no evidence is lost). `unmerge_job` does
**not** restore the links — they now legitimately describe the survivor, and a page's evidence
following the job it documents is the behavior you want. Say so in `unmerge_job`'s message.

Every reconciliation query filters `merged_into IS NULL`. `onenote_link_page` refuses a
`job_id` whose row is merged, naming the survivor.

---

## HTML Handling — `onenote/htmlio.py`

### Reading: OneNote HTML → text

Graph returns page content as a full HTML document. Its body is a pile of absolutely
positioned `<div>`s, each with a `data-id`, plus inline styles, `<img>` tags pointing at
authenticated resource URLs, and `<object data-attachment=...>` for file attachments.

Use **`html.parser` from the standard library**. No `beautifulsoup4`, no `lxml`. Part 1's
spec sets a "no unnecessary dependencies" bar and an HTML-to-text extractor is ~80 lines.

Requirements:

- Drop `<script>`, `<style>`, `<head>` entirely.
- **Never fetch `<img>` or `<object>` sources.** They are authenticated Graph URLs pointing
  at binary blobs. Emit a placeholder — `[image]`, `[attachment: Resume.pdf]` — and move on.
  Downloading a résumé PDF to turn it into text is not in scope and burns throttle budget.
- Preserve structure that carries meaning:
  - `<table>` → one line per `<tr>`, cells joined by ` | `. Application logs are tables far
    more often than prose, and column alignment is the signal.
  - `<li>` → `- ` prefix.
  - `<p>`, `<div>`, `<br>`, `<h1>`–`<h6>` → line breaks.
- Unescape entities (`&amp;` → `&`), collapse runs of whitespace **within** a line, collapse
  3+ blank lines to 2. Do not collapse newlines globally; the line structure is the data.
- Return `str`. Store it in `onenote_pages.content_text`.

### Writing: text → OneNote page HTML

```python
def build_page_html(title: str, body_lines: list[str], created: str | None = None,
                    marker: str | None = None) -> str:
```

The exact document Graph expects. **`title` is not a parameter of the POST — Graph reads it
from `<title>` in `<head>`.** Omit the head and the page is created untitled; the first draft
never pinned this shape.

```html
<!DOCTYPE html>
<html>
  <head>
    <title>Applied: Staff Engineer @ Acme Corp</title>
    <meta name="created" content="2026-07-09T16:29:04-07:00" />
  </head>
  <body>
    <p>Applied via the company site.</p>
    <p>Recruiter: Dana</p>
    <p><i>job-tracker-id: a1b2c3d4e5f60718</i></p>
  </body>
</html>
```

- Escape `title` and every body line with `html.escape(..., quote=True)`. A job title
  containing `&` or `<` — `R&D Engineer`, `Engineer <Backend>` — must not produce broken
  markup or, worse, inject markup. This is the injection boundary of Part 2, and the content
  arrives from job-alert emails, which are attacker-influenced.
- `<meta name="created" content="{ISO 8601 with offset}">` — Graph honors this and it is how
  the page gets a sensible timestamp. Use Part 1's `now_str()`; it already attaches the local
  offset via `.astimezone()`.
- The **marker**: append a final paragraph

  ```html
  <p><i>job-tracker-id: a1b2c3d4e5f60718</i></p>
  ```

  This is a visible, plain-text, human-readable footer. It is deliberately **not** a
  `<meta>` tag or a `data-` attribute: OneNote's HTML sanitizer is not contractually obliged
  to round-trip those, and a marker that silently vanishes turns idempotency into duplicate
  pages. Text in the body survives. It is slightly ugly. Correctness wins.

  On read, `MARKER_RE = re.compile(r"job-tracker-id:\s*([0-9a-f]{16})")` against
  `content_text` yields a `match_kind='marker'`, `confidence=1.0` link. This is the only
  100%-reliable page↔job join, and every page this tool creates has one.

---

## MCP Tools — Part 2

Ten new tools (16–25). Server total: **25** (Part 1 shipped 15, not the 13 this document
first assumed).

Every one is `@mcp.tool` + `@_tool_guard`, returns a plain dict, and follows Part 1's error
envelope. Every docstring states when to prefer it over a sibling — that text is what the
model actually reads.

---

### Tool 16: `onenote_auth_status`

**Purpose:** Diagnose the OneNote connection without touching a notebook. The first tool to
call when anything OneNote-shaped fails.

**Parameters:** none

**Returns:**
```json
{
  "configured": true,
  "authenticated": true,
  "account": "randyn@r-nelson.com",
  "account_type": "personal",
  "scopes": ["Notes.ReadWrite"],
  "notebook": {"display_name": "Randy's Notebook", "id": "1-abc...", "resolved": true},
  "section":  {"display_name": "Job Searching",    "id": "1-def...", "resolved": true},
  "write_enabled": false,
  "cached_pages": 47,
  "last_sync": "2026-07-09T09:14:02-07:00"
}
```

`configured: false` when `JOB_TRACKER_MS_CLIENT_ID` is unset — and then `message` carries the
Phase-0 instructions verbatim. Never returns `{"error": ...}` for the un-configured case;
"you have not set this up yet" is a **successful** diagnosis, and an error string would send
the model looking for a bug.

Makes at most one cheap Graph call (`GET /me?$select=userPrincipalName`). Never triggers
interactive auth.

---

### Tool 17: `onenote_list_sections`

**Purpose:** One-time discovery. Enumerate notebooks and their sections so the user can
confirm the exact `displayName`s, or find the right ids when names are ambiguous.

**Parameters:** `notebook: str | None = None` (filter by normalized display name)

**Returns:**
```json
{
  "notebooks": [
    {"id": "1-abc...", "display_name": "Randy's Notebook", "is_default": true,
     "sections": [
       {"id": "1-def...", "display_name": "Job Searching", "page_count": null,
        "path": "Randy's Notebook / Job Searching"},
       {"id": "1-ghi...", "display_name": "AI Article", "path": "Randy's Notebook / AI Article"}
     ]}
  ],
  "total_sections": 12
}
```

`path` includes any section-group ancestry. This is the tool that resolves the "is it a
section or a section group" question empirically, on the live notebook, rather than by
inference from backup filenames.

---

### Tool 18: `onenote_sync`

**Purpose:** Refresh the local mirror of the `Job Searching` section. **The only tool that
requires the network.** Everything else reads the cache.

**Parameters:**
```
full          bool  optional  default False   (refetch content for every page, ignoring the cache)
fetch_content bool  optional  default True    (False = metadata only; fast, cheap)
max_pages     int   optional  default 200, clamped [1, 1000]
```

**Returns:**
```json
{
  "result": "synced",
  "section": "Job Searching",
  "pages_seen": 47,
  "pages_new": 2,
  "pages_updated": 1,
  "pages_unchanged": 44,
  "pages_tombstoned": 0,
  "content_fetched": 3,
  "content_skipped_cached": 44,
  "duration_ms": 4180,
  "throttled_retries": 0,
  "message": "3 pages fetched; 44 served from cache."
}
```

**Logic, in three strictly separated phases.** The phase boundary is the whole design.

**Phase R — read the DB, release the lock.** One short transaction: load the current
`(page_id, last_modified_dt, content_synced_dt)` for the section. If `full=True`, treat every
`content_synced_dt` as `NULL`.

**Phase N — network, holding no lock and no transaction.**
1. Resolve/validate `section_id` (see [Resolving](#resolving-the-notebook-and-section)).
2. `GET /me/onenote/sections/{id}/pages?$top=100&$select=id,title,createdDateTime,lastModifiedDateTime,links`,
   following `@odata.nextLink`. Stop at `max_pages` and, if more remain,
   **say so in `message`** — a silently truncated sync that reports success is how a
   reconciliation report ends up confidently wrong.
3. For each enumerated page where `fetch_content` and
   `content_synced_dt IS NULL OR content_synced_dt != last_modified_dt`:
   `GET /me/onenote/pages/{id}/content`, run `html_to_text`, accumulate **in memory**.

**Phase W — one short write transaction, no network inside it.**
4. Upsert every enumerated page into `onenote_pages`; set `last_seen_at = now`; write the
   fetched `content_html` / `content_text` and set `content_synced_dt = last_modified_dt`.
5. Any non-tombstoned page in the table whose `page_id` was **not** returned by the
   enumeration gets `deleted_at = now`. Never `DELETE`.

> ### Never hold the database lock across an HTTP call
>
> The first draft said *"all of steps 3–5 run in one transaction"* — with the content fetch
> as step 4, **inside** it. That is a production hang, not a nit.
>
> Part 1's `Database` serializes **every** method on a single `threading.RLock` over a single
> `sqlite3.Connection`. A sync of 40 changed pages would hold that lock, and an open SQLite
> write transaction, across 40 sequential network round-trips. For the duration — seconds on a
> good link, minutes on a bad one, indefinitely if Graph stalls before the httpx timeout — every
> other tool in the server blocks. `get_jobs` blocks. `add_job` blocks. The server looks dead.
>
> Phase N therefore takes no lock and opens no transaction. Phase W is a single fast commit.
> A sync interrupted during Phase N leaves the database exactly as it was; interrupted during
> Phase W, the transaction rolls back. Neither can leave half the section tombstoned.
>
> `test_sync_holds_no_lock_during_http` asserts this directly: the mock transport's handler
> tries `db._lock.acquire(blocking=False)` mid-request and fails the test if it cannot.

6. `full=True` forces a refetch of everything (Phase R reports every page as uncached). This is
   the escape hatch for when the local text extractor has been improved and the cache is stale
   *with respect to the parser*, not with respect to Graph — a real scenario the
   `last_modified_dt` key cannot detect.

Phase N is also where `throttled_retries` accumulates, and where a `max_pages` truncation is
recorded so Phase W can surface it in `message`.

---

### Tool 19: `onenote_list_pages`

**Purpose:** List cached pages in the section. Reads the local mirror; **never** hits the
network. Call `onenote_sync` first if you need it fresh.

**Parameters:**
```
include_ignored bool  optional  default False
include_deleted bool  optional  default False
modified_since  str   optional  ISO date "YYYY-MM-DD"
limit           int   optional  default 50, clamped [1, 200]
offset          int   optional  default 0
```

**Returns:** Part 1's pagination envelope, exactly.
```json
{
  "pages": [
    {"page_id": "1-x...", "title": "Applications — July 2026",
     "created_dt": "2026-07-01T14:02:11Z", "last_modified_dt": "2026-07-08T22:15:03Z",
     "has_content": true, "is_ignored": false, "linked_job_ids": ["a1b2c3d4e5f60718"],
     "web_url": "https://onedrive.live.com/..."}
  ],
  "total": 47, "offset": 0, "limit": 50
}
```

Default order: `last_modified_dt DESC, page_id ASC`. The trailing `page_id ASC` makes the
order total so pagination cannot skip or repeat a row — same reasoning as Part 1's `id ASC`.

`content_text` is **not** included. A 47-page dump would obliterate the model's context. Use
`onenote_get_page`.

---

### Tool 20: `onenote_get_page`

**Purpose:** Read one page's content as plain text.

**Parameters:**
```
page_id   str   required
format    str   optional  "text" (default) | "html"
max_chars int   optional  default 20000, clamped [500, 100000]
offset    int   optional  default 0   (character offset, for paging through a long page)
```

**Returns:**
```json
{
  "page_id": "1-x...", "title": "Applications — July 2026",
  "format": "text", "content": "...", "chars": 20000, "total_chars": 34112,
  "truncated": true, "offset": 0, "next_offset": 20000,
  "last_modified_dt": "2026-07-08T22:15:03Z", "stale": false
}
```

- `stale: true` when `content_synced_dt != last_modified_dt` — the cached text predates the
  page's latest edit. Report it; do not silently auto-sync inside a read tool.
- `truncated` + `next_offset` let the model page through a long log without a second
  mechanism. Returning a silently-cut string is how a model concludes "there are only 40
  applications" when there are 300.
- `format="html"` returns the raw stored HTML. Rarely wanted; the text is what gets reasoned
  over. Say so in the docstring.

---

### Tool 21: `onenote_search_pages`

**Purpose:** Substring search across cached page titles and text. Finds "did I write anything
about Acme?" without pulling every page into context.

**Parameters:** `query: str` (required, non-empty), `limit`, `offset`.

**Returns:** `pages` (as in `onenote_list_pages`) plus a `snippets` array: up to 3 matched
lines per page, ±100 characters of surrounding context.

**Logic:** `LIKE '%q%'` over `title` and `COALESCE(content_text,'')`, both sides `LOWER()`ed,
wildcards escaped with Part 1's `like_escape()` (import it; do not write a second copy). An
empty query is an error, not a full scan. Excludes tombstoned pages.

Snippets rather than full pages: a search over 47 pages that returns 47 page bodies is not a
search, it is a denial-of-service on the context window.

---

### Tool 22: `onenote_create_page`

**Purpose:** Create a new page in the `Job Searching` section. **The only tool that writes to
OneNote.** Gated behind `JOB_TRACKER_ONENOTE_WRITE=1`.

**Parameters:**
```
title      str        required
body_lines list[str]  required   (plain text; one paragraph per line)
job_id     str        optional   (link the page to a tracked job; embeds the marker)
confirm    bool       optional   default False
```

**Returns:**
```json
{
  "result": "created",
  "page_id": "1-newpage...",
  "title": "Applied: Staff Engineer @ Acme Corp",
  "web_url": "https://onedrive.live.com/...",
  "client_url": "onenote:https://...",
  "linked_job_id": "a1b2c3d4e5f60718",
  "message": "Page created in Randy's Notebook / Job Searching."
}
```

**Logic:**
1. If `JOB_TRACKER_ONENOTE_WRITE != "1"` → error naming the variable and the config file.
2. If `confirm` is not `True` → return
   `{"result": "preview", "would_create": {...}, "html": "...", "message": "Set confirm=true to create this page."}`
   **A dry run by default.** Page creation is not undoable through this server, and a model
   that misreads an instruction should produce a preview, not a page. The docstring says:
   *call once with `confirm=false` to show the user, once with `confirm=true` after they say
   yes.*
3. If `job_id` is given: look it up (404 → error). **Check `onenote_links` for an existing
   `match_kind='created_by_tool'` row for that `job_id`.** If one exists, return
   `{"result": "exists", "page_id": ..., "message": "A page for this job was already created on ..."}`
   — **not** an error, and **not** a second page. This is the idempotency guarantee, and it
   mirrors `add_job`'s.
4. Build HTML via `build_page_html(title, body_lines, created=now_str(), marker=job_id)`.
5. `POST /me/onenote/sections/{section_id}/pages`, `Content-Type: text/html`.
   Expect `201`. **Explicitly to the resolved `section_id`** — never `?sectionName=`.
6. Insert the returned page into `onenote_pages` (so the next `onenote_sync` sees it as
   unchanged, not new), and insert an `onenote_links` row with
   `match_kind='created_by_tool', confidence=1.0`.

Steps 5 and 6 are not atomic — Graph committed before SQLite did. If the insert fails, the
page still exists. Log loudly; the next `onenote_sync` picks it up and the marker re-links it.
That is why the marker exists: **the local link table is an optimization; the marker in the
page body is the source of truth.**

There is deliberately **no `onenote_update_page` and no `onenote_delete_page`.** Graph's
`PATCH /pages/{id}/content` takes a JSON patch array addressing elements by `data-id`, which
requires round-tripping ids this extractor throws away; getting it wrong overwrites a
person's notes. Out of scope. If Part 3 wants it, it needs its own spec.

---

### Tool 23: `onenote_link_page`

**Purpose:** Manually assert that a page documents a job, when automatic matching could not.
Also the tool to *un*-link a wrong match.

**Parameters:**
```
page_id      str   required
job_id       str   required
unlink       bool  optional  default False
evidence     str   optional  (the snippet that justifies the link)
date_applied str   optional  ISO "YYYY-MM-DD" — the date THE PAGE says you applied
extracted    dict  optional  whatever else was read off the page
```

**Returns:** `{"result": "linked" | "unlinked" | "exists", "page_id": ..., "job_id": ...}`

Writes `match_kind='manual'`, `confidence=1.0`. A manual link **always wins** and is never
overwritten by a later reconciliation — reconcile only ever inserts `marker`/`exact`/`fuzzy`
rows with `INSERT ... ON CONFLICT DO NOTHING`.

`date_applied` is what closes the Tier-B loop, and it is not optional in spirit. Tier B is the
*primary* extraction path: the model reads a page, sees *"Applied to Staff Engineer at Acme,
7/2"*, and links it. If that `7/2` is not carried onto the link row, `reconcile_applications`
has nothing to hand `mark_applied`, defaults to today, and records — permanently — that you
applied on the day you happened to run reconciliation. The docstring must tell the model to
pass the date it just read.

Rejects a `job_id` whose row has `merged_into` set, naming the survivor. Linking evidence to a
hidden row is how evidence gets lost.

`unlink=True` on a `created_by_tool` link is refused: that row is the record that this server
created a page, and deleting it would let a second page be created for the same job. Error:
`"Cannot unlink a page this server created. Delete the page in OneNote first."`

---

### Tool 24: `reconcile_applications`

**Purpose:** *The point of Part 2.* Cross-reference the `jobs` table against the OneNote
mirror and report where they disagree.

**Parameters:**
```
dry_run          bool  optional  default True
min_confidence   float optional  default 0.6, clamped [0.3, 1.0]   -- Jaccard scale, == Part 1's DUP_THRESHOLD
apply_marks      bool  optional  default False   (mark_applied for OneNote-only applications)
limit            int   optional  default 100
```

**Returns:**
```json
{
  "matched": [
    {"page_id": "1-w...", "job_id": "b3c4...", "match_kind": "marker", "confidence": 1.0,
     "agrees": true}
  ],
  "matched_count": 28,
  "onenote_only": [
    {"page_id": "1-x...", "page_title": "Applied: Staff Eng @ Acme",
     "extracted": {"company": "Acme Corp", "title": "Staff Engineer", "date_applied": "2026-07-02"},
     "job_id": "a1b2c3d4e5f60718", "match_kind": "exact", "confidence": 1.0,
     "db_status": "new",
     "proposed_action": "mark_applied(id='a1b2c3d4e5f60718', date_applied='2026-07-02')"}
  ],
  "db_only": [
    {"job_id": "f7e6...", "company": "Beta Inc", "title": "Principal Engineer",
     "date_applied": "2026-06-28", "status": "applied",
     "proposed_action": "onenote_create_page(job_id='f7e6...')"}
  ],
  "unmatched": [
    {"page_id": "1-v...", "page_title": "July log",
     "extracted": {"company": "Initech", "title": "Staff Engineer", "date_applied": "2026-07-04"},
     "proposed_action": "add_job(company='Initech', title='Staff Engineer', location='Unknown') then mark_applied"}
  ],
  "ambiguous": [
    {"page_id": "1-y...", "extracted": {"company": "Acme", "title": "Sr Staff Eng"},
     "candidates": [
       {"job_id": "a1b2...", "title": "Staff Engineer",        "confidence": 0.79},
       {"job_id": "9f8e...", "title": "Senior Staff Engineer", "confidence": 0.82}
     ],
     "proposed_action": "onenote_link_page(page_id=..., job_id=<pick one>)"}
  ],
  "unparsed_pages": [
    {"page_id": "1-z...", "title": "Resume drafts", "reason": "no application record found",
     "proposed_action": "onenote_ignore_page or ignore"}
  ],
  "dry_run": true,
  "message": "1 job appears applied in OneNote but is 'new' in the database. Re-run with dry_run=false, apply_marks=true to update."
}
```

### The `unmatched` bucket

Found while building: a page row that parses cleanly and names a company the tracker has
never seen matched nothing, and was reported **nowhere** -- neither `onenote_only` (no job),
nor `ambiguous` (no candidates), nor `unparsed_pages` (it parsed fine). A job you applied to,
silently omitted from the report. Exactly the failure this whole design exists to prevent.

Such records now land in `unmatched`, with `add_job(...)` as the proposed action. Pinned by
`test_different_company_is_unmatched_not_silent`.

`matched` is a **list, not a bare count.** The first draft returned `"matched": 28`, which is
unauditable: a reconciler that reports 28 matches and 0 discrepancies is indistinguishable
from one that matched 28 things wrongly. Each entry names the page, the job, how they were
joined, and whether the two sources agree on status. `matched_count` is there for the summary
line. If the list would be enormous, truncate it and say so in `message` — never silently.

### The matching ladder

Applied in order; the first hit wins and stops the ladder for that page.

| Tier | `match_kind` | Rule | Confidence |
|---|---|---|---|
| 1 | `manual` | Pre-existing `manual` row in `onenote_links` | `1.0` |
| 2 | `marker` | `job-tracker-id: <16 hex>` found in `content_text` | `1.0` |
| 3 | `exact` | `canonicalize_company(c)` **and** `canonicalize_title(t)` both equal a live job's | `1.0` |
| 4 | `fuzzy` | company canonical-equal **and** `_title_similarity(t1, t2) >= min_confidence` | the score |
| 5 | — | company canonical-equal, several titles ≥ `AMBIGUOUS_FLOOR` → **`ambiguous`** | — |

**Manual outranks marker**, reversing the first draft. A marker is machine-written and exact,
but it can go **stale**: duplicate a page as a template, edit it to describe a different role,
and the old `job-tracker-id` rides along in the body text. A human who has just looked at the
page and asserted a link has strictly better information than a string copied from a page they
forgot they copied. Markers still beat everything below them.

**Only live jobs participate.** Every query behind tiers 3–5 filters `merged_into IS NULL`.
Matching a page to a merged, hidden job would produce a `proposed_action` that Part 1 then
refuses.

### The fuzzy metric: reuse Part 1's, do not reinvent

The first draft specified `difflib.SequenceMatcher(None, t1, t2).ratio()`. **That metric is
wrong, and wrong in a way that inverts the answer.** Measured on the real strings:

| Pair | `SequenceMatcher` | `_title_similarity` (Jaccard) | Truth |
|---|---|---|---|
| `staff engineer` / `staff software engineer` | 0.76 | **0.67** | same requisition |
| `staff engineer` / `senior staff engineer` | **0.80** | 0.67 | different level |
| `backend engineer` / `frontend engineer` | 0.73 | **0.33** | different roles |

`SequenceMatcher` ranks a *different seniority level* (0.80) **above** the true duplicate
(0.76). No threshold can separate them; the ladder would confidently match the wrong job.
Characters measure spelling. Word-set overlap measures meaning. Part 1 already learned this,
deleted the character metric, and pinned the lesson with a regression test. Import
`Database._title_similarity` — do not write a second copy, and do not reach for `difflib`.

**Threshold recalibration follows from the metric.** The first draft's `min_confidence=0.85`
matches **nothing** — the true duplicate above scores 0.67 — and a reconciler that matches
nothing reports "0 discrepancies" and looks like it is working. That is finding #1's failure
mode wearing a different hat.

```python
DEFAULT_MIN_CONFIDENCE = 0.6      # == Part 1's DUP_THRESHOLD; same metric, same scale
AMBIGUOUS_FLOOR        = 0.5      # below min_confidence: "worth a human glance"
```

`min_confidence` is clamped to `[0.3, 1.0]`. Any change to `_title_similarity` changes both
Part 1's duplicate hints and Part 2's reconciliation; that coupling is intentional and the
docstring must say so.

**Location is deliberately excluded from matching.** This is the central insight and the
reason `generate_job_id()` cannot be used here. Part 1's id is
`sha256(company_canonical | title_canonical | location_canonical)`, but a hand-written OneNote
note says *"Applied to Staff Engineer at Acme, 7/2"* — it almost never records the location.
Hashing `(company, title, "unknown")` produces an id that matches **nothing**, and a
reconciler that silently matches nothing reports "0 discrepancies" and looks like it works.

So: match on `(company_canonical, title_canonical)`, and accept that one company posting two
same-titled roles in two cities produces an `ambiguous` entry a human resolves once via
`onenote_link_page`. Fail toward "ask", never toward "guess".

**Use the persisted canonical columns.** The first draft matched in Python over a full
`SELECT` of `jobs`, reasoning that adding `company_norm`/`title_norm` columns "would have
coupled Part 2 to the very key-versioning hazard that Part 1's spec spends a page warning
about." Part 1 shipped those columns anyway — `company_canonical` and `title_canonical`, both
indexed — and documents the versioning hazard itself. The coupling already exists and is
already paid for. So tier 3 is an indexed lookup, and tiers 4–5 scan only the rows sharing a
canonical company:

```sql
SELECT id, title, title_canonical, status, date_applied
  FROM jobs
 WHERE company_canonical = ? AND merged_into IS NULL
```

This is both faster and *more correct* than the first draft: `canonicalize_company` collapses
`Acme Corp` / `Acme Corporation` / `Acme, Inc.`, which `normalize_key_part` does not. A page
saying "Acme Corp" against a job stored as "Acme, Inc." would have fallen through tier 3 to
fuzzy for no reason at all.

### Where `extracted` comes from — two-tier extraction

**Do not write a regex parser for free-form prose.** It will work on the five pages you test
it against and fail on the sixth, and its failures are silent.

- **Tier A — structural (in code).** If a page's text contains lines shaped like a table —
  `Acme Corp | Staff Engineer | 2026-07-02 | Applied` — parse them. Header-row detection:
  a first row whose cells casefold into `{company, title, role, position, date, applied,
  status, ...}`. This handles the common case, which is a running table, for free.
- **Tier B — the model (via the tools).** For everything else, `reconcile_applications`
  returns those pages in `unparsed_pages`. Claude reads them with `onenote_get_page`,
  extracts the records itself — this is exactly the task an LLM is good at and a regex is
  not — and calls `onenote_link_page` per record. The link persists, so the extraction cost
  is paid once per page, not once per reconciliation.

`reconcile.py` is **pure functions over data structures**: no SQL, no HTTP. `match_pages(
pages: list[PageRecord], jobs: list[JobRecord], min_confidence: float) -> Reconciliation`.
That is what makes the interesting logic testable without a Graph mock or a database.

### Mutation is opt-in twice

`dry_run=True` **and** `apply_marks=False` by default. To actually change the `jobs` table you
must pass both `dry_run=False` and `apply_marks=True`. When they are set:

- Only `onenote_only` entries with `confidence >= min_confidence` **and** a resolved `job_id`
  are acted on.
- The action is Part 1's `mark_applied(id, date_applied, notes=f"Reconciled from OneNote page '{title}' on {today}")`.
  It reuses the existing tool — including its `ALLOWED_TRANSITIONS` check. A `rejected` job
  will refuse the transition and land in a `skipped` array with the reason. **Part 2 never
  writes to `jobs` directly and never bypasses the lifecycle table.**
- `ambiguous` entries are **never** acted on regardless of flags.
- The response gains `"applied": [...]` and `"skipped": [...]`.

`db_only` is never auto-acted-on — that would mean creating OneNote pages in bulk, which is
irreversible. It reports `proposed_action` and stops.

---

### Tool 25: `onenote_ignore_page`

**Purpose:** Mark a page as not-an-application, so reconciliation stops reporting it.

**Parameters:** `page_id: str`, `ignore: bool = True`

Sets `onenote_pages.is_ignored`. Without this, `unparsed_pages` accumulates the same "Resume
drafts" and "Interview prep" pages on every single run, training the user to ignore the
report — which is how a reconciliation tool dies.

---

## Rate Limits, Cost, and Why the Cache Exists

Graph's OneNote endpoints are among the most aggressively throttled in the API, and
`GET /pages/{id}/content` is the expensive one. A naive reconciler that refetches 47 page
bodies on every run will earn a `429` with a multi-second `Retry-After` within a few
iterations, and the tool will feel broken.

The design consequences, already baked in above:

- Enumerate with `$select` (cheap, one call per 100 pages) → compare `last_modified_dt` →
  fetch content only for changed pages. A steady-state sync is **one** HTTP request.
- Reconciliation reads SQLite, never Graph.
- `onenote_sync` reports `throttled_retries`. If that number is ever non-zero in normal use,
  something is refetching that shouldn't.
- Honor `Retry-After` to the second. Do not add your own jitter on 429 — Graph told you when
  to come back.

---

## Security and Privacy

- **Token storage:** macOS Keychain via `keyring`. Fallback file is `0600` and outside the
  repo. The token is never written to the database, never logged, never returned by a tool.
  `onenote_auth_status` returns the *account name*, not the token.
- **Scope:** `Notes.ReadWrite` (delegated). Not `Notes.ReadWrite.All`, not `Files.Read`, not
  `User.Read.All`. This grants access to the signed-in user's own notebooks and nothing else.
- **No client secret.** Public client + device code. A secret sitting in
  `claude_desktop_config.json` would be a credential in plaintext on disk with no
  compensating control.
- **Log redaction:** `graph.py` redacts `Authorization` on every logged request. Write the
  helper; do not trust yourself to remember.
- **The database now contains notebook content.** `jobs.db` was previously job-alert metadata;
  after Part 2 it holds the text of Randy's OneNote pages, which may contain salary figures,
  recruiter names, and personal notes. `data/` and `*.db` are already gitignored by Part 1 —
  **verify that before the first sync**, not after. Add a line to the README saying so.
- **HTML escaping on write** is the injection boundary: `body_lines` originate in job-alert
  emails, which are attacker-controlled text. `html.escape(quote=True)` on every line, always.
- **`.gitignore` additions:** `.job-tracker-mcp/`, `*.pre-v2`.

---

## Failure Isolation

Non-negotiable, and worth a test each:

1. `import job_tracker_mcp.server` with `JOB_TRACKER_MS_CLIENT_ID` unset, no network, and no
   Keychain entry → **succeeds**, registers all 25 tools, writes nothing to stdout.
2. All 15 Part-1 tools work identically with OneNote entirely unconfigured.
3. Every OneNote tool, unconfigured, returns a `{"error": ...}` or a `{"configured": false}`
   diagnosis — never an exception, never a hang, never a network timeout that blocks the
   stdio loop for 30 seconds.
4. The `Database` constructor never imports `msal` or `httpx`. Lazy-import the `onenote`
   package inside the tool bodies, or behind an `@functools.cache`d factory.

Point 4 matters more than it looks: Part 1's `db = Database()` runs at module import, before
FastMCP is listening. An `msal` import that touched the network there would make the server
appear crashed to Claude Desktop with no diagnosable output.

---

## Testing

Zero network. Zero Keychain. Zero live notebook. Enforced, not merely intended:

```python
# tests/conftest.py -- ADD
@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any real socket in the test suite is a bug.

    Blocks at the socket layer, NOT at httpx. The first draft patched
    httpx.HTTPTransport.handle_request -- but msal talks over `requests`/urllib3,
    so the entire auth path could have reached the internet while this fixture
    reported success. Sockets are the one chokepoint both libraries share.
    """
    def _boom(*a, **k):
        raise AssertionError("test attempted a real network connection")
    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket.socket, "connect_ex", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)


@pytest.fixture(autouse=True)
def _no_keychain(monkeypatch):
    """Never touch the real Keychain, and never block on its dialog."""
    monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)
    monkeypatch.setattr(keyring, "set_password", lambda *a, **k: None)
```

`httpx.MockTransport` is injected explicitly and never opens a socket, so it sails through.
Anything that *does* open one fails loudly, with a stack trace pointing at the culprit.

Graph is faked with `httpx.MockTransport` and a handful of captured-shape JSON fixtures in
`tests/fixtures/graph/`. Auth is faked by injecting `token_provider=lambda: "fake-token"`.

### Test list (minimum)

**`test_onenote_htmlio.py`**
- `test_html_to_text_strips_positioning` — absolutely-positioned `div`s produce clean lines.
- `test_html_to_text_table_to_pipes` — a `<table>` becomes `a | b | c` rows.
- `test_html_to_text_placeholders_images` — `<img>` → `[image]`; the src URL is **never**
  fetched and never appears in the output.
- `test_html_to_text_entities` — `&amp;` → `&`.
- `test_build_page_html_escapes` — a title `R&D <Engineer>` round-trips escaped; assert the
  raw `<` does not appear.
- `test_build_page_html_marker_present` — the `job-tracker-id: <id>` line is in the body.
- `test_marker_roundtrip` — `build_page_html` output, run through `html_to_text`, yields a
  string that `MARKER_RE` matches with the original id.

**`test_onenote_store.py`**
- `test_migration_v1_to_v2` — build a v1 database (create `SCHEMA_SQL`, set `user_version=1`),
  insert a job, open a `Database` on it, assert `user_version==2`, the job survived
  **byte-identical**, and the three new tables exist.
- `test_fresh_db_is_v2` — new file gets both schemas and `user_version=2`.
- `test_v3_database_refuses` — `user_version=3` still raises, per Part 1.
- `test_link_cascade_on_page_delete` — deleting a page removes its `onenote_links` rows.
  (Proves `PRAGMA foreign_keys=ON` is actually live. Note this is the cascade that fires;
  the one into `jobs` never does, because Part 1 never deletes a job.)
- `test_tombstone_not_delete` — a vanished page keeps its row and its links.
- `test_content_cache_key` — content refetched iff `content_synced_dt != last_modified_dt`.
- `test_merge_jobs_repoints_links` — link page→B, `merge_jobs(A, B)`, assert the link now
  names A and no row references the hidden B.
- `test_merge_jobs_link_collision` — a page linked to **both** A and B; after the merge exactly
  one row survives (`PRIMARY KEY(page_id, job_id)` is not violated) and no evidence is lost.
- `test_link_page_refuses_merged_job` — `onenote_link_page(page, B)` where B is merged returns
  an error naming A.
- `test_migration_is_idempotent` — run `_migrate(1, 2)`, then simulate the crash gap by
  resetting `user_version` to 1, and run it again. No error; tables intact; version 2.

**`test_onenote_client.py`** (all via `MockTransport`)
- `test_resolve_section` — finds `Job Searching` under `Randy's Notebook`.
- `test_resolve_section_apostrophe` — the notebook name `Randy's Notebook` resolves without
  any OData `$filter` being sent. Assert the request URL contains no `$filter`.
- `test_resolve_ambiguous_errors` — two sections with the same name → error, not a guess.
- `test_paging_follows_nextlink` — 3 pages of 100; assert the nextLink is requested
  **verbatim**, with no re-appended `$top`.
- `test_429_honors_retry_after` — one `429` with `Retry-After: 2`, then `200`; assert sleep(2)
  and success, and `throttled_retries == 1`.
- `test_401_refreshes_once_then_gives_up` — assert exactly two attempts, then the
  "run onenote-auth.sh" message. Assert no infinite loop.
- `test_create_page_posts_to_section_id` — assert the URL is `/sections/{id}/pages` and that
  the string `sectionName` appears **nowhere** in the request.
- `test_create_page_requires_write_env` — with the env var unset, no HTTP request is made.
- `test_create_page_confirm_false_is_preview` — no HTTP request is made; `result=="preview"`.
- `test_create_page_idempotent` — a second call with the same `job_id` returns
  `result=="exists"` and issues no POST.
- `test_authorization_header_redacted_in_logs` — caplog contains no bearer token.

**`test_reconcile.py`** (pure, no DB, no HTTP — the important one)
- `test_marker_match_wins` — a page with a marker links to that job even if the text names a
  different company.
- `test_manual_beats_marker` — a page carrying a stale marker for job B, manually linked to
  job A, reconciles to A. (Pins the tier-order reversal.)
- `test_fuzzy_metric_is_word_overlap_not_characters` — the regression pin for the metric.
  Assert `staff engineer` / `staff software engineer` matches while
  `backend engineer` / `frontend engineer` does not. Under `SequenceMatcher` the second scores
  *higher* than the first, so a test that only checks the true positive would pass with the
  wrong metric. **Assert both directions.**
- `test_default_min_confidence_matches_the_true_duplicate` — with the shipped default, the
  `staff software engineer` page matches. Guards against a threshold that silently matches
  nothing and reports "0 discrepancies."
- `test_merged_jobs_are_not_match_candidates` — a merged job never appears in `onenote_only`,
  `ambiguous`, or `matched`.
- `test_manual_link_date_applied_is_used` — reconcile proposes `mark_applied` with the date
  from the link row, **not** today's date.
- `test_exact_match_uses_canonical_company` — a page saying `Acme Corp` matches a job stored as
  `Acme, Inc.` at tier 3 (`exact`), not tier 4. Pins the `canonicalize_company` switch.
- `test_exact_match_company_title` — normalization applies (`"Acme  Corp."` vs `"acme corp."`).
- `test_location_is_not_part_of_the_key` — a job stored with `location="Remote, US"` matches a
  page that never mentions a location. **The regression test for the central design decision.**
- `test_fuzzy_below_threshold_is_ambiguous_not_matched`.
- `test_two_candidates_above_threshold_is_ambiguous` — never picks one.
- `test_manual_link_survives_reconcile` — a `manual` row is not overwritten by a `fuzzy` match.
- `test_apply_marks_respects_transitions` — a `rejected` job proposed for `mark_applied` lands
  in `skipped` with the transition error, and the DB is unchanged.
- `test_dry_run_default_mutates_nothing` — assert `updated_at` on every job is untouched.
- `test_ignored_pages_excluded`.
- `test_table_extraction` — a pipe-table page yields structured `extracted` records.

**`test_server_isolation.py`**
- `test_import_without_client_id` — import succeeds, stdout empty, 25 tools registered.
- `test_part1_tools_unaffected` — `add_job`/`get_stats` behave identically with OneNote off.

---

## Implementation Order

Each milestone is independently verifiable. Do not proceed past a red one.

**Phase 0 does not block the start.** The first draft put the Entra app registration at
milestone 0, gating everything. But milestones 1, 2, 3, and 5 need no client id, no token, and
no network — `MockTransport` and a temp SQLite file cover them entirely. That is most of the
code, fully tested, before Randy opens a browser. Phase 0 is a prerequisite of milestone 4,
not of the project.

| # | Milestone | Needs Phase 0? | Done when |
|---|---|---|---|
| 1 | `pip install msal`; pin `msal`, `httpx`, `keyring` in `requirements.txt` | no | `pip show msal` |
| 2 | Schema v2, `store.py`, `merge_jobs` link re-pointing, migration tests | no | `pytest tests/test_onenote_store.py` green; `cp data/jobs.db data/jobs.db.pre-v2` taken |
| 3 | `htmlio.py` + tests (pure) | no | `pytest tests/test_onenote_htmlio.py` green |
| 5 | `graph.py` + `client.py` + `MockTransport` tests | no | `pytest tests/test_onenote_client.py` green |
| 7a | `reconcile.py` pure functions + `test_reconcile.py` | no | green, incl. the location and metric regression pins |
| — | *Randy completes Phase 0* | — | `echo $JOB_TRACKER_MS_CLIENT_ID` prints a GUID |
| 4 | `auth.py` + `onenote-auth.sh` (incl. keychain timeout) | **yes** | `./onenote-auth.sh` completes; `onenote_auth_status` reports `authenticated: true` |
| 6 | Tools 16–21 (read path) | **yes** | `onenote_sync` mirrors the real section; `onenote_list_pages` shows the real page titles |
| 7b | Tools 23, 24, 25 wired to the DB | yes | `reconcile_applications(dry_run=true)` produces a report a human agrees with |
| 8 | Tool 22 `onenote_create_page` (write path) | yes | preview → confirm → one page appears in OneNote; a second call returns `exists` |
| 9 | Full suite + stdout-purity smoke tests + Claude Desktop config `env` block | yes | all green; ⌘Q and relaunch |

**Milestone 6 before 8.** Read the section, look at the report, and confirm the extraction is
right *before* granting anything the ability to write into a personal notebook.

**Milestone 7a before 6.** `reconcile.py` is pure functions over data structures — no SQL, no
HTTP. Its tests are the ones that catch the two design errors this document has already made
once (hashing location into the key; using a character-similarity metric). Write them before
anything can reach a live notebook and make a wrong report look authoritative.

---

## `requirements.txt` after Part 2

```
fastmcp>=3.0,<4
msal>=1.31,<2
httpx>=0.27,<1
keyring>=25,<26
```

Measured on this machine (2026-07-09): `httpx 0.28.1` ✓ and `keyring 25.7.0` ✓ are already
present, arriving transitively through `fastmcp` → **`fastmcp-slim`** (fastmcp's only direct
requirement — so these are two levels down, which makes declaring them more important, not
less). `msal` is **not** installed; it is the one genuinely new dependency. **Declare all
three anyway.** Part 2 imports them directly, and a future `fastmcp` that drops one would
break this server with an `ImportError` at a moment nobody is looking at it.

`msal` is the only genuinely new dependency. It is Microsoft's own, is pure Python, and its
only transitive deps are `requests`, `PyJWT`, and `cryptography`.

> **`requests` is a new transitive dependency and it must never see stdout.** It won't — but
> the import-time stdout-purity assertion from Part 1's step 9 is what proves it. Extend that
> smoke test to import the `onenote` package, not just `server`.

---

## Claude Desktop Config After Part 2

```json
{
  "mcpServers": {
    "job-tracker": {
      "command": "/Users/randyn/projects/git/job-tracker-mcp/start.sh",
      "args": [],
      "env": {
        "JOB_TRACKER_MS_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
        "JOB_TRACKER_ONENOTE_NOTEBOOK": "Randy's Notebook",
        "JOB_TRACKER_ONENOTE_SECTION": "Job Searching",
        "JOB_TRACKER_ONENOTE_WRITE": "0"
      }
    }
  }
}
```

Claude Desktop launches `start.sh` with a minimal environment and **does not read your shell
profile**. The `env` block is the only way these reach the server. Merge with
`json.load`/`json.dump`, re-parse before writing, ⌘Q and relaunch.

Flip `JOB_TRACKER_ONENOTE_WRITE` to `"1"` only after milestone 8 has been exercised by hand.

---
---

# Gap Review — Second Pass

The plan above was reviewed against the question *"what will actually go wrong, and what did
the first draft quietly assume?"* Fourteen findings. Items 1–9 have been folded into the body
above; 10–14 are recorded here as accepted risks or deferred work.

### Folded in

1. **The `generate_job_id` trap.** The obvious design — hash the OneNote-extracted
   `(company, title, location)` and look up `jobs.id` — is wrong, and wrong in the worst way:
   it matches nothing, silently, and reports zero discrepancies. Handwritten notes don't
   record locations, so `location_canonical` becomes `"unknown"` and the hash diverges. Fixed
   by matching on `(company, title)` in Python, with `test_location_is_not_part_of_the_key` as
   the regression pin.

2. **Interactive auth cannot live in a stdio server.** The first draft had a `onenote_login`
   tool. It would have deadlocked: no TTY, and stdout belongs to JSON-RPC. Fixed by moving
   auth to a separate `onenote-auth.sh` binary and making every tool's failure message name
   that command.

3. **App-only auth was removed from the OneNote API on 2025-03-31.** Any design assuming a
   client-credentials daemon flow is dead on arrival. This was checked against current docs
   rather than recalled. It is the reason a human must sign in.

4. **`?sectionName=` silently creates sections.** A single typo would fork the notebook. Now
   a hard rule: always POST to a resolved `section_id`, with a test asserting the string
   `sectionName` never appears in a request.

5. **Page creation is not idempotent by nature.** Graph has no natural key and will happily
   create ten identical pages. Fixed with a three-layer guard: the local `created_by_tool`
   link, the visible `job-tracker-id:` marker in the page body (which survives a lost local
   DB), and `confirm=False` defaulting to a preview.

6. **The marker cannot be a `<meta>` tag or a `data-` attribute.** OneNote's sanitizer makes
   no round-trip guarantee for those, and a marker that vanishes silently converts
   idempotency into duplicate pages. It must be visible body text. Accepting a slightly ugly
   footer is the correct trade.

7. **`_migrate` was a `NotImplementedError` stub.** Part 1 shipped `SCHEMA_VERSION = 1` with
   no migration path. Bumping to 2 without implementing 1→2 would make Part 2 refuse to open
   Randy's existing, populated `data/jobs.db`. The migration is purely additive — new tables
   only — which is precisely why Part 2 was designed to add **zero** columns to `jobs`.

8. **Content refetching will get you throttled.** `GET /pages/{id}/content` is the expensive
   call. The `content_synced_dt != last_modified_dt` cache key reduces a steady-state sync to
   one HTTP request. Without it, reconciliation is 47 requests and a `429`.

9. **A "Job Searching" section is not only applications.** Resume drafts, interview prep, and
   recruiter lists live there too. Without `is_ignored`, every reconciliation report carries
   the same permanent noise, and the user learns to skim past the whole thing. Hence tool 23.

### Accepted risks and deferred work

10. **Ambiguous matches require a human, once per page.** One company posting `Staff Engineer`
    in Austin and in Denver produces an `ambiguous` entry. This is by design — Part 1 already
    argues that a visible duplicate beats a silent false merge — but it means reconciliation
    is not fully autonomous on first run. Mitigation: `onenote_link_page` persists the
    decision, so the cost is paid once. **Accepted.**

11. **The local cache holds personal notebook content.** After the first sync, `jobs.db`
    contains the text of Randy's OneNote pages: possibly salary figures, recruiter names,
    interview feedback about named people. It is gitignored and unencrypted. This is a
    material change in the sensitivity of that file and the README must say so plainly.
    A future option: store only a normalized `(company, title, date)` extract and discard
    `content_text` after parsing. **Deferred** — full text is needed for Tier-B extraction and
    for `onenote_search_pages`, and dropping it would make the reconciler unauditable.

12. **No page editing.** `PATCH /pages/{id}/content` addresses elements by the `data-id`
    attributes that `html_to_text` discards, and a wrong patch overwrites a person's notes
    irreversibly. Appending "Applied 2026-07-09" to an existing running-log page is the
    obvious Part 3 feature and the obviously dangerous one. **Explicitly out of scope.**
    Preserving `data-id` in a sidecar column would be the enabling prerequisite.

13. **Two-way sync is not attempted, and should not be.** OneNote is treated as an
    append-only source of evidence and a write target for new pages. The `jobs` table is the
    system of record for status. If they conflict — OneNote says applied, the DB says
    rejected — reconciliation **reports and stops**; `ALLOWED_TRANSITIONS` refuses the change
    and it lands in `skipped`. Making these authoritative over each other requires a conflict
    model (last-write-wins? vector clocks?) that no job tracker needs. **Deliberately
    unbuilt.**

14. **Refresh tokens expire.** Roughly 90 days of inactivity, or immediately on a password
    change or a revoked consent. There is no notification; the next `onenote_sync` simply
    fails. The mitigation is entirely in the error string — it must name `onenote-auth.sh` by
    absolute path — plus `onenote_auth_status` existing as the obvious first thing to call.
    A cron-based silent-refresh keepalive was considered and rejected: a background process
    holding a Graph token, running unattended, to save one terminal command every three
    months, is a bad trade. **Accepted.**

### One improvement worth naming

The `unparsed_pages` → `onenote_get_page` → `onenote_link_page` loop (Tier-B extraction) is
not a fallback. It is the **primary** mechanism, and the structural table-parser in Tier A is
the optimization. This inversion is what makes the design robust: a regex parser confronted
with an unfamiliar note fails silently and reports a wrong number, whereas a model confronted
with the same note reads it correctly and writes a durable link. Building the regex first and
the model path second would produce a tool that appears to work and quietly under-reports.
Build Tier B first; add Tier A only once the real pages show a table worth optimizing for.

---
---

# Gap Review — Third Pass (against shipped Part 1)

The second pass reviewed this plan against itself. This one reviewed it against the Part 1 that
actually shipped, and against the machine it will run on. Everything below is folded into the
body above.

**Two defects that would have produced a confidently wrong reconciler:**

1. **Tier 4 specified `difflib.SequenceMatcher`.** Measured, that metric *inverts the answer*:
   `staff engineer` / `senior staff engineer` (different level) scores **0.80**, above
   `staff engineer` / `staff software engineer` (same requisition) at **0.76**. Part 1
   encountered this exact bug, deleted the metric, and pinned the lesson. Part 2 now imports
   Part 1's `_title_similarity`. Compounding it, `min_confidence=0.85` matched **nothing** on
   either scale — the reconciler would have reported "0 discrepancies" and looked healthy.
   That is finding #1 of the second pass, recurring in a different disguise: *a reconciler
   that matches nothing is indistinguishable from one that has nothing to find.* Default is
   now 0.6.

2. **`onenote_sync` held the DB lock across every HTTP call.** "Steps 3–5 in one transaction,"
   with content fetching as step 4. Part 1 serializes all database access on one `RLock` and
   one connection, so a 40-page sync would have frozen every other tool for the length of 40
   network round-trips. Restructured into read / network / write phases, with a test that
   asserts the lock is free while a request is in flight.

**Three things Part 1 shipped that this document predates:**

3. **`merged_into`.** Jobs are never deleted; duplicates are merged and hidden. Links to a
   merged job orphan silently, and `mark_applied` on one is refused. `merge_jobs` now
   re-points `onenote_links`, and every reconciliation query filters `merged_into IS NULL`.

4. **`canonicalize_company` / `canonicalize_title`.** Tier 3 used `normalize_key_part`, so a
   page saying `Acme Corp` would miss a job stored as `Acme, Inc.` The canonical forms are
   already persisted as indexed columns, which also retires this document's argument for
   matching in Python.

5. **Fifteen tools, not thirteen.** Part 2's tools are 16–25; the server ends at 25.

**Three environmental facts, measured rather than assumed:**

6. **`sqlite3.executescript()` commits any pending transaction before it runs.** The migration
   is therefore *not* atomic. It is safe only because it is idempotent. Written down, because
   the next migration may not be.

7. **A Keychain "allow access" dialog blocks; it does not raise.** The token-cache fallback
   was guarded by `except`, which such a dialog never triggers. In a stdio server with no TTY,
   that is an unrecoverable hang. Now a timed read on a worker thread.

8. **`msal` speaks `requests`, not `httpx`.** The `_no_network` fixture patched
   `httpx.HTTPTransport`, leaving the entire auth path free to reach the internet during tests
   while the fixture reported success. Now blocked at `socket.connect`.

**Two smaller corrections:** `onenote_link_page` gained `date_applied`, without which Tier-B —
the *primary* extraction path — would have recorded the reconciliation date as the application
date, permanently. And `reconcile_applications` returns `matched` as an auditable list rather
than a bare integer, because "28 matches, 0 discrepancies" is exactly what a broken reconciler
also prints.

**One reversal of judgment:** the ladder now ranks `manual` above `marker`. A marker is exact
but can go stale — duplicate a page as a template, edit it, and the old `job-tracker-id` rides
along in the body. A human who just read the page knows more than a string they forgot they
copied.

**One sequencing change:** Phase 0 (the Entra app registration) no longer gates the project.
Milestones 1, 2, 3, 5, and the whole of `reconcile.py` need no client id, no token, and no
network. Build and test those first; open a browser later.
