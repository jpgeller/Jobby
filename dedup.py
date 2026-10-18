"""Keep one row per application (fuzzy match) and decide how statuses may change (spec Section 3)."""
import re

from rapidfuzz import fuzz

import db

# 90, not 85: at 85 "Southwind Robotics" matches "Northwind Robotics".
MATCH_THRESHOLD = 90
# With only one application at a company, a looser role match is enough
# (emails often shorten the title, e.g. "SWE Intern" vs "Software Engineering Intern, Summer 2027").
SINGLE_CANDIDATE_ROLE_THRESHOLD = 60

_SUFFIXES = r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|the)\b"

STATUS_RANK = {"applied": 0, "assessment": 1, "interview": 2, "offer": 3}
ALWAYS_APPLIES = {"rejected", "offer"}

CATEGORY_TO_STATUS = {
    "application_confirmation": "applied",
    "assessment_invite": "assessment",
    "interview_invite": "interview",
    "interview_reschedule": "interview",
    "rejection": "rejected",
    "offer": "offer",
}


def normalize_company(name):
    text = re.sub(r"[^\w\s]", " ", (name or "").lower())
    text = re.sub(_SUFFIXES, " ", text)
    return re.sub(r"\s+", " ", text).strip()


def applications_at(conn, company):
    norm = normalize_company(company)
    return [a for a in db.list_applications(conn) if fuzz.ratio(a["company_norm"], norm) > MATCH_THRESHOLD]


def find_application(conn, company, role):
    """Return the matching applications row, or None."""
    candidates = applications_at(conn, company)
    if not candidates:
        return None
    if not role:
        return candidates[0] if len(candidates) == 1 else None
    if len(candidates) == 1 and not candidates[0]["role"]:
        return candidates[0]  # role unknown so far (e.g. from a Handshake confirmation): same application

    scored = [(fuzz.token_set_ratio(a["role"].lower(), role.lower()), a) for a in candidates]
    score, best = max(scored, key=lambda pair: pair[0])
    threshold = SINGLE_CANDIDATE_ROLE_THRESHOLD if len(candidates) == 1 else MATCH_THRESHOLD
    return best if score >= threshold else None


def next_status(current, proposed):
    """Status only moves forward, except rejections and offers, which always apply."""
    if proposed is None or proposed == current:
        return current
    if proposed in ALWAYS_APPLIES:
        return proposed
    if current in STATUS_RANK and proposed in STATUS_RANK and STATUS_RANK[proposed] > STATUS_RANK[current]:
        return proposed
    return current
