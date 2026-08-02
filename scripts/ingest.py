#!/usr/bin/env python3
"""End-to-end job-alert ingest.

Reads new mail from the hardened apple-mail-mcp server, parses each alert with
extract_postings (URLs already attached there), and inserts via the job
tracker's own bulk_add_jobs path. The triage watermark lives in the DB
(app_state); this reads it, fetches only newer mail, and advances it to the
newest message processed. Idempotent -- re-running reprocesses the boundary
message but adds nothing new, since add_job records a re-sighting.

  python scripts/ingest.py                       # fetch, insert, advance watermark
  python scripts/ingest.py --dry-run             # parse only; no writes
  python scripts/ingest.py --since 2026-07-11    # override the start (backfill)
  python scripts/ingest.py --since 2026-07-11 --dry-run

Requires the apple-mail-mcp binary (default ~/.local/bin/apple-mail-mcp), which
is hardened to read-only access of the RandyN Exchange / Job_Alerts_Viewed
mailbox. Run the DB-writing form single-writer -- don't trigger a Desktop
triage run against the same DB at the same time.
"""
import argparse
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

from job_tracker_mcp.database import Database
import extract_postings as ep

# get_message_links' own job_links_only filter recognizes only LinkedIn/Indeed/
# awstrack URLs and drops company careers links (e.g. jobs.grainger.com/job/...),
# which left those postings with no url. So fetch links unfiltered and keep only
# these posting-shaped URLs -- covers careers/ATS pages while excluding nav,
# social, and preference links, so extract's anchor-text matching can't misfire.
_JOB_URL = re.compile(r"(/job/|/jobs/view|viewjob|[?&]jk=|awstrack\.me|"
                      r"myworkdayjobs\.com|greenhouse\.io|lever\.co|icims\.com|ashbyhq)",
                      re.I)

MAIL_BIN = os.environ.get("APPLE_MAIL_MCP", os.path.expanduser("~/.local/bin/apple-mail-mcp"))
ACCOUNT = "RandyN Exchange"
MAILBOX = "Job_Alerts_Viewed"


class AppleMail:
    """Minimal MCP stdio client for the apple-mail-mcp binary."""

    def __init__(self, binary=MAIL_BIN):
        self.p = subprocess.Popen(
            [binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self._id = 0
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "ingest", "version": "1"}})

    def _rpc(self, method, params):
        self._id += 1
        rid = self._id
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                       "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("apple-mail-mcp closed the connection")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # the server logs JSON to stdout too; skip non-replies
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg["result"]

    def call(self, tool, **args):
        res = self._rpc("tools/call", {"name": tool, "arguments": args})
        txt = "\n".join(c.get("text", "") for c in res.get("content", [])
                        if c.get("type") == "text")
        return json.loads(txt)

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


def new_messages(mail, watermark):
    """Job_Alerts_Viewed messages strictly newer than the watermark, oldest
    first. A null watermark (never run) returns the whole mailbox."""
    args = dict(account=ACCOUNT, mailbox=MAILBOX, sort="oldest", limit=1000)
    if watermark:
        args["date_from"] = watermark
    msgs = mail.call("search_messages", **args)["messages"]
    return [m for m in msgs if not watermark or m["date_received"] > watermark]


def postings_for(mail, msg):
    full = mail.call("get_message", message_id=msg["id"], include_content=True)["message"]
    raw = mail.call("get_message_links", message_id=msg["id"],
                    job_links_only=False).get("links", [])
    links = [l for l in raw if _JOB_URL.search(l.get("url", ""))]
    postings, board = ep.extract(full.get("sender", ""), full.get("content", ""), links)
    for p in postings:
        p["source"] = board
        p["email_subject"] = msg["subject"]
        p["date_seen"] = msg["date_received"][:10]
    return postings, board


def main():
    ap = argparse.ArgumentParser(description="Ingest job-alert email into the tracker.")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and parse only; no inserts, no watermark change")
    ap.add_argument("--since", metavar="DATETIME",
                    help="override the watermark start (ISO date or datetime); for backfill")
    args = ap.parse_args()

    db = Database()
    watermark = args.since or db.get_triage_state(ACCOUNT, MAILBOX).get("last_message_dt")
    print(f"watermark: {watermark or '(none -- full mailbox)'}"
          + ("  [--since override]" if args.since else ""))

    mail = AppleMail()
    msgs = new_messages(mail, watermark)
    print(f"new messages: {len(msgs)}\n")

    seen = 0
    newest = watermark
    tot = dict(created=0, duplicates=0, auto_rejected=0, off_profile=0, errors=0)
    for msg in msgs:
        postings, board = postings_for(mail, msg)
        seen += len(postings)
        if not newest or msg["date_received"] > newest:
            newest = msg["date_received"]
        miss = sum(1 for p in postings if not p.get("url"))
        flag = f"  ({miss} MISSING url)" if miss else ""
        print(f'  {msg["date_received"][:10]}  {board:8} {len(postings):2}  '
              f'{msg["subject"][:56]}{flag}')
        if not args.dry_run and postings:
            res = db.bulk_add_jobs(postings)
            for k in tot:
                tot[k] += res.get(k, 0)
    mail.close()

    total_missing = "checked per-message above"
    print(f"\npostings parsed: {seen}  ({total_missing})")
    if args.dry_run:
        print("dry run -- no writes")
    else:
        print("inserted: " + ", ".join(f"{k}={v}" for k, v in tot.items()))
        if newest and not args.since:
            db.record_triage_run(ACCOUNT, MAILBOX, newest,
                                 emails_processed=len(msgs), postings_seen=seen)
            print(f"watermark advanced -> {newest}")
        elif args.since:
            print("watermark NOT advanced (--since backfill run)")
    db.close()


if __name__ == "__main__":
    main()
