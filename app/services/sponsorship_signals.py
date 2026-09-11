"""
Pure regex/string sponsorship-signal extraction from job description
text. No LLM, no external call, no cost -- runs in-process at intake
(see intake_service.py's _ingest_raw_posting) so every posting gets
these flags for free, before match scoring ever happens.

Distinct from JobApplication.visa_sponsorship -- an LLM-derived
classification computed later in matching_service.evaluate_match(),
and only when an application is actually scored (a real, on-demand
LLM cost, per matching_service.py's own docstring: "Scoring is
deliberately NOT run automatically on every posting"). This module is
the free, always-on signal the hard gate (queue_service.py) and the
mechanical score components (scoring_service.py) both use; the LLM's
visa_sponsorship stays a secondary, higher-quality confirming signal
used only when it happens to already be available.

Deliberately narrow, word-boundary-anchored patterns -- the bare word
"sponsorship" is too broad on its own (a JD's "manage our conference
sponsorship program" or "sponsorship opportunities for partners"
section has nothing to do with visas), so every pattern below requires
real visa/work-authorization context around the word, never the bare
word alone. Same design posture as intake_service.py's own
_ELIGIBILITY_PATTERNS: mechanical, surfaced, never a silent filter by
itself -- the hard gate is a separate, explicit decision layered on
top in queue_service.py, not something this module does.
"""

import re

# Each entry blocks the posting when matched -- these are JD statements
# that a candidate needing sponsorship should never even see in an
# active queue. "Wins" over a positive signal at the GATING layer
# (queue_service.py), not here -- both sponsorship_blocked and
# sponsorship_signal are independent booleans, see detect_sponsorship_signals.
_NEGATIVE_PATTERNS = [
    (r"\bno visa sponsorship\b", "no visa sponsorship"),
    (r"\bnot able to sponsor\b", "not able to sponsor"),
    (r"\bunable to sponsor\b", "unable to sponsor"),
    (r"\bdoes not sponsor\b", "does not sponsor"),
    (r"\bdo not sponsor\b", "do not sponsor"),
    (r"\bwithout sponsorship\b", "without sponsorship"),
    (
        r"\bmust be authorized to work in the (u\.?s\.?|united states) without\b",
        "must be authorized to work without sponsorship",
    ),
    (r"\bu\.?s\.?\s*citizens? only\b", "US citizens only"),
    (r"\bcitizenship required\b", "citizenship required"),
    (r"\bactive security clearance\b", "active security clearance"),
    (r"\bitar\b", "ITAR"),
    (r"\bexport control(led)?\b", "export control"),
    (r"\bgreen card holder\b", "green card holder required"),
    (r"\bpermanent residents? only\b", "permanent residents only"),
    (r"\bc2c\b", "C2C"),
    (r"\bcorp[- ]to[- ]corp\b", "corp-to-corp"),
    (r"\bw2 only,? no sponsorship\b", "W2 only, no sponsorship"),
]

# A real, explicit positive statement about sponsorship -- not just any
# mention of a visa-adjacent acronym; each requires the acronym/phrase
# to appear in a context that plausibly means "we do this here."
_POSITIVE_PATTERNS = [
    (r"\bvisa sponsorship available\b", "visa sponsorship available"),
    (r"\bwill sponsor\b", "will sponsor"),
    (r"\bwilling to sponsor\b", "willing to sponsor"),
    (r"\bh-?1b\b", "H-1B mentioned"),
    (r"\bsponsorship provided\b", "sponsorship provided"),
    (r"\bcap[- ]exempt\b", "cap-exempt"),
    (r"\bopt\b", "OPT"),
    (r"\bcpt\b", "CPT"),
    (r"\bstem opt\b", "STEM OPT"),
]

_WORKSITE_AMBIGUOUS_PATTERNS = [
    (r"\bwork from anywhere\b", "work from anywhere"),
    (r"\bfully distributed\b", "fully distributed"),
    (r"\banywhere in the world\b", "anywhere in the world"),
    (r"\bany\s?time\s?zone\b", "any timezone"),
    (r"\bglobally distributed\b", "globally distributed"),
    (r"\bremote\s*\((anywhere|worldwide|global)\)", "remote (anywhere/worldwide/global)"),
    (r"\bremote[,:]\s*(anywhere|worldwide|global)\b", "remote, anywhere/worldwide/global"),
]


def detect_sponsorship_signals(jd_text: str) -> dict:
    """Returns {"sponsorship_blocked": bool, "sponsorship_signal": bool,
    "worksite_ambiguous": bool, "signal_matches": {"blocked": [...],
    "signal": [...], "worksite_ambiguous": [...]}}. Each boolean
    reflects only its own pattern list, independently -- a JD that
    trips both a negative and a positive pattern gets both booleans
    set True, both hit lists recorded; deciding which one "wins" for
    gating purposes is queue_service.py's job, not this pure function's."""
    empty_matches = {"blocked": [], "signal": [], "worksite_ambiguous": []}
    if not jd_text:
        return {
            "sponsorship_blocked": False,
            "sponsorship_signal": False,
            "worksite_ambiguous": False,
            "signal_matches": empty_matches,
        }

    lower = jd_text.lower()
    blocked_hits = [label for pattern, label in _NEGATIVE_PATTERNS if re.search(pattern, lower)]
    signal_hits = [label for pattern, label in _POSITIVE_PATTERNS if re.search(pattern, lower)]
    ambiguous_hits = [label for pattern, label in _WORKSITE_AMBIGUOUS_PATTERNS if re.search(pattern, lower)]

    return {
        "sponsorship_blocked": bool(blocked_hits),
        "sponsorship_signal": bool(signal_hits),
        "worksite_ambiguous": bool(ambiguous_hits),
        "signal_matches": {"blocked": blocked_hits, "signal": signal_hits, "worksite_ambiguous": ambiguous_hits},
    }
