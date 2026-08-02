"""Fetch job descriptions programmatically and cache them.

LinkedIn: public guest job-posting endpoint (no auth).
Built In: schema.org JobPosting embedded in the page (JSON-LD `description`).
Indeed:   blocks headless fetch (403); deferred to the agent's browser-grade
          WebFetch (written to /tmp/jd_indeed.json).

Hosts are fetched round-robin, one per lap, so a host's per-host throttle window
is spent fetching the OTHER hosts rather than sleeping. Each host still honors
PER_HOST_DELAY between its own calls, so no board is hammered.

Input:  /tmp/jd_worklist.json  -- [{id, url, company, title}, ...]
Output: descriptions written to the DB; Indeed/other deferred to /tmp/jd_indeed.json
"""
import html as H
import json
import re
import sys
import time
from collections import OrderedDict, deque

import httpx

sys.path.insert(0, ".")
from job_tracker_mcp.database import Database

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}
PER_HOST_DELAY = 2.5   # seconds between calls to the same board


def clean(fragment: str) -> str:
    fragment = re.sub(r"(?is)<(script|style).*?</\1>", " ", fragment)
    fragment = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"(?i)</(p|li|div|h[1-6])>", "\n", fragment)
    txt = H.unescape(re.sub(r"(?s)<[^>]+>", " ", fragment))
    txt = re.sub(r"[ \t]+", " ", txt)
    return re.sub(r"\n\s*\n+", "\n\n", txt).strip()


_DATE_POSTED = re.compile(r'"datePosted"\s*:\s*"(\d{4}-\d{2}-\d{2})')
_VALID_THROUGH = re.compile(r'"validThrough"\s*:\s*"(\d{4}-\d{2}-\d{2})')


def extract_meta(text):
    """Best-effort posting metadata from a page's schema.org JobPosting: ISO
    datePosted and validThrough. Built In embeds both; LinkedIn's guest API
    usually has only a relative "N days ago", which we skip rather than store an
    approximate date."""
    dp = _DATE_POSTED.search(text)
    vt = _VALID_THROUGH.search(text)
    return {"date_posted": dp.group(1) if dp else None,
            "valid_through": vt.group(1) if vt else None}


# Each fetcher returns (text, meta): text is the JD (or "__ERR__..."), meta is a
# dict of posting metadata (date_posted, valid_through) -- empty {} on error.
def linkedin(client, url):
    m = re.search(r"/jobs/view/(\d+)", url)
    if not m:
        return None, {}
    r = client.get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}")
    if r.status_code != 200:
        return f"__ERR__{r.status_code}", {}
    meta = extract_meta(r.text)
    blk = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', r.text, re.S)
    body = clean(blk.group(1)) if blk else ""
    crit = re.findall(r'description__job-criteria-subheader[^>]*>(.*?)</h3>.*?'
                      r'description__job-criteria-text[^>]*>(.*?)</span>', r.text, re.S)
    extra = "\n".join(f"{clean(k)}: {clean(v)}" for k, v in crit)
    full = (body + ("\n\n" + extra if extra else "")).strip()
    return (full or "__ERR__empty"), meta


def builtin(client, url):
    r = client.get(url)
    if r.status_code != 200:
        return f"__ERR__{r.status_code}", {}
    meta = extract_meta(r.text)
    # Built In embeds a schema.org JobPosting; its `description` is the JD as
    # HTML. This is stable across page redesigns -- the old content-div scrape
    # (job-post-item-description) broke when that container was removed.
    m = re.search(r'"@type"\s*:\s*"JobPosting".*?"description"\s*:\s*"((?:[^"\\]|\\.)*)"',
                  r.text, re.S)
    if m:
        return (clean(json.loads('"' + m.group(1) + '"')) or "__ERR__empty"), meta
    # fallback: older content-block scrape, in case a page lacks the JSON-LD
    m = re.search(r'(?is)job-post-item-description.*?>(.*?)</(?:div|section|article)>', r.text)
    if not m:
        m = re.search(r'(?is)<section[^>]*>(.*?)</section>', r.text)
    return ((clean(m.group(1)) if m else "") or "__ERR__empty"), meta


FETCHERS = {"linkedin": linkedin, "builtin": builtin}


def host_of(url: str) -> str:
    if "linkedin.com" in url:
        return "linkedin"
    if "builtin.com" in url:
        return "builtin"
    if "indeed.com" in url:
        return "indeed"
    return "other"


def main():
    work = json.load(open("/tmp/jd_worklist.json"))
    db = Database()

    queues = OrderedDict((h, deque()) for h in FETCHERS)   # fixed host order per lap
    deferred = []
    for j in work:
        host = host_of(j["url"])
        (queues[host] if host in FETCHERS else deferred).append(j)

    last_hit = {}
    done = err = 0
    with httpx.Client(headers=UA, follow_redirects=True, timeout=25) as client:
        while any(queues.values()):
            for host, q in queues.items():
                if not q:
                    continue
                j = q.popleft()
                wait = PER_HOST_DELAY - (time.monotonic() - last_hit.get(host, 0))
                if wait > 0:
                    time.sleep(wait)
                try:
                    text, meta = FETCHERS[host](client, j["url"])
                except Exception as e:  # noqa: BLE001
                    text, meta = f"__ERR__{type(e).__name__}", {}
                last_hit[host] = time.monotonic()
                if not text or text.startswith("__ERR__") or len(text) < 120:
                    err += 1
                    print(f"  MISS [{host}] {j['company'][:20]:20} {j['title'][:30]:30} "
                          f"{text[:16] if text else 'none'}")
                    continue
                db.set_job_description(j["id"], text[:8000],
                                      date_posted=meta.get("date_posted"),
                                      valid_through=meta.get("valid_through"))
                done += 1
    db.close()
    print(f"\n  stored {done}, missed {err}, deferred {len(deferred)} to WebFetch (Indeed/other)")
    # The WebFetch step that handles these should also read the posting's stated
    # date and pass it: set_job_description(id, text, date_posted="YYYY-MM-DD").
    # Indeed usually shows a relative "posted N days ago"; only store an exact date.
    json.dump(deferred, open("/tmp/jd_indeed.json", "w"))


if __name__ == "__main__":
    main()
