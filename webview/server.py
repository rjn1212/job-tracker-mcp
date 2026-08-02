#!/usr/bin/env python3
"""Local web viewer for the job-tracker database.

Serves a single-page UI and a JSON API over the jobs table. Reads open the DB
read-only. One narrow write path -- POST /api/status -- flips a job's status to
rejected (or back to new) through the app's own update_job. Binds to localhost
only, and blocks cross-origin POSTs.

  PORT=8787 JOB_TRACKER_DB=/path/to/jobs.db python server.py
"""
import json
import os
import re
import sqlite3
import sys
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("JOB_TRACKER_DB", os.path.join(HERE, "..", "data", "jobs.db"))


def _env_int(name, default):
    raw = os.environ.get(name, default)
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"Invalid {name}={raw!r}: want an integer")


PORT = _env_int("PORT", "8787")
HOST = "127.0.0.1"
MAX_ROWS = 5000
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SORTS = {"date": "date_seen DESC, last_seen DESC",
         "score": "fit_score DESC, date_seen DESC",
         # applied date, most recent first; un-applied (NULL) sink last in DESC
         "applied": "date_applied DESC, date_seen DESC"}
STATUSES = {"new", "applied", "expired", "rejected", "hold"}

FIELDS = ("id", "title", "company", "date_seen", "last_seen", "fit_score",
          "status", "source", "url", "salary_raw", "date_applied", "date_posted",
          "valid_through", "description")


def connect():
    # Read-only: the viewer must never write the live DB. Prefer a true ro open;
    # fall back to a normal open pinned query_only, because a pure ro open can
    # fail to map the -wal/-shm of a concurrently-written WAL database (e.g. the
    # writer stopped with an uncheckpointed WAL). The SELECT forces the open now
    # so the fallback triggers here rather than on the first real query.
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        conn.execute("SELECT 1")
        return conn
    except sqlite3.OperationalError:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute("PRAGMA query_only = 1")
        return conn


def _int(qs, key, default=0):
    try:
        return max(0, int(qs.get(key, [default])[0]))
    except (TypeError, ValueError):
        return default


def query_jobs(qs):
    since = qs.get("since", [""])[0].strip()
    min_score = _int(qs, "min_score", 0)
    min_desc_len = _int(qs, "min_desc_len", 0)
    status = qs.get("status", [""])[0].strip().lower()
    applied = qs.get("applied", [""])[0].strip().lower()
    jobid = qs.get("jobid", [""])[0].strip().lower()
    company = qs.get("company", [""])[0].strip().lower()
    sort = qs.get("sort", ["date"])[0]

    where = ["merged_into IS NULL"]
    params = []
    if DATE_RE.match(since):
        where.append("date_seen >= ?"); params.append(since)
    if min_score > 0:
        where.append("fit_score >= ?"); params.append(min_score)
    if min_desc_len > 0:
        where.append("COALESCE(LENGTH(description), 0) >= ?"); params.append(min_desc_len)
    if status in STATUSES:
        where.append("status = ?"); params.append(status)
    # "applied" = has a date_applied (the durable signal; survives a later
    # status change to expired/rejected).
    if applied == "yes":
        where.append("date_applied IS NOT NULL")
    elif applied == "no":
        where.append("date_applied IS NULL")
    if jobid:
        # substring "find" on the job id; ids are lowercase hex, input is lowered
        where.append("instr(id, ?) > 0"); params.append(jobid)
    if company:
        # case-insensitive substring "find" on company name
        where.append("instr(lower(company), ?) > 0"); params.append(company)

    order = SORTS.get(sort, SORTS["date"])
    sql = (f"SELECT {', '.join(FIELDS)} FROM jobs "
           f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT {MAX_ROWS}")

    conn = connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    jobs = []
    for r in rows:
        d = dict(r)
        desc = d.pop("description")
        d["desc_len"] = len(desc) if desc else 0
        d["description"] = desc or ""
        jobs.append(d)
    return {"count": len(jobs), "jobs": jobs}


_write_db = None
_write_lock = threading.Lock()


ACTION_STATUS = {"reject": "rejected", "unreject": "new", "hold": "hold"}


def apply_action(jid, action):
    """The one write path: change a job's status via the app's update_job, so
    transition rules apply. 'unhold' resolves to expired when the posting is
    already past its validThrough, otherwise back to new. The read-write
    Database is created lazily and shared (thread-safe: check_same_thread=False
    + lock)."""
    global _write_db
    with _write_lock:
        if _write_db is None:
            sys.path.insert(0, os.path.dirname(HERE))  # repo root, for job_tracker_mcp
            from job_tracker_mcp.database import Database
            _write_db = Database()
        if action == "unhold":
            row = _write_db.conn.execute(
                "SELECT valid_through FROM jobs WHERE id = ?", (jid,)).fetchone()
            vt = row["valid_through"] if row else None
            status = "expired" if (vt and vt < date.today().isoformat()) else "new"
        else:
            status = ACTION_STATUS[action]
        return _write_db.update_job(jid, status=status)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        # no-store: the page and data are live and change often; never let the
        # browser serve a stale cached copy (which looks like "filters do nothing").
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        route = urlparse(self.path)
        if route.path == "/":
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "index.html not found", "text/plain")
        elif route.path == "/api/jobs":
            try:
                data = query_jobs(parse_qs(route.query))
                self._send(200, json.dumps(data), "application/json")
            except Exception as e:  # noqa: BLE001 -- report, don't crash the server
                self._send(500, json.dumps({"error": str(e)}), "application/json")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        route = urlparse(self.path)
        if route.path != "/api/status":
            self._send(404, "not found", "text/plain"); return
        # CSRF guard: a cross-site POST from another page carries a foreign
        # Origin; our own page's fetch sends our host (or no Origin at all).
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
            self._send(403, json.dumps({"error": "cross-origin blocked"}), "application/json")
            return
        length = int(self.headers.get("Content-Length") or 0)
        params = parse_qs(self.rfile.read(length).decode("utf-8")) if length else {}
        jid = params.get("id", [""])[0].strip().lower()
        action = params.get("action", ["reject"])[0]
        if not re.fullmatch(r"[0-9a-f]{16}", jid):
            self._send(400, json.dumps({"error": "bad id"}), "application/json"); return
        if action not in ("reject", "unreject", "hold", "unhold"):
            self._send(400, json.dumps({"error": "bad action"}), "application/json"); return
        try:
            self._send(200, json.dumps(apply_action(jid, action)), "application/json")
        except Exception as e:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}), "application/json")

    def log_message(self, *args):
        pass  # quiet


def main():
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"DB not found: {DB_PATH} (set JOB_TRACKER_DB)")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Job-tracker viewer -> http://{HOST}:{PORT}   (DB: {os.path.abspath(DB_PATH)}, read-only)")
    print("Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
