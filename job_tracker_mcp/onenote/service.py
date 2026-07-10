"""Orchestration for the OneNote tools: env, resolution, sync phases, reconcile.

Imported lazily from server.py tool bodies. Importing it pulls in msal and httpx,
which must never happen during the MCP server's module import -- Part 1's
`db = Database()` runs there, before FastMCP is listening, and a network stack
failing at that point is invisible to Claude Desktop.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date

from job_tracker_mcp.database import Database, now_str, today_str
from job_tracker_mcp.onenote import auth
from job_tracker_mcp.onenote.client import OneNoteClient
from job_tracker_mcp.onenote.graph import AuthExpired, GraphClient, GraphError
from job_tracker_mcp.onenote.reconcile import (
    DEFAULT_MIN_CONFIDENCE,
    JobRecord,
    LinkRecord,
    PageRecord,
    match_pages,
    scope_to_subtree,
)
from job_tracker_mcp.onenote.store import OneNoteStore

log = logging.getLogger(__name__)

K_NOTEBOOK_ID = "notebook_id"
K_SECTION_ID = "section_id"
K_SECTION_NAME = "section_name"
K_LAST_SYNC = "last_sync"

MAX_LIMIT = 200


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def notebook_name() -> str:
    return _env("JOB_TRACKER_ONENOTE_NOTEBOOK", "Randy's Notebook")


def section_name() -> str:
    return _env("JOB_TRACKER_ONENOTE_SECTION", "Job Searching")


def write_enabled() -> bool:
    return _env("JOB_TRACKER_ONENOTE_WRITE", "0") == "1"


def root_page() -> str:
    """The parent page whose subpages are applications, e.g. "2026".

    Empty means "the whole section". The section also holds interview prep,
    resume drafts, and previous years, none of which are applications.
    """
    return _env("JOB_TRACKER_ONENOTE_ROOT_PAGE", "2026")


def _err(msg: str) -> dict:
    return {"error": msg}


class OneNoteService:
    def __init__(self, db: Database, graph: GraphClient | None = None):
        self.db = db
        self.store = OneNoteStore(db)
        self._graph = graph            # injected in tests; built lazily otherwise
        # The env var still wins, but onenote-auth.sh persists the client id so the
        # server works without one. Set unconditionally, so constructing a service
        # against a different database cannot inherit a stale id.
        auth.set_client_id_fallback(self.store.get_state(auth.CLIENT_ID_KEY))

    # ------------------------------------------------------------- plumbing

    def _client(self) -> OneNoteClient:
        if self._graph is None:
            account = self.store.get_state(auth.ACCOUNT_KEY)
            timeout = float(_env("JOB_TRACKER_GRAPH_TIMEOUT", "30") or 30)
            self._graph = GraphClient(
                lambda: auth.acquire_token_silent(account), timeout=timeout
            )
        return OneNoteClient(self._graph)

    def _section_id(self) -> str:
        """Resolve once, cache, then re-validate cheaply on every use."""
        override = _env("JOB_TRACKER_ONENOTE_SECTION_ID")
        if override:
            return override

        cached = self.store.get_state(K_SECTION_ID)
        c = self._client()
        if cached:
            try:
                live = c.validate_section(cached)
            except GraphError as exc:
                if getattr(exc, "status", None) == 404:
                    # Deleted or moved. Do NOT auto-re-resolve: that can silently
                    # retarget a different section.
                    self.store.clear_state(K_SECTION_ID, K_SECTION_NAME, K_NOTEBOOK_ID)
                    raise GraphError(
                        f"The cached OneNote section no longer exists. Run "
                        f"onenote_list_sections to find the right one, then sync again."
                    ) from None
                raise
            if live.get("displayName") != self.store.get_state(K_SECTION_NAME):
                raise GraphError(
                    f"The section was renamed to {live.get('displayName')!r} "
                    f"(expected {self.store.get_state(K_SECTION_NAME)!r}). This may mean the "
                    f"notebook was reorganized. Confirm with onenote_list_sections, "
                    f"then set JOB_TRACKER_ONENOTE_SECTION to the new name."
                )
            return cached

        resolved = c.resolve_section(notebook_name(), section_name())
        self.store.set_state(K_NOTEBOOK_ID, resolved["notebook_id"])
        self.store.set_state(K_SECTION_ID, resolved["section_id"])
        self.store.set_state(K_SECTION_NAME, resolved["section_name"])
        return resolved["section_id"]

    # ---------------------------------------------------------------- tools

    def auth_status(self) -> dict:
        """Never returns {"error"} for the un-configured case.

        "You have not set this up yet" is a successful diagnosis; an error string
        would send the model hunting for a bug.
        """
        out = {"configured": False, "authenticated": False,
               "write_enabled": write_enabled(),
               "cached_pages": self.store.count_pages(),
               "last_sync": self.store.get_state(K_LAST_SYNC),
               "notebook": {"display_name": notebook_name(),
                            "id": self.store.get_state(K_NOTEBOOK_ID)},
               "section": {"display_name": section_name(),
                           "id": self.store.get_state(K_SECTION_ID)}}
        try:
            auth.client_id()
        except auth.AuthError as exc:
            out["message"] = str(exc)
            out["reason"] = exc.reason
            return out
        out["configured"] = True

        try:
            info = auth.account_info(self.store.get_state(auth.ACCOUNT_KEY))
        except auth.AuthError as exc:
            out["message"] = str(exc)
            out["reason"] = exc.reason          # e.g. keychain_timeout
            return out

        if not info:
            out["message"] = str(auth.not_authenticated())
            out["reason"] = "not_authenticated"
            return out

        out["authenticated"] = True
        out["account"] = info["username"]
        out["scopes"] = auth.SCOPES
        return out

    def list_sections(self, notebook: str | None = None) -> dict:
        c = self._client()
        books = c.notebooks()
        if notebook:
            from job_tracker_mcp.models import normalize_key_part
            want = normalize_key_part(notebook)
            books = [b for b in books if normalize_key_part(b.get("displayName")) == want]

        out, total = [], 0
        for b in books:
            secs = c.sections_of(b["id"])
            total += len(secs)
            out.append({
                "id": b["id"], "display_name": b.get("displayName"),
                "is_default": b.get("isDefault", False),
                "sections": [{"id": s["id"], "display_name": s.get("displayName"),
                              "path": " / ".join([b.get("displayName", ""), *s["path"]])}
                             for s in secs],
            })
        return {"notebooks": out, "total_sections": total}

    def sync(self, full: bool = False, fetch_content: bool = True,
             max_pages: int = 200) -> dict:
        """Three phases. Phase N holds no lock and no transaction -- see the spec.

        Fetching page content inside a write transaction would hold Part 1's
        single connection lock across every HTTP round-trip, freezing every other
        tool in the server for the duration.
        """
        started = time.monotonic()
        max_pages = max(1, min(int(max_pages), 1000))
        c = self._client()
        section_id = self._section_id()

        # -- Phase R: short read, then release.
        if full:
            self.store.clear_content_sync(section_id)
        known = self.store.sync_state(section_id)

        # -- Phase N: network. No lock. No transaction.
        pages, truncated = c.list_pages(section_id, max_pages=max_pages)
        contents: dict[str, tuple[str, str]] = {}
        skipped = 0
        if fetch_content:
            for p in pages:
                prev = known.get(p["page_id"])
                cached = prev and prev["content_synced_dt"] == p["last_modified_dt"]
                if cached:
                    skipped += 1
                    continue
                contents[p["page_id"]] = c.page_content(p["page_id"])

        # -- Phase W: one short transaction. No network below this line.
        counts = self.store.write_sync(section_id, pages, contents)
        self.store.set_state(K_LAST_SYNC, now_str())

        msg = f"{len(contents)} page(s) fetched; {skipped} served from cache."
        if truncated:
            # A silently truncated sync that reports success is how a
            # reconciliation report ends up confidently wrong.
            msg += (f" TRUNCATED at max_pages={max_pages}: more pages exist and were "
                    f"NOT synced. Re-run with a higher max_pages.")

        return {"result": "synced", "section": self.store.get_state(K_SECTION_NAME),
                "pages_seen": len(pages), **counts,
                "content_fetched": len(contents), "content_skipped_cached": skipped,
                "truncated": truncated,
                "throttled_retries": self._graph.throttled_retries,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "message": msg}

    def get_page(self, page_id: str, format: str = "text",
                 max_chars: int = 20000, offset: int = 0) -> dict:
        row = self.store.get_page_row(page_id)
        if row is None:
            return _err(f"No cached page {page_id}. Run onenote_sync first.")
        if format not in ("text", "html"):
            return _err(f"Invalid format {format!r}. Use 'text' or 'html'.")

        body = (row["content_html"] if format == "html" else row["content_text"]) or ""
        max_chars = max(500, min(int(max_chars), 100_000))
        offset = max(0, int(offset))
        chunk = body[offset:offset + max_chars]
        end = offset + len(chunk)
        return {
            "page_id": page_id, "title": row["title"], "format": format,
            "content": chunk, "chars": len(chunk), "total_chars": len(body),
            "truncated": end < len(body),
            "offset": offset, "next_offset": end if end < len(body) else None,
            "last_modified_dt": row["last_modified_dt"],
            # The cached text predates the page's latest edit. Report it; do not
            # silently auto-sync inside a read tool.
            "stale": row["content_synced_dt"] != row["last_modified_dt"],
        }

    def create_page(self, title: str, body_lines: list[str],
                    job_id: str | None = None, confirm: bool = False) -> dict:
        if not write_enabled():
            return _err(
                "OneNote writing is disabled. Set JOB_TRACKER_ONENOTE_WRITE=1 in the "
                "`env` block of ~/Library/Application Support/Claude/"
                "claude_desktop_config.json, then fully quit (Cmd-Q) and relaunch "
                "Claude Desktop."
            )
        if not title or not str(title).strip():
            return _err("title is required")

        marker = None
        if job_id:
            job = self.db.get_job(job_id)
            if "error" in job:
                return job
            if job["merged_into"]:
                return _err(f"Job {job_id} was merged into {job['merged_into']}. "
                            f"Create the page for {job['merged_into']} instead.")
            existing = self.store.created_by_tool_link(job_id)
            if existing:
                # Idempotency, mirroring add_job's. Not an error, and not a second page.
                return {"result": "exists", "page_id": existing["page_id"],
                        "job_id": job_id,
                        "message": f"A page for this job was already created on "
                                   f"{existing['created_at']}."}
            marker = job_id

        html_created = now_str()
        if not confirm:
            from job_tracker_mcp.onenote.htmlio import build_page_html
            # A dry run by default. Page creation is not undoable through this
            # server, and a model that misreads an instruction should produce a
            # preview, not a page.
            return {"result": "preview",
                    "would_create": {"title": title, "body_lines": body_lines,
                                     "job_id": job_id,
                                     "section": self.store.get_state(K_SECTION_NAME)},
                    "html": build_page_html(title, body_lines, html_created, marker),
                    "message": "Set confirm=true to actually create this page."}

        section_id = self._section_id()
        row = self._client().create_page(section_id, title, body_lines,
                                         created=html_created, marker=marker)

        # Graph committed before SQLite did. If this insert fails the page still
        # exists -- which is exactly why the marker is in the page body: the local
        # link table is an optimization, the marker is the source of truth.
        try:
            self.store.upsert_page(row, section_id)
            if job_id:
                self.store.add_link(row["page_id"], job_id, "created_by_tool", 1.0,
                                    evidence="created by onenote_create_page")
        except Exception:  # noqa: BLE001
            log.exception("page %s created in OneNote but not recorded locally; "
                          "the next onenote_sync will re-link it via its marker",
                          row["page_id"])

        return {"result": "created", "page_id": row["page_id"], "title": row["title"],
                "web_url": row["web_url"], "client_url": row["client_url"],
                "linked_job_id": job_id,
                "message": f"Page created in {notebook_name()} / "
                           f"{self.store.get_state(K_SECTION_NAME)}."}

    def link_page(self, page_id: str, job_id: str, unlink: bool = False,
                  evidence: str | None = None, date_applied: str | None = None,
                  extracted: dict | None = None) -> dict:
        if self.store.get_page_row(page_id) is None:
            return _err(f"No cached page {page_id}. Run onenote_sync first.")

        job = self.db.get_job(job_id)
        if "error" in job:
            return job
        if job["merged_into"]:
            return _err(f"Job {job_id} was merged into {job['merged_into']}. "
                        f"Link the page to {job['merged_into']} instead.")

        if unlink:
            existing = self.store.get_link(page_id, job_id)
            if existing and existing["match_kind"] == "created_by_tool":
                return _err("Cannot unlink a page this server created. "
                            "Delete the page in OneNote first.")
            if not self.store.remove_link(page_id, job_id):
                return _err(f"No link between {page_id} and {job_id}")
            return {"result": "unlinked", "page_id": page_id, "job_id": job_id}

        if date_applied and (msg := Database._bad_date(date_applied)):
            return _err(msg)

        result = self.store.add_link(page_id, job_id, "manual", 1.0, evidence=evidence,
                                     date_applied=date_applied, extracted=extracted,
                                     overwrite=True)
        return {"result": "linked" if result else "linked",
                "page_id": page_id, "job_id": job_id}

    def ignore_page(self, page_id: str, ignore: bool = True) -> dict:
        if not self.store.set_ignored(page_id, ignore):
            return _err(f"No cached page {page_id}. Run onenote_sync first.")
        return {"result": "ignored" if ignore else "unignored", "page_id": page_id}

    # ---------------------------------------------------------- reconcile

    def _records(self):
        jobs = [
            JobRecord(id=j["id"], company=j["company"], title=j["title"],
                      company_canonical=j["company_canonical"],
                      title_canonical=j["title_canonical"],
                      status=j["status"], date_applied=j["date_applied"])
            for j in self.db.live_jobs()          # merged rows excluded
        ]
        listing = self.store.list_pages(include_ignored=False, limit=MAX_LIMIT)
        pages = []
        for p in listing["pages"]:
            row = self.store.get_page_row(p["page_id"])
            pages.append(PageRecord(page_id=p["page_id"], title=p["title"],
                                    text=row["content_text"] or "",
                                    is_ignored=bool(row["is_ignored"]),
                                    created_dt=row["created_dt"],
                                    page_order=row["page_order"]))

        root = root_page()
        if root:
            scoped = scope_to_subtree(pages, root)
            if not scoped:
                log.warning("root page %r not found; reconciling nothing", root)
            pages = scoped

        links = [LinkRecord(page_id=l["page_id"], job_id=l["job_id"],
                            match_kind=l["match_kind"], confidence=l["confidence"],
                            date_applied=l["date_applied"])
                 for l in self.store.links()]
        return jobs, pages, links

    def reconcile(self, dry_run: bool = True,
                  min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                  apply_marks: bool = False, limit: int = 100) -> dict:
        min_confidence = max(0.3, min(float(min_confidence), 1.0))
        jobs, pages, links = self._records()
        rec = match_pages(pages, jobs, links, min_confidence,
                          default_year=date.today().year)
        scope_note = (f" Scoped to the {len(pages)} page(s) under {root_page()!r}."
                      if root_page() else "")

        # Persist the derived links so the extraction cost is paid once per page.
        # ON CONFLICT DO NOTHING: a manual link is never overwritten.
        for e in rec.matched + rec.onenote_only:
            if e["match_kind"] != "manual":
                self.store.add_link(e["page_id"], e["job_id"], e["match_kind"],
                                    e["confidence"], evidence="reconcile",
                                    date_applied=e.get("date_applied"),
                                    overwrite=False)

        out = {
            "matched": rec.matched[:limit], "matched_count": len(rec.matched),
            "onenote_only": rec.onenote_only[:limit],
            "db_only": rec.db_only[:limit],
            "ambiguous": rec.ambiguous[:limit],
            "unmatched": rec.unmatched[:limit],
            "unparsed_pages": rec.unparsed_pages[:limit],
            "dry_run": dry_run, "min_confidence": min_confidence,
        }

        if dry_run or not apply_marks:
            n = len(rec.onenote_only)
            out["scope"] = {"root_page": root_page(), "pages_in_scope": len(pages)}
            out["message"] = (
                (f"{n} job(s) appear applied in OneNote but not in the database. "
                 f"Re-run with dry_run=false, apply_marks=true to update."
                 if n else "No discrepancies to apply.") + scope_note
            )
            return out

        applied, skipped = [], []
        for e in rec.onenote_only:
            if e["confidence"] < min_confidence:
                skipped.append({**e, "reason": "below min_confidence"})
                continue
            # Reuses Part 1's tool, including its ALLOWED_TRANSITIONS check. Part 2
            # never writes to `jobs` directly and never bypasses the lifecycle.
            res = self.db.mark_applied(
                e["job_id"], date_applied=e.get("date_applied"),
                notes=f"Reconciled from OneNote page {e['page_title']!r} on {today_str()}",
            )
            (skipped if "error" in res else applied).append(
                {**e, **({"reason": res["error"]} if "error" in res else {})}
            )

        out["applied"] = applied
        out["skipped"] = skipped
        out["message"] = (f"{len(applied)} job(s) marked applied; "
                          f"{len(skipped)} skipped. Ambiguous entries are never "
                          f"acted on.")
        return out
