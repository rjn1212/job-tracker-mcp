"""Score new jobs by likelihood of a match, not by seniority.

Randy's 27 applications: 12 Manager, 6 Senior Manager, 2 Sr. Manager, 4 Director,
2 Lead, 1 Principal. Manager and Senior Manager are where he lands; Director is a
stretch and VP more so. So the seniority ladder is ordered by HIT RATE, not by
prestige -- a Senior Manager posting is a better bet than a Director posting.

Salary is a graded swing, not a flag. It can move a job a full band, so a
Director role paying $320k outranks a Senior Manager role paying $150k, while a
Senior Manager role paying $320k beats them both.

A NULL salary contributes 0. It is not a low salary; the posting simply did not
say. Penalizing it would bury every LinkedIn job, since 202 of 297 state nothing.

This is a heuristic PRE-score over the fields a row holds -- company, title,
location, salary. It cannot read the job description, so it is a judgment about
a title. Override any individual job with update_job(id, fit_score=N).
"""
import re
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from job_tracker_mcp.database import Database

# Staffing firms, recruiters, and job boards that list under their own name.
# The role may be real; the `company` field is not the employer.
AGENCY = {
    "randstad digital americas", "edison smart®", "edison smart", "saragossa",
    "gforce life sciences", "ladders", "nexwave", "mitr", "robert half",
    "motion recruitment", "jobot", "cybercoders", "insight global", "teksystems",
    "apex systems", "aaa global", "harnham", "selby jennings", "storm4", "hays",
    "michael page", "phaidon international",
}

# Individual-contributor roles. Anchored with word boundaries: "lead engineer"
# as a bare substring matches inside "Lead Engineering Senior Manager", which
# demoted two senior-manager roles to 3 the first time this ran.
IC = re.compile(
    r"\b(software developer|staff engineer|principal engineer|senior software engineer"
    r"|developer [34]|site reliability developer|full stack engineer|linux engineer)\b")

NON_ENG = re.compile(
    r"\b(product manager|product line manager|sales|category management"
    r"|presentation design|program management|project manager|recruiter|marketing"
    r"|quantitative finance|tax|audit)\b")

# Widened deliberately: real leadership titles often never say "engineering".
# "AVP, Enterprise Authentication", "Mgr Applications", "US IT Leader".
ENG = re.compile(
    r"(software|engineer|technolog|platform|infrastructure|devops|cloud|architect"
    r"|technical|application|system|\bit\b|information technology|authentication"
    r"|directory services|\bdata\b|security|\bsre\b|reliability|development"
    r"|developer|digital|\bai\b|machine learning|observability|integration"
    r"|\bcto\b|\bcio\b|\bsre\b)")

SUBURBS = ("oak brook", "deerfield", "lake forest", "northbrook", "buffalo grove",
           "rolling meadows", "burr ridge", "lemont", "bannockburn", "rosemont")


def seniority(title: str) -> int:
    """Base score = how likely Randy is to match, per his own application history."""
    t = title.lower()
    if re.search(r"\b(sr\.?|senior) manager\b", t):                    return 9   # his level
    if re.search(r"\b(engineering manager|manager, (software|engineering))\b", t): return 8
    if re.search(r"\bdirector\b", t):                                  return 7   # a stretch
    if re.search(r"\b(manager|mgr)\b", t):                             return 7
    if re.search(r"\bhead of\b", t):                                   return 6
    if re.search(r"\b(avp|assistant vice president)\b", t):            return 6
    if re.search(r"\b(vp|vice president)\b", t):                       return 5
    if re.search(r"\b(lead|leader)\b", t):                             return 5
    if re.search(r"\b(svp|cto|chief)\b", t):                           return 3   # far reach
    return 4


def salary_swing(annual_max: int | None) -> float:
    """Graded, and able to reorder within a seniority band."""
    if not annual_max:
        return 0.0            # not stated. NOT low.
    if annual_max >= 300_000:
        return 2.0
    if annual_max >= 250_000:
        return 1.5
    if annual_max >= 200_000:
        return 1.0
    if annual_max >= 150_000:
        return 0.0
    return -1.5


def score(company: str, title: str, location: str, annual_max: int | None) -> int:
    c, t, l = company.lower(), (title or "").lower(), (location or "").lower()
    s = float(seniority(t))

    if not ENG.search(t):
        s -= 3
    if IC.search(t) or NON_ENG.search(t):
        s = min(s, 3)
    if c in AGENCY:
        s -= 3

    remote = "remote" in l
    chicago = ("chicago" in l or re.search(r"\bil\b", l) or "illinois" in l
               or "multiple locations" in l or "united states" in l)
    if not (remote or chicago or l in ("", "unknown")):
        s -= 3                                   # Brookfield WI, and the like
    elif any(x in l for x in SUBURBS):
        s -= 1                                   # a commute, not a dealbreaker

    s += salary_swing(annual_max)
    return max(1, min(10, int(round(s))))


def main() -> None:
    db = Database()
    rows = db.conn.execute(
        "SELECT id, company, title, location, salary_annual_max, fit_score FROM jobs "
        "WHERE status = 'new' AND merged_into IS NULL").fetchall()
    changed = 0
    for r in rows:
        s = score(r["company"], r["title"], r["location"], r["salary_annual_max"])
        if s != r["fit_score"]:
            changed += 1
        db.update_job(r["id"], fit_score=s)
    print(f"scored {len(rows)} jobs; {changed} changed")
    db.close()


if __name__ == "__main__":
    main()
