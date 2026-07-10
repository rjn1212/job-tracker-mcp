"""Matching OneNote pages against tracked jobs. Pure functions over data.

No SQL, no HTTP, no clock. That is what makes the interesting logic testable
without a Graph mock or a database, and it is where the two errors this design
made on paper would have lived.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from job_tracker_mcp.models import canonicalize_company, canonicalize_title
from job_tracker_mcp.onenote.htmlio import find_marker

# Same scale and same metric as Part 1's DUP_THRESHOLD -- word-set overlap, not
# character similarity. SequenceMatcher scores "staff engineer" vs "senior staff
# engineer" (0.80, a different level) ABOVE "staff engineer" vs "staff software
# engineer" (0.76, the same job). It inverts the answer; no threshold saves it.
DEFAULT_MIN_CONFIDENCE = 0.6
AMBIGUOUS_FLOOR = 0.5

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")                 # 2026-07-02
_DMY_NAME = re.compile(r"\b(\d{1,2})-([A-Za-z]{3,9})-(\d{4})\b")  # 15-Jun-2026
_MDY_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")       # 07/02/2026
_MDY_DASH = re.compile(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b")        # 07-09-2026
_MD_ONLY = re.compile(r"\b(\d{1,2})/(\d{1,2})\b")                 # 7/2

# The label hierarchy Randy actually uses: "Applied:" wins, then "Date:".
#
# \b matters more than it looks. Without it, `date\s*:` matches the tail of
# "candidates:" and harvests "s must possess 3 years" as a date. With it, the
# 'd' in "candidates" has a word character before it and cannot start a match.
_APPLIED_LABEL = re.compile(r"\bapplied\s*:\s*([^\n]{0,40})", re.I)
_DATE_LABEL = re.compile(r"\bdate\s*:\s*([^\n]{0,40})", re.I)

_HEADER_WORDS = {"company", "employer", "title", "role", "position", "date",
                 "applied", "status", "link", "url", "notes"}


@dataclass(frozen=True)
class JobRecord:
    id: str
    company: str
    title: str
    company_canonical: str
    title_canonical: str
    status: str
    date_applied: str | None = None


@dataclass(frozen=True)
class PageRecord:
    page_id: str
    title: str
    text: str
    is_ignored: bool = False
    created_dt: str | None = None      # Graph UTC; the best available "when I applied"
    page_order: int | None = None      # position in the section's visual sequence


@dataclass(frozen=True)
class LinkRecord:
    page_id: str
    job_id: str
    match_kind: str
    confidence: float
    date_applied: str | None = None


@dataclass
class Reconciliation:
    matched: list[dict] = field(default_factory=list)
    onenote_only: list[dict] = field(default_factory=list)
    db_only: list[dict] = field(default_factory=list)
    ambiguous: list[dict] = field(default_factory=list)
    # An application record read cleanly off a page, naming a company the tracker
    # has never heard of. Without this bucket such a row parses successfully,
    # matches nothing, and is reported nowhere -- a job you applied to that the
    # report silently omits. That is the exact failure this design exists to avoid.
    unmatched: list[dict] = field(default_factory=list)
    unparsed_pages: list[dict] = field(default_factory=list)


def title_similarity(a: str, b: str) -> float:
    """Jaccard over word sets. Imported from Part 1 so the two never diverge."""
    from job_tracker_mcp.database import Database
    return Database._title_similarity(a, b)


# ------------------------------------------------------------- extraction

def normalize_date(raw: str, default_year: int) -> str | None:
    """Every date shape seen in the real notebook, tried most-specific first."""
    raw = raw or ""

    if m := _ISO.search(raw):                       # 2026-07-02
        return f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}"

    if m := _DMY_NAME.search(raw):                  # 15-Jun-2026
        month = _MONTHS.get(m[2][:3].lower())
        if month:
            return f"{int(m[3]):04d}-{month:02d}-{int(m[1]):02d}"

    for pat in (_MDY_SLASH, _MDY_DASH):             # 07/02/2026, 07-09-2026
        if m := pat.search(raw):
            month, day = int(m[1]), int(m[2])
            if 1 <= month <= 12 and 1 <= day <= 31:
                return f"{int(m[3]):04d}-{month:02d}-{day:02d}"

    if m := _MD_ONLY.search(raw):                   # 7/2
        return f"{default_year:04d}-{int(m[1]):02d}-{int(m[2]):02d}"

    return None


# A first line that is NOTHING BUT a date, as on the Zoro page:
#     15-Jun-2026
#     Sr. Manager, Software Engineering
# Deliberately anchored and total: "R243298" and "Rob Bishop" must not match, and
# a date buried in a job description ("candidates with 10/10/2020 experience")
# must never be mistaken for the day you applied.
_BARE_DATE_LINE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2}|\d{1,2}-[A-Za-z]{3,9}-\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s*$"
)


def _first_line_date(text: str, default_year: int) -> str | None:
    for line in (text or "").split("\n"):
        if not line.strip():
            continue
        m = _BARE_DATE_LINE.match(line)
        return normalize_date(m.group(1), default_year) if m else None
    return None


def extract_applied_date(text: str, default_year: int) -> tuple[str | None, str | None]:
    """The date the page says you applied, and which label it came from.

    "Applied:", then "Date:", then a bare date alone on the first line. Returns
    (None, None) when none of those yields a date -- the caller then falls back
    to the page's creation date, which is when the note was written rather than
    when the application happened, and is reported as such.
    """
    for source, pat in (("applied", _APPLIED_LABEL), ("date", _DATE_LABEL)):
        for m in pat.finditer(text or ""):
            if parsed := normalize_date(m.group(1), default_year):
                return parsed, source
    if parsed := _first_line_date(text, default_year):
        return parsed, "first_line"
    return None, None


# Leading status marks a human puts on a page title: "x ", "X ", "*X* ".
_TITLE_MARK = re.compile(r"^\s*(?:\*+\s*[xX]\s*\*+|[xX])\s+")
# Split only on a dash with whitespace on at least one side, so "Coca-Cola" and
# "84.51 degrees" survive but "Mitr -Global IT" and "Acme - Manager" split.
_TITLE_SEP = re.compile(r"\s[-–—]\s*|\s*[-–—]\s")
_YEAR_PAGE = re.compile(r"^\s*\d{4}\s*$")


def is_anchor_page(title: str) -> bool:
    """A bare year, e.g. "2026". These are the parents of the job pages."""
    return bool(_YEAR_PAGE.match(title or ""))


def parse_page_title(title: str) -> dict | None:
    """Company, role, and rejection status from a page title.

    "Capital One - Director, SWE"    -> role in the title
    "x Capital One - Director, SWE"  -> the same, and Randy was turned down
    "Deloitte"                       -> company only; role is None

    `title` is None when the page is named for a company alone. Guessing a role
    there would be fabrication; those pages go to the model, which reads the
    body and supplies the real one.

    KNOWN HAZARD: a company whose name genuinely begins with "X " -- X (Twitter),
    or "X Corp" -- would be read as a rejection marker and lose its first word.
    No such company appears in this notebook. If one ever does, the title parse
    must be overridden with a manual link rather than the marker rule relaxed.
    """
    raw = (title or "").strip()
    rejected = bool(_TITLE_MARK.match(raw))
    t = _TITLE_MARK.sub("", raw).strip()
    if not t or is_anchor_page(t):
        return None
    parts = _TITLE_SEP.split(t, maxsplit=1)
    company = parts[0].strip(" -–—")
    role = parts[1].strip(" -–—") if len(parts) > 1 else ""
    if not company:
        return None
    return {"company": company, "title": role or None, "rejected": rejected}


def scope_to_subtree(pages: list[PageRecord], root_title: str) -> list[PageRecord]:
    """The pages indented under `root_title`, using the visual sequence.

    Graph exposes neither `level` nor `order` on pages, so true indentation is
    unavailable. But subpages always follow their parent in $orderby=order, and
    the next anchor page (a bare year) ends the subtree. Pages before the root,
    and the root itself, are excluded.

    An unknown root yields [] rather than everything -- silently reconciling the
    whole section would be worse than reconciling none of it.
    """
    ordered = sorted((p for p in pages if p.page_order is not None),
                     key=lambda p: p.page_order)
    root = next((p for p in ordered if (p.title or "").strip() == root_title.strip()), None)
    if root is None:
        return []
    out = []
    for p in ordered:
        if p.page_order <= root.page_order:
            continue
        if is_anchor_page(p.title):
            break
        out.append(p)
    return out


def extract_table_rows(text: str, default_year: int) -> list[dict]:
    """Tier A: structural extraction from a pipe-table, in code.

    Deliberately narrow. A regex parser for free-form prose works on the five
    pages you test it against and fails silently on the sixth; that is why Tier B
    (the model reads the page) is the primary path and this is the optimization.
    """
    lines = [ln for ln in text.splitlines() if "|" in ln]
    if not lines:
        return []

    header = [c.strip().lower() for c in lines[0].split("|")]
    if len(set(header) & _HEADER_WORDS) < 2:
        return []                                   # not a recognizable log table

    def col(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    ci, ti = col("company", "employer"), col("title", "role", "position")
    di, si = col("date", "applied"), col("status")
    if ci is None or ti is None:
        return []

    out = []
    for line in lines[1:]:
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < len(header) or not cells[ci] or not cells[ti]:
            continue
        rec = {"company": cells[ci], "title": cells[ti]}
        if di is not None and di < len(cells):
            rec["date_applied"] = normalize_date(cells[di], default_year)
        if si is not None and si < len(cells):
            rec["status"] = cells[si]
        out.append(rec)
    return out


# ---------------------------------------------------------------- matching

def _candidates(jobs: list[JobRecord], company: str, title: str | None):
    cc = canonicalize_company(company)
    same_company = [j for j in jobs if j.company_canonical == cc]

    if title is None:
        # The page names a company and no role. Matching on company alone is a
        # guess whenever that company has more than one tracked job, so it is
        # only allowed to resolve when there is exactly one candidate.
        if len(same_company) == 1:
            return "company_only", [(same_company[0], 0.8)]
        return "company_only", [(j, 0.5) for j in same_company]

    tc = canonicalize_title(title)
    exact = [j for j in same_company if j.title_canonical == tc]
    if exact:
        return "exact", [(exact[0], 1.0)]
    scored = sorted(
        ((j, title_similarity(tc, j.title_canonical)) for j in same_company),
        key=lambda p: -p[1],
    )
    return "fuzzy", scored


def match_page(page: PageRecord, jobs: list[JobRecord],
               links: list[LinkRecord], min_confidence: float,
               default_year: int = 2026) -> dict:
    """Run one page down the ladder. First hit wins."""
    by_id = {j.id: j for j in jobs}

    # Tier 1: a human said so. Outranks the marker, which can go stale when a
    # page is duplicated as a template and edited to describe a different role.
    for ln in links:
        if ln.page_id == page.page_id and ln.match_kind == "manual":
            if ln.job_id in by_id:
                return {"kind": "matched", "job_id": ln.job_id, "match_kind": "manual",
                        "confidence": 1.0, "date_applied": ln.date_applied}

    # Tier 2: a marker this server wrote.
    marker = find_marker(page.text)
    if marker and marker in by_id:
        return {"kind": "matched", "job_id": marker, "match_kind": "marker",
                "confidence": 1.0, "date_applied": None}

    # Tiers 3-5 need something to match ON. A running-log table if the page has
    # one; otherwise the page title itself, which is where this notebook keeps
    # "Company - Role" and the page's creation date is when the note was made.
    records = extract_table_rows(page.text, default_year)
    if not records:
        from_title = parse_page_title(page.title)
        if from_title:
            # "Applied:" beats "Date:" beats the page's creation date. The last
            # is when the note was written, which is only a proxy for when the
            # application happened -- so say which one was used.
            found, label = extract_applied_date(page.text, default_year)
            from_title["date_applied"] = found or (
                page.created_dt[:10] if page.created_dt else None)
            from_title["date_source"] = label or "page_created"
            from_title["source"] = "page_title"
            records = [from_title]
    if not records:
        return {"kind": "unparsed", "reason": "no application record found"}

    results = []
    for rec in records:
        kind, scored = _candidates(jobs, rec["company"], rec.get("title"))
        if kind == "exact":
            job, conf = scored[0]
            results.append({"kind": "matched", "job_id": job.id, "match_kind": "exact",
                            "confidence": 1.0, "extracted": rec,
                            "date_applied": rec.get("date_applied")})
            continue

        if kind == "company_only":
            if len(scored) == 1:
                job, conf = scored[0]
                results.append({"kind": "matched", "job_id": job.id, "match_kind": "fuzzy",
                                "confidence": conf, "extracted": rec,
                                "date_applied": rec.get("date_applied")})
            elif scored:
                results.append({"kind": "ambiguous", "extracted": rec,
                                "candidates": [{"job_id": j.id, "title": j.title,
                                                "confidence": s} for j, s in scored]})
            else:
                results.append({"kind": "unmatched", "extracted": rec})
            continue

        strong = [(j, s) for j, s in scored if s >= min_confidence]
        if len(strong) == 1:
            job, conf = strong[0]
            results.append({"kind": "matched", "job_id": job.id, "match_kind": "fuzzy",
                            "confidence": round(conf, 2), "extracted": rec,
                            "date_applied": rec.get("date_applied")})
        elif len(strong) > 1:
            results.append({"kind": "ambiguous", "extracted": rec,
                            "candidates": [{"job_id": j.id, "title": j.title,
                                            "confidence": round(s, 2)}
                                           for j, s in strong]})
        else:
            near = [(j, s) for j, s in scored if s >= AMBIGUOUS_FLOOR]
            if near:
                results.append({"kind": "ambiguous", "extracted": rec,
                                "candidates": [{"job_id": j.id, "title": j.title,
                                                "confidence": round(s, 2)}
                                               for j, s in near]})
            else:
                results.append({"kind": "unmatched", "extracted": rec})
    return {"kind": "multi", "results": results}


def match_pages(pages: list[PageRecord], jobs: list[JobRecord],
                links: list[LinkRecord], min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                default_year: int = 2026) -> Reconciliation:
    """`jobs` must contain only live rows -- merged jobs are hidden and would
    produce a proposed_action that mark_applied then refuses."""
    rec = Reconciliation()
    by_id = {j.id: j for j in jobs}
    seen_jobs: set[str] = set()

    for page in pages:
        if page.is_ignored:
            continue
        out = match_page(page, jobs, links, min_confidence, default_year)
        entries = out["results"] if out["kind"] == "multi" else [out]

        parsed_anything = False
        for e in entries:
            if e["kind"] == "matched":
                parsed_anything = True
                job = by_id[e["job_id"]]
                seen_jobs.add(job.id)
                ex = e.get("extracted") or {}
                entry = {"page_id": page.page_id, "page_title": page.title,
                         "job_id": job.id, "match_kind": e["match_kind"],
                         "confidence": e["confidence"],
                         "extracted": e.get("extracted"),
                         "db_status": job.status}
                # A leading "x" on the page title means Randy was turned down.
                # Proposing mark_applied for those would be exactly backwards.
                if ex.get("rejected"):
                    if job.status != "rejected":
                        rec.onenote_only.append({
                            **entry, "rejected": True,
                            "date_applied": e.get("date_applied"),
                            "proposed_action": f"reject_job(id={job.id!r})",
                        })
                    else:
                        rec.matched.append({**entry, "agrees": True})
                elif job.status == "applied":
                    rec.matched.append({**entry, "agrees": True})
                else:
                    date = e.get("date_applied")
                    rec.onenote_only.append({
                        **entry,
                        "date_applied": date,
                        "date_source": ex.get("date_source"),
                        "proposed_action": (
                            f"mark_applied(id={job.id!r}"
                            + (f", date_applied={date!r})" if date else ")")
                        ),
                    })
            elif e["kind"] == "ambiguous":
                parsed_anything = True
                rec.ambiguous.append({
                    "page_id": page.page_id, "extracted": e["extracted"],
                    "candidates": e["candidates"],
                    "proposed_action": (
                        f"onenote_link_page(page_id={page.page_id!r}, job_id=<pick one>)"
                    ),
                })
            elif e["kind"] == "unmatched":
                parsed_anything = True
                ex = e["extracted"]
                rec.unmatched.append({
                    "page_id": page.page_id, "page_title": page.title,
                    "extracted": ex,
                    "proposed_action": (
                        f"add_job(company={ex['company']!r}, title={ex['title']!r}, "
                        f"location='Unknown') then mark_applied"
                    ),
                })

        if not parsed_anything:
            rec.unparsed_pages.append({
                "page_id": page.page_id, "title": page.title,
                "reason": out.get("reason", "no application record matched a tracked job"),
                "proposed_action": (
                    "onenote_get_page to read it, then onenote_link_page "
                    "(or onenote_ignore_page if it is not an application)"
                ),
            })

    for job in jobs:
        if job.status == "applied" and job.id not in seen_jobs:
            rec.db_only.append({
                "job_id": job.id, "company": job.company, "title": job.title,
                "date_applied": job.date_applied, "status": job.status,
                "proposed_action": f"onenote_create_page(job_id={job.id!r})",
            })

    return rec
