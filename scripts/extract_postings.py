"""Deterministic posting extraction, one parser per sender layout.

The links are the authoritative posting list where a board sends them: the
anchor text IS the job title, so it cannot drift from what the body says.
Company, location and salary are then read from the body around that title.
"""
import re, json

SKIP = "messages-noreply"
FOOTER = ("See all jobs", "View all jobs", "GET MORE RECOMMENDATIONS",
          "Get the new LinkedIn", "This email was intended", "Discover more jobs",
          "Getting these notifications")
MIDDOT = "·"
SALARY = re.compile(r"^\$\s?[\d,]")
RATING = re.compile(r"^\d(\.\d)?$")          # Indeed's star rating line
WORKTYPE = re.compile(r"^(In Office|Remote|Hybrid)\b", re.I)


def clean(body: str) -> list[str]:
    for stop in FOOTER:
        if stop in body:
            body = body.split(stop)[0]
    out = []
    for l in body.split("\n"):
        l = l.replace("￼", " ").strip()
        if not l or "͏" in l or set(l) <= {"͏", " "}:
            continue
        out.append(l)
    return out


def board_of(sender: str) -> str:
    s = sender.lower()
    if "linkedin" in s:
        return "linkedin"
    if "indeed" in s:
        return "indeed"
    if "builtin" in s:
        return "builtin"
    if "oracle" in s:
        return "oracle"
    if "ajla" in s or "joblink" in s:
        return "illinoisjoblink"
    return "direct"


def _sal(lines, i):
    for j in (i + 1, i + 2):
        if j < len(lines) and SALARY.match(lines[j]):
            return lines[j]
    return None


REMOTE_IN_TITLE = re.compile(r"\(remote\)", re.I)
HAS_WORKTYPE = re.compile(r"\((remote|hybrid|on-?site)\)", re.I)


def _fold_remote(title, location):
    """LinkedIn writes "(Remote)" in the TITLE on some postings and on the
    LOCATION line on others. Same fact, two places, two ids unless folded."""
    if REMOTE_IN_TITLE.search(title) and not HAS_WORKTYPE.search(location):
        return f"{location} (Remote)"
    return location


def linkedin(lines, links):
    out = []
    for i, l in enumerate(lines):
        if MIDDOT not in l or i == 0:
            continue
        company, _, location = l.partition(MIDDOT)
        title = lines[i - 1].strip()
        if not title or SALARY.match(title) or MIDDOT in title:
            continue
        location = _fold_remote(title, location.strip() or "Unknown")
        out.append(dict(company=company.strip(), title=title,
                        location=location, salary=_sal(lines, i)))
    return out


def indeed(lines, links):
    out = []
    titles = {l["text"].strip(): l["url"] for l in links}
    for i, l in enumerate(lines):
        if l not in titles:
            continue
        j = i + 1
        if j >= len(lines):
            continue
        company = lines[j]; j += 1
        if j < len(lines) and RATING.match(lines[j]):
            j += 1                                  # skip the star rating
        location = lines[j] if j < len(lines) else "Unknown"
        salary = lines[j + 1] if j + 1 < len(lines) and SALARY.match(lines[j + 1]) else None
        out.append(dict(company=company, title=l, location=location or "Unknown",
                        salary=salary, url=titles[l]))
    return out


def builtin(lines, links):
    out = []
    for i, l in enumerate(lines):
        if not WORKTYPE.match(l) or i < 2:
            continue
        worktype, _, loc = l.partition(" ")
        location = (loc.strip() or worktype).strip()
        if worktype.lower() in ("remote",) and location.lower() in ("usa", "us"):
            location = "Remote"
        elif worktype.lower() != "remote":
            location = loc.strip() or "Unknown"
        out.append(dict(company=lines[i - 2], title=lines[i - 1],
                        location=location, salary=_sal(lines, i)))
    return out


def oracle(lines, links):
    try:
        start = next(i for i, l in enumerate(lines) if "latest roles" in l.lower()) + 1
    except StopIteration:
        return []
    return [dict(company="Oracle", title=l, location="Unknown", salary=None)
            for l in lines[start:] if len(l) > 6 and not l.lower().startswith("hello")]


# "CHICAGO, IL, US, 60654-4203" -> "Chicago, IL". The country and the postcode
# are noise, and a postcode in the key would split one job across two mailings.
ZIPPY = re.compile(r"^(.*?,\s*[A-Z]{2})\s*,\s*US.*$", re.I)


def _clean_direct_location(loc: str) -> str:
    m = ZIPPY.match(loc.strip())
    city_state = m.group(1) if m else loc.strip()
    city, _, state = city_state.rpartition(",")
    return f"{city.strip().title()}, {state.strip().upper()}" if city else city_state


def direct(lines, links, preamble_company=None):
    try:
        start = next(i for i, l in enumerate(lines) if l.strip().lower() == "job matches:") + 1
        candidates = lines[start:]
    except StopIteration:
        # No "job matches:" marker (e.g. Fitch's talent-community mail). Fall back
        # to the body lines that are themselves job-posting links -- their anchor
        # text is "Title - Location", so they carry exactly the postings.
        link_texts = {l["text"].strip() for l in links}
        candidates = [l for l in lines if l.strip() in link_texts]
    out = []
    for l in candidates:
        if " - " not in l:
            continue
        title, _, loc = l.rpartition(" - ")
        out.append(dict(company=preamble_company or "Unknown", title=title.strip(),
                        location=_clean_direct_location(loc), salary=None))
    return out


# IllinoisJobLink (AJLA, notifications@ajla.net): the body is a run-on table
# "Title | Posting ID | City" with one tracked link per row. The posting IDs are
# the only reliable field in the email; the employer and salary live on the
# posting page (see scripts/ill_joblink.py), so ingest ENRICHES each id before
# adding -- the parser just yields the canonical URL keyed by posting id.
AJLA_ID = re.compile(r"\|\s*(\d{6,})\s*\|")
AJLA_URL = "https://illinoisjoblink.illinois.gov/jobs/{}"


def illinoisjoblink(lines, links):
    body = "\n".join(lines)
    out, seen = [], set()
    for m in AJLA_ID.finditer(body):
        jid = m.group(1)
        if jid in seen:
            continue
        seen.add(jid)
        # best-effort title = the run-on text before this id, minus a leading
        # city carried over from the prior row; authoritative title comes from
        # enrichment, so a rough value is fine (and unused once enriched).
        pre = body[:m.start()].rpartition("|")[2].strip()
        out.append(dict(company="", title=pre[-80:].strip(), location="",
                        salary=None, url=AJLA_URL.format(jid), posting_id=jid))
    return out


PARSERS = dict(linkedin=linkedin, indeed=indeed, builtin=builtin, oracle=oracle,
               illinoisjoblink=illinoisjoblink)


def company_from_preamble(lines):
    """Direct-from-company mail names the employer only in prose or the domain."""
    for l in lines[:6]:
        for pat in (r"join(?:ed|ing) the (.+?) talent community",
                    r"jobs? (?:posted )?from ([A-Z][\w &.\-]+)",
                    r"at ([A-Z][\w &.\-]+?) (?:talent|careers)"):
            m = re.search(pat, l, re.I)
            if m:
                return m.group(1).strip().rstrip("!.,")
    return None


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def attach_urls(postings, links):
    """Fill each posting's `url` from the message's links by matching anchor
    text to the title. LinkedIn's anchor text IS the title; Built In's is
    "Company Title". Indeed postings already carry a url from their own parser
    and are left untouched. A posting with no matching link keeps url=None --
    the alert body named a job the links section didn't, which is rare but must
    not drop the posting.

    This lives here, not in the caller, so every consumer of extract() gets
    URL-complete postings without re-deriving the match.
    """
    by_text = {_norm(l["text"]): l["url"] for l in (links or [])}
    for p in postings:
        if p.get("url"):
            continue
        title = _norm(p["title"])
        combined = _norm(f"{p.get('company', '')} {p['title']}")
        if title in by_text:
            p["url"] = by_text[title]
        elif combined in by_text:
            p["url"] = by_text[combined]
        else:
            p["url"] = next((u for t, u in by_text.items()
                             if title and (title in t or t.endswith(title))), None)
    return postings


def extract(sender, body, links):
    board = board_of(sender)
    lines = clean(body)
    if board == "direct":
        postings = direct(lines, links, company_from_preamble(lines))
    else:
        postings = PARSERS[board](lines, links)
    return attach_urls(postings, links), board
