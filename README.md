# job-tracker-mcp

A local MCP server that wraps a SQLite database for tracking job opportunities
surfaced from email job alerts. It runs on your machine, stores everything in a
single file, and talks to nothing on the network.

It is one half of a two-server workflow in Claude Desktop:

- **`apple-mail-mcp`** reads the job alert emails.
- **`job-tracker-mcp`** (this server) reads and writes the tracking state.

Claude reads an alert, calls `bulk_add_jobs`, and the server deduplicates,
auto-filters companies you've blacklisted, and files the rest for triage. This
server never touches email and holds no credentials.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
chmod +x start.sh
.venv/bin/python -m pytest tests/ -q
```

Requires Python 3.11+ (for `StrEnum`; on 3.10 change `models.py` to use
`class Status(str, Enum)`). Depends on `fastmcp` 3.x, plus `msal`, `httpx`, and
`keyring` for the OneNote integration.

## Register with Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

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

The `command` must be an absolute path — Claude Desktop does not consult your
`PATH` or shell profile. **Fully quit Claude Desktop (⌘Q) and relaunch**;
closing the window is not enough.

Logs land in `~/Library/Logs/Claude/mcp-server-job-tracker.log`. That is where
tracebacks go when a tool misbehaves.

## Database

Stored at `data/jobs.db`, overridable with the `JOB_TRACKER_DB` environment
variable. The tests use a temp file and never touch it.

`data/` and `*.db` are gitignored. **The database is the only durable state and
has no backup story.** If it matters to you, copy it somewhere:

```bash
cp data/jobs.db ~/Backups/jobs-$(date +%F).db
```

## Status lifecycle

```
        ┌──────────────────────────────┐
        │                              ▼
     ┌─────┐         ┌─────────┐   ┌─────────┐
     │ new │────────▶│ applied │──▶│ expired │
     └─────┘         └─────────┘   └─────────┘
        │                 │             │  ▲
        │                 ▼             │  │
        │           ┌──────────┐        │  │
        └──────────▶│ rejected │◀───────┘  │
                    └──────────┘           │
                          └────────────────┘
                         (reopen to new)
```

`applied → new` is forbidden: once you have applied, the record has a
`date_applied` and calling it "new" is a lie. Use `expired` for a role that was
filled or withdrawn. A `rejected` or `expired` job **can** be reopened to `new`,
because reconsidering a company is a real workflow.

Reopening a job does not delete the rejection pattern that auto-rejected it. Use
`remove_rejected_pattern` for that; the two are deliberately independent.

## Deduplication

A job's id is a hash of its canonicalized `(company, title, location)`. All three
are canonicalized deterministically **in code**, never by asking the model to
normalize — a stable key requires reproducing the same output across sessions,
which an LLM cannot do.

Pass the posting's strings verbatim. The server handles the rest:

| Field | Input | Canonical |
|---|---|---|
| company | `Acme Corp`, `Acme Corporation`, `Acme, Inc.` | `acme` |
| title | `Staff Engineer`, `Staff Engineer (Remote)`, `Staff Engineer [R-12345]`, `Staff Engineer - $180k-$220k` | `staff engineer` |
| location | `Remote`, `Remote, US`, `United States (Remote)`, `100% Remote`, `WFH` | `remote` |

Deliberately **not** collapsed, because the difference is real:

| Kept distinct | Why |
|---|---|
| `Senior Staff Engineer`, `Staff Engineer II`, `Staff Engineer, Platform` | seniority and specialization are meaningful |
| `Remote - EMEA` vs `Remote, US` | you can't take the EMEA one |
| `Hybrid - Remote 2 days` vs `Remote` | not fully remote |
| `Austin, TX` vs `Austin, Texas` | expanding abbreviations needs a gazetteer; `IN`/`OR`/`ME` are states *and* words |

Re-adding a job records a re-sighting (`times_seen`, `last_seen`) and never
overwrites your `fit_score`, `status`, or `notes`.

### Near-duplicates the key can't catch

Hashing can't know that `Staff Engineer` and `Staff Software Engineer` are one
requisition. So `add_job` still creates the row, and additionally returns
`possible_duplicates` — similar titles at the same employer, scored by word-set
overlap. Nothing is merged automatically.

Claude (or you) then calls `merge_jobs(keep_id, duplicate_id)`. The judgment is
stored in the database, so it's made **once**, not re-derived every session.

Merging sets `merged_into` on the duplicate rather than deleting it. The row is
hidden from `get_jobs`, `search_jobs` and `get_stats`; any later `add_job` that
resolves to it is redirected to the survivor; and `unmerge_job` puts it back.
If only the duplicate had been applied to, the survivor adopts that application.

The governing principle throughout: **fail toward a visible duplicate, never a
silent false merge.** A duplicate is annoying and fixable. A false merge quietly
destroys the record of a job you applied to.

**The canonicalization functions are part of the persisted primary key.**
Changing them orphans every stored row. See the warning in `models.py`.

## Tools

| Tool | Purpose |
|---|---|
| `add_job` | Add one job; idempotent |
| `bulk_add_jobs` | Add up to 100 at once — use this for triaging an alert email |
| `check_job` | Look up a job without modifying it |
| `mark_applied` | Mark applied; **appends** notes |
| `reject_job` | Reject, optionally learning a company/keyword/source pattern |
| `get_jobs` | List with filters, best fit first |
| `get_job` | Fetch one by id |
| `update_job` | Revise fields; **replaces** notes; enforces transitions |
| `merge_jobs` | Record that two rows are one requisition; reversible |
| `unmerge_job` | Undo a merge |
| `get_rejected_patterns` | Show what is being auto-filtered |
| `add_rejected_pattern` | Add a filter directly |
| `remove_rejected_pattern` | Stop filtering |
| `get_stats` | Counts, averages, and the unscored-new backlog |
| `search_jobs` | Substring search over company, title, notes, subject |

Rejection patterns apply at insert time only — adding one never retroactively
rejects jobs already in the database.

## OneNote integration

Ten more tools mirror the `Job Searching` section of a OneNote notebook into the
same database and reconcile it against tracked jobs, answering one question:
**which jobs have I already applied to?**

| Tool | Purpose |
|---|---|
| `onenote_auth_status` | Diagnose the connection. Call this first when anything fails. |
| `onenote_list_sections` | Enumerate notebooks and sections |
| `onenote_sync` | Refresh the local mirror. The only tool needing network. |
| `onenote_list_pages` / `onenote_get_page` / `onenote_search_pages` | Read the mirror |
| `onenote_create_page` | Create a page. Dry-run by default, gated, idempotent. |
| `onenote_link_page` / `onenote_ignore_page` | Curate what reconciliation sees |
| `reconcile_applications` | Cross-reference jobs against pages |

Everything but `onenote_sync` reads the local cache, so reconciliation never
touches the network. Page content is refetched only when the page changed
upstream: a steady-state sync is one HTTP request. OneNote's Graph endpoints are
aggressively throttled, and `GET /pages/{id}/content` is the expensive call.

### Setup

Sign-in **cannot** happen inside the MCP server: it's a stdio process with no
terminal, and its stdout carries the protocol. So auth is out-of-band.

1. Register an app at <https://entra.microsoft.com>: public client, "any org
   directory and personal Microsoft accounts", delegated **`Notes.ReadWrite`**
   (not `.All`), **allow public client flows = Yes**. No client secret.
2. Put the client id in the Claude Desktop config `env` block — Claude Desktop
   does not read your shell profile.
3. Run `./onenote-auth.sh` in a real terminal, once.

```json
"job-tracker": {
  "command": "/Users/randyn/projects/git/job-tracker-mcp/start.sh",
  "args": [],
  "env": {
    "JOB_TRACKER_MS_CLIENT_ID": "<guid>",
    "JOB_TRACKER_ONENOTE_NOTEBOOK": "Randy's Notebook",
    "JOB_TRACKER_ONENOTE_SECTION": "Job Searching",
    "JOB_TRACKER_ONENOTE_WRITE": "0"
  }
}
```

Writing is **off by default**. Creating a page is the one irreversible thing this
server can do — Graph has no undo, and there is no delete tool by design. Flip
`JOB_TRACKER_ONENOTE_WRITE` to `"1"` only when you want it.

**Rebuilding `.venv` requires re-running `onenote-auth.sh`.** The Keychain item is
bound to the binary that created it; a new interpreter triggers an "allow access"
dialog that a background process cannot answer. Token reads are time-boxed so
that shows up as an error rather than a hang.

### Reconciliation

`reconcile_applications` partitions everything and, by default, changes nothing:

- `matched` — both sources agree
- `onenote_only` — the page says applied, the database doesn't
- `db_only` — the database says applied, no page documents it
- `ambiguous` — several jobs plausibly match; a human picks, once
- `unmatched` — a page names a company the tracker has never seen
- `unparsed_pages` — read them, then `onenote_link_page`

Mutation is opt-in **twice**: `dry_run=false` *and* `apply_marks=true`. Ambiguous
entries are never acted on, `db_only` never triggers bulk page creation, and
`mark_applied` still enforces the status lifecycle — a rejected job lands in
`skipped` rather than being quietly revived.

Matching deliberately ignores location: a handwritten note says "Applied to Staff
Engineer at Acme, 7/2" and never records where the job was. Hashing the Part 1 key
would match nothing, and a reconciler that matches nothing reports zero
discrepancies and looks like it works.

Pages this server creates carry a visible `job-tracker-id:` footer. That marker,
not the local link table, is the source of truth for which page documents which
job — it survives a lost database, and it's why page creation is idempotent.

> **After the first sync, `jobs.db` contains the text of your OneNote pages** —
> possibly salary figures, recruiter names, and interview notes about named
> people. It is gitignored and unencrypted. That is a real change in how sensitive
> that file is.

## Development

`database.py` holds all the logic and all the tests point at it directly, with
no MCP client in the loop. `server.py` is a thin adapter whose docstrings are
the tool descriptions the model reads. `models.py` is enums and the natural key.

Nothing in `job_tracker_mcp/` may write to stdout: Claude Desktop speaks MCP
over stdio, and a stray `print()` corrupts the protocol.
