"""Enums, the status transition table, and natural-key normalization.

Everything here is pure: no database, no I/O. The functions in the
"natural key" section are part of the persisted primary key -- see the
warning above canonicalize_location before changing any of them.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from enum import StrEnum


class Status(StrEnum):
    NEW = "new"
    APPLIED = "applied"
    REJECTED = "rejected"
    EXPIRED = "expired"


class PatternType(StrEnum):
    COMPANY = "company"
    TITLE_KEYWORD = "title_keyword"
    SOURCE = "source"


STATUSES = tuple(s.value for s in Status)
PATTERN_TYPES = tuple(p.value for p in PatternType)

# Authoritative. update_job and mark_applied are the only consumers.
# Self-transitions are permitted so that retries are idempotent.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "new": {"new", "applied", "rejected", "expired"},
    "applied": {"applied", "rejected", "expired"},
    "rejected": {"rejected", "expired", "new"},
    "expired": {"expired", "new", "applied"},
}


JOB_FIELDS = (
    "id", "company", "company_canonical", "title", "title_canonical",
    "location", "location_canonical", "url", "source",
    "date_seen", "last_seen", "times_seen", "date_applied",
    "status", "fit_score", "notes", "email_subject", "merged_into",
    "created_at", "updated_at",
)


def row_to_job(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in JOB_FIELDS}


# --------------------------------------------------------------------------
# Natural key
#
# WARNING: normalize_key_part, canonicalize_location, and the join format in
# generate_job_id together define every stored `id`. Changing any of them
# orphans existing rows. Bump SCHEMA_VERSION and write a migration that
# recomputes ids and merges newly-colliding rows. This is a data migration
# every time -- do not "just tweak the token list".
# --------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_SEP = re.compile(r"[,/|()\[\]\-–—:;]+")


def normalize_key_part(value: str | None) -> str:
    """Lowercase, collapse internal whitespace, strip the ends.

    Corporate suffixes (Inc, LLC) are deliberately NOT stripped: doing so
    would collide distinct entities. Tolerates None -- values arrive from an
    LLM and a missing location must not raise.
    """
    if value is None:
        return ""
    return _WS.sub(" ", value).strip().lower()


_UNKNOWN = frozenset({"", "unknown", "n a", "na", "not specified", "unspecified", "tbd"})

_REMOTE_PHRASES = ("work from home", "working from home", "work at home")

# The role is NOT fully remote even though the string says "remote",
# e.g. "Hybrid - Remote 2 days/week".
_NOT_REMOTE = ("hybrid", "onsite", "on site", "in office", "in person")

_REMOTE_TOKENS = frozenset({
    "remote", "anywhere", "wfh", "telecommute", "telecommuting",
    "virtual", "distributed",
})

# Carry no geographic information once "remote" is established. Note that
# "emea", "canada", "apac" are deliberately absent: geography-restricted
# remote is a different job from open remote.
_REMOTE_QUALIFIERS = frozenset({
    "us", "usa", "u.s.", "u.s.a.", "united", "states", "america",
    "based", "only", "fully", "full", "time", "100%", "nationwide", "national",
    "work", "from", "home", "in", "the",
})

# Dropped from the tail of non-remote locations so that "Austin, TX, USA"
# and "Austin, TX" collide.
_COUNTRY_TOKENS = frozenset({"us", "usa", "u.s.", "u.s.a.", "united", "states", "america"})


def canonicalize_location(value: str | None) -> str:
    """Collapse the many spellings of a location into one canonical form.

    "remote" for a fully-remote role with no geographic restriction,
    "unknown" for absent/placeholder values, otherwise a normalized token
    string such as "austin tx".
    """
    s = normalize_key_part(value)
    if not s:
        return "unknown"

    s = _WS.sub(" ", _SEP.sub(" ", s)).strip()
    if s in _UNKNOWN:
        return "unknown"

    for phrase in _REMOTE_PHRASES:
        s = s.replace(phrase, "remote")

    if not any(marker in s for marker in _NOT_REMOTE):
        tokens = s.split()
        has_remote = any(t in _REMOTE_TOKENS for t in tokens)
        rest_is_noise = all(
            t in _REMOTE_TOKENS or t in _REMOTE_QUALIFIERS for t in tokens
        )
        if has_remote and rest_is_noise:
            return "remote"

    tokens = s.split()
    while tokens and tokens[-1] in _COUNTRY_TOKENS:
        tokens.pop()
    return " ".join(tokens) or "unknown"


# Legal-entity suffixes, stripped from the tail of a company name so that
# "Acme Corp", "Acme Corporation" and "Acme, Inc." are one employer.
#
# This deliberately merges "Acme Corp" with "Acme LLC". For a personal job
# tracker that is nearly always correct and the information loss is acceptable;
# for anything doing entity resolution across real corporate structures it
# would not be.
_LEGAL_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "l.l.c", "ltd", "limited", "corp",
    "corporation", "co", "company", "gmbh", "plc", "lp", "llp", "pllc",
    "sa", "nv", "bv", "ab", "ag", "as", "oy", "pty", "pbc", "srl", "kk",
    "holdings", "group",
})


def canonicalize_company(value: str | None) -> str:
    """Strip trailing legal-entity suffixes. "Acme, Inc." -> "acme"."""
    s = normalize_key_part(value).replace(",", " ")
    tokens = _WS.sub(" ", s).strip().split()
    while tokens and tokens[-1].strip(".") in _LEGAL_SUFFIXES:
        tokens.pop()
    # A company legitimately named e.g. "Limited" must not normalize to "".
    return " ".join(tokens) or normalize_key_part(value)


# A segment is dropped only when it is unambiguously not part of the role name.
_SALARY = re.compile(r"\$\s*\d")
_PAY_RANGE = re.compile(r"^[\d,.]+\s*k?\s*[-–—]\s*[\d,.]+\s*k?(\s*(usd|eur|gbp))?$")
_REQ_ID = re.compile(r"^(req|job|jr|r)[-\s#]?\d+$")
# Only split on separators surrounded by whitespace: "Full-Stack Engineer" and
# "Front-end Developer" must survive intact.
_TITLE_SPLIT = re.compile(r"\s+[-–—|]\s+")
_BRACKETED = re.compile(r"\[[^\]]*\]")
_PARENTHETICAL = re.compile(r"\(([^)]*)\)")


def _is_noise_segment(seg: str) -> bool:
    s = seg.strip()
    if not s:
        return True
    # Reuse the location logic: "(Remote)", "- US" are geography, not job title.
    if canonicalize_location(s) in ("remote", "unknown"):
        return True
    if _SALARY.search(s):
        return True
    low = s.lower()
    return bool(_PAY_RANGE.match(low) or _REQ_ID.match(low))


def canonicalize_title(value: str | None) -> str:
    """Strip board decorations from a job title.

    Removes bracketed req IDs, parenthesized geography, salary ranges, and
    trailing " - Remote"-style segments.

    Deliberately preserves seniority and specialization: "Senior", "II", and
    ", Platform" all carry meaning, and a normalizer that ate them would merge
    genuinely different requisitions into one row.
    """
    s = normalize_key_part(value)
    if not s:
        return ""

    s = _BRACKETED.sub(" ", s)
    s = _PARENTHETICAL.sub(
        lambda m: " " if _is_noise_segment(m.group(1)) else m.group(0), s
    )

    segments = _TITLE_SPLIT.split(s)
    kept = [segments[0]] + [g for g in segments[1:] if not _is_noise_segment(g)]
    out = _WS.sub(" ", " ".join(kept)).strip(" ,-–—|")
    return out or s.strip()


def generate_job_id(company: str, title: str, location: str | None) -> str:
    raw = "|".join((
        canonicalize_company(company),
        canonicalize_title(title),
        canonicalize_location(location),
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
