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
| `Remote - EMEA` vs `Remote, US` | you can't take the EMEA one |

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

## The triage workflow

Two MCP **prompts** are served by this server. They are the canonical workflow,
versioned alongside the tools they call, so they cannot drift out of sync. In
Claude Desktop they appear under the server's attachment menu.

| Prompt | When |
|---|---|
| `triage_job_alerts(since, max_emails, account, mailbox)` | every alert-triage run |
| `setup_job_filters()` | once, and whenever a filter is wrong |

The triage prompt names both servers explicitly — `apple-mail` and `job-tracker`,
the keys in `claude_desktop_config.json` — because the model has to know which
tool lives where. Two things about the mail side are baked in, having been
verified against the running server rather than assumed:

- **`search_messages` requires `account`, and this build is restricted** to
  mailbox `Job_Alerts_Viewed` on account `RandyN Exchange`. Anything else returns
  `access restricted`. Both are prompt arguments, so they can be changed without
  editing code.
- **`date_sent` is unset** in that mailbox — it comes back as
  `0001-01-01T00:00:00Z`. The real field is `date_received`, which already carries
  a local UTC offset, so its first ten characters *are* the local calendar date.
  `search_messages` has no date filter at all, so `since` is applied after the
  call.

The division of labour matters more than the prompt text:

- **Determinism lives in code and data.** Deduplication is the id hash. Filters
  are rows in `rejected_patterns`, consulted by `add_job` on every insert. The
  status lifecycle is a transition table. A prompt cannot *guarantee* any of
  these — it can only usually remember them.
- **Judgment lives in the model.** Fit scores, whether two similar titles are
  one requisition, reading an unstructured page.

### Filters: two kinds, deliberately asymmetric

**Negative** filters (`rejected_patterns`) — a match **rejects**.
**Positive** filters (`profile_rules`) — a non-match **demotes**.

The asymmetry is the whole design. A bad negative rule wrongly rejects one
company; the row is still stored, tagged with the pattern that caught it, and one
`update_job` from returning. A bad *positive* rule silently drops everything it
didn't anticipate — `Head of Engineering`, `Distinguished Engineer`, `VP,
Platform` — and the good jobs are exactly the ones with unusual titles. So a
positive non-match never rejects; it sets `off_profile`, which hides the job from
the default triage list and nothing more.

#### Positive rules (`profile_rules`)

| kind | effect |
|---|---|
| `title_include` | on-profile if the canonical title contains **any** include value |
| `title_exclude` | off-profile if it contains **any** exclude value (excludes beat includes) |

**With no include rules at all, every job is on-profile** — never the reverse.

`off_profile` is **derived, not a decision**, so `add_profile_rule` and
`remove_profile_rule` immediately reclassify existing jobs and report how many.
Status is never touched. `get_jobs()` hides off-profile **new** jobs; pass
`include_off_profile=true` to see them. Applied, rejected and expired jobs are
*never* hidden — history must not vanish because you narrowed your search later.

That retroactive sweep is safe precisely *because* it only moves a flag.
`rejected_patterns` has no equivalent, and shouldn't: rejecting is a decision,
and a retroactive one could bury a job you already applied to.

Include lists are dangerous when narrow. `director` demotes `Head of
Engineering`. Be generous, then audit with `get_jobs(include_off_profile=true)`.

Rules match the **canonical title** — the same string used for deduplication,
with `(Remote)`, `[R-1234]` and salary ranges stripped, seniority preserved. So
write them in that form. A rule the canonicalizer would rewrite, like
`director (remote)`, is **refused** rather than installed dead; the error names
the form that works. The same guard applies to `company` patterns
(`Infosys Inc` → use `infosys`) and `title_keyword` patterns.

Profile rules see the **title only** — never the company, location, salary, or
the email body. Companies belong in a `company` pattern.

#### Negative rules (`rejected_patterns`)

A filter is a row in `rejected_patterns`, of exactly one of three kinds:

| kind | match | example |
|---|---|---|
| `company` | substring, against the **canonical** company | `infosys` catches `Infosys BPM Ltd` |
| `title_keyword` | substring, against the **canonical** title | `staffing` catches `Staffing Coordinator` |
| `source` | **exact**, case-insensitive | `dice` catches `Dice`, not `Dice Inc` |

Company names have already had `Inc`/`LLC`/`Corp` stripped, and titles have lost
`(Remote)`, salary ranges and req ids — but **not** seniority. Don't include
those in a pattern.

A filtered job is **still stored**, with `status="rejected"` and a note naming
the pattern that fired. Deliberately:

- **Dedup** — the same posting arrives in next week's alert. A stored rejection
  means you decide once, not weekly.
- **Audit** — you can see what the filter ate, which is the only way to notice
  it's too broad.
- **Reversal** — `remove_rejected_pattern`, then `update_job(id, status="new")`.

A silently discarded job is the one thing that cannot be undone.

Substrings bite: `eng` matches `Engineering`, `ai` matches `Air` and `Maintain`.
Before installing a pattern, run `search_jobs(query="<pattern>")` and read what
it would have caught. A pattern that catches a job you applied to is a bad one.

Patterns apply at **insert time only** — adding one never retroactively rejects
what's already tracked.

### Where triage left off

`get_triage_state(account, mailbox)` returns the `date_received` of the newest
alert email already processed; `record_triage_run(...)` advances it. The
watermark lives in the `app_state` table, per mailbox — not in the prompt,
because a number the model has to remember is a number it will eventually get
wrong.

The first run reports `has_run: false`, meaning "process everything." Every run
after that reads `date_received >= last_message_dt`, **inclusively**.

Inclusive is the whole design. Re-reading the boundary email is free — `add_job`
is idempotent and a re-sighting keeps the earliest `date_seen`. A gap is not
free: it loses a job silently, and nothing ever goes back for it. **Overlap is
cheap; gaps are permanent.**

Two invariants the tools enforce, so the prompt can't get them wrong:

- The watermark **never moves backward**. Re-running an older slice of mail can't
  erase progress. Comparison is by *instant*, not by string — `2026-07-09T23:00Z`
  looks later than `2026-07-09T19:00-05:00` lexically, and is actually an hour
  earlier.
- It records the newest message **actually processed**, never "now". Mail that
  arrives mid-run has a later `date_received` and must be caught next time, not
  skipped because the clock moved past it. And a failed run must not record at
  all.

### When a job first appeared

`date_seen` is the date the posting first showed up, and it must come from the
**alert email's date**, not from when triage happened to run. Pass it to
`add_job`/`bulk_add_jobs`. On a re-sighting the server keeps the *earliest*
`date_seen` and the *latest* `last_seen`, so emails can be processed in any
order — including a backlog replayed out of sequence.

## Development

`database.py` holds all the logic and all the tests point at it directly, with
no MCP client in the loop. `server.py` is a thin adapter whose docstrings are
the tool descriptions the model reads. `models.py` is enums and the natural key.

Nothing in `job_tracker_mcp/` may write to stdout: Claude Desktop speaks MCP
over stdio, and a stray `print()` corrupts the protocol.
