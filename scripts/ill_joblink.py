"""Fetch and parse IllinoisJobLink.com job postings (AJLA state job bank).

The alert email (notifications@ajla.net) lists only Title | Posting ID | City with
no company or salary, so each posting must be pulled from its own page to learn
the employer and pay. Public pages render without a login at
    https://illinoisjoblink.illinois.gov/jobs/{ID}
and expose the fields as plain text (no schema.org JSON-LD). Removed/expired jobs
return HTTP 401 with a "job is not active" page.

Key extraction anchors (stable across postings):
  - <h1> holds the title; <title> is "<title> - IllinoisJobLink.com".
  - A header line reads "<title> at <COMPANY> in <CITY, STATE, United States>"
    immediately before the "Job Description"/"Job Overview"/"Posting Type" block.
  - Salary appears in the JD prose, most often "...between following values:$X and $Y",
    or a "$X - $Y" / "$X.00 - $Y.00/yr" range, or a single "$X/hr".
"""
import re
import html as H

import httpx

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}

STATES = {"Illinois": "IL", "Indiana": "IN", "Wisconsin": "WI", "Iowa": "IA",
          "Missouri": "MO", "Michigan": "MI", "California": "CA", "Texas": "TX",
          "New York": "NY", "Washington": "WA", "Georgia": "GA", "Florida": "FL",
          "Ohio": "OH", "North Carolina": "NC", "Minnesota": "MN", "Virginia": "VA"}


def _strip(fragment: str) -> str:
    fragment = re.sub(r"(?is)<(script|style).*?</\1>", " ", fragment)
    txt = H.unescape(re.sub(r"(?s)<[^>]+>", " ", fragment))
    return re.sub(r"\s+", " ", txt).strip()


_HDR = re.compile(
    r"\bat\s+(.+?)\s+in\s+([A-Z][^,]+?,\s*[A-Za-z ]+?)(?:,\s*United States)?\s+"
    r"(?:Job\s+Description|Job\s+Overview|Posting\s+Type|Job\s+Details|Occupation)",
    re.S)
_BETWEEN = re.compile(r"between following values\s*:?\s*\$?([\d,]+(?:\.\d+)?)\s*and\s*\$?([\d,]+(?:\.\d+)?)", re.I)
_RANGE = re.compile(r"\$([\d,]+(?:\.\d+)?)\s*(?:-|–|to)\s*\$?([\d,]+(?:\.\d+)?)\s*(/\s*hr|/\s*hour|per\s+hour)?", re.I)
_HOURLY_ONE = re.compile(r"\$([\d,]+(?:\.\d+)?)\s*(?:/\s*hr|/\s*hour|per\s+hour)", re.I)


def _num(s):
    return int(round(float((s or "0").replace(",", ""))))


def _clean_loc(loc: str) -> str:
    loc = loc.strip().rstrip(",").strip()
    city, _, state = loc.rpartition(",")
    if city:
        city = city.strip()
        city = re.sub(r"^(.+?)\1$", r"\1", city)   # source doubles it: "ChicagoChicago"
        st = state.strip()
        return f"{city}, {STATES.get(st, st)}"
    return loc


def parse_salary(text: str):
    """Best-effort (raw, min, max, is_hourly) from JD prose. None if not found."""
    m = _BETWEEN.search(text)
    if m:
        return (f"${m.group(1)} - ${m.group(2)}", _num(m.group(1)), _num(m.group(2)), False)
    m = _RANGE.search(text)
    if m:
        hourly = bool(m.group(3))
        lo, hi = _num(m.group(1)), _num(m.group(2))
        if lo and (lo != 0 or hi != 0):
            raw = f"${m.group(1)} - ${m.group(2)}" + ("/hr" if hourly else "")
            return (raw, lo, hi, hourly)
    m = _HOURLY_ONE.search(text)
    if m:
        v = _num(m.group(1))
        return (f"${m.group(1)}/hr", v, v, True)
    return None


def parse(html_text: str, url: str = "") -> dict:
    """Structured fields from a posting's HTML. status: OK | EXPIRED."""
    if "job is not active" in html_text.lower() or "Unauthorized - IllinoisJobLink" in html_text:
        return {"status": "EXPIRED", "url": url}
    mh = re.search(r"<h1[^>]*>(.*?)</h1>", html_text, re.S)
    mt = re.search(r"<title>(.*?)</title>", html_text, re.S)
    title = _strip(mh.group(1)) if mh else (
        _strip(mt.group(1)).split(" - IllinoisJobLink")[0] if mt else "")
    flat = _strip(html_text)
    company, location = None, None
    mhdr = _HDR.search(flat)
    if mhdr:
        company = mhdr.group(1).strip()
        location = _clean_loc(mhdr.group(2))
    # description: from the overview/description marker onward, trimmed at apply/footer
    body = flat
    ms = re.search(r"(Job Overview|Job Description|Posting Type)", body)
    if ms:
        body = body[ms.start():]
    for cut in ("How To Apply", "How to Apply", "Application Instructions",
                "Similar Jobs", "Save Job", "Report this job", "Occupations"):
        i = body.find(cut)
        if i > 200:
            body = body[:i]
    sal = parse_salary(flat)
    salary_raw = sal[0] if sal else None
    return {"status": "OK", "url": url, "title": title, "company": company,
            "location": location, "salary_raw": salary_raw,
            "salary_min": sal[1] if sal else None,
            "salary_max": sal[2] if sal else None,
            "is_hourly": sal[3] if sal else False,
            "annual_max": (sal[2] * 2080 if sal and sal[3] else (sal[2] if sal else None)),
            "description": body[:8000]}


def fetch(url: str, client: httpx.Client | None = None) -> dict:
    own = client is None
    client = client or httpx.Client(headers=UA, follow_redirects=True, timeout=25)
    try:
        r = client.get(url)
        if r.status_code == 401 or "job is not active" in r.text.lower():
            return {"status": "EXPIRED", "url": url}
        if r.status_code != 200:
            return {"status": f"FAILED:{r.status_code}", "url": url}
        return parse(r.text, url)
    finally:
        if own:
            client.close()


def url_for(posting_id) -> str:
    return f"https://illinoisjoblink.illinois.gov/jobs/{posting_id}"
