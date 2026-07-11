"""Fetch job descriptions programmatically and cache them.

LinkedIn: public guest job-posting endpoint (no auth). 79 of the worklist.
Built In: public page. 9 of the worklist.
Indeed:   blocks headless fetch (403); handled separately via the agent's
          browser-grade WebFetch.

Throttled with a per-host delay so no board is hammered.
"""
import html as H
import re
import sys
import time

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


def linkedin(client, url):
    m = re.search(r"/jobs/view/(\d+)", url)
    if not m:
        return None
    r = client.get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}")
    if r.status_code != 200:
        return f"__ERR__{r.status_code}"
    blk = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', r.text, re.S)
    body = clean(blk.group(1)) if blk else ""
    crit = re.findall(r'description__job-criteria-subheader[^>]*>(.*?)</h3>.*?'
                      r'description__job-criteria-text[^>]*>(.*?)</span>', r.text, re.S)
    extra = "\n".join(f"{clean(k)}: {clean(v)}" for k, v in crit)
    full = (body + ("\n\n" + extra if extra else "")).strip()
    return full or "__ERR__empty"


def builtin(client, url):
    r = client.get(url)
    if r.status_code != 200:
        return f"__ERR__{r.status_code}"
    # the JD sits in a large content block; grab the biggest <div>/<section> text
    m = re.search(r'(?is)job-post-item-description.*?>(.*?)</(?:div|section|article)>', r.text)
    if not m:
        m = re.search(r'(?is)<section[^>]*>(.*?)</section>', r.text)
    txt = clean(m.group(1)) if m else ""
    return txt or "__ERR__empty"


def main():
    import json
    work = json.load(open("/tmp/jd_worklist.json"))
    db = Database()
    last_hit = {}
    done = err = skipped_indeed = 0
    indeed = []
    with httpx.Client(headers=UA, follow_redirects=True, timeout=25) as client:
        for j in work:
            url = j["url"]
            host = "linkedin" if "linkedin.com" in url else \
                   "builtin" if "builtin.com" in url else \
                   "indeed" if "indeed.com" in url else "other"
            if host == "indeed" or host == "other":
                indeed.append(j); skipped_indeed += 1; continue
            # throttle per host
            wait = PER_HOST_DELAY - (time.monotonic() - last_hit.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            fn = linkedin if host == "linkedin" else builtin
            try:
                text = fn(client, url)
            except Exception as e:  # noqa: BLE001
                text = f"__ERR__{type(e).__name__}"
            last_hit[host] = time.monotonic()
            if not text or text.startswith("__ERR__") or len(text) < 120:
                err += 1
                print(f"  MISS [{host}] {j['company'][:20]:20} {j['title'][:30]:30} {text[:16] if text else 'none'}")
                continue
            db.set_job_description(j["id"], text[:8000])
            done += 1
    db.close()
    print(f"\n  stored {done}, missed {err}, deferred {skipped_indeed} Indeed to WebFetch")
    json.dump(indeed, open("/tmp/jd_indeed.json", "w"))


if __name__ == "__main__":
    main()
