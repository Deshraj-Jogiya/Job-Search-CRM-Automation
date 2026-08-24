"""
Living profile. Manages ProfileVariant/ProfileVersion records --
creating variants, seeding/updating their content from a portfolio-hosted
resume.json, or diffing in a pasted LinkedIn export via the LLM.

Nothing here auto-activates an AI-proposed change: portfolio syncs replace
the active version directly (it's the user's own site, zero-risk), but
LinkedIn paste-diffs are created as a pending (is_active=False) version
that a human has to explicitly approve or reject -- see approve_version()
/ reject_version().
"""

import os
import json
import requests

from sqlalchemy.orm import Session

from ..models import ProfileVariant, ProfileVersion
from .llm import get_llm_provider, parse_json_response
from .activity_logger import log_activity



class ProfileServiceError(Exception):
    """Raised for user-facing failures (bad JSON, unreachable portfolio,
    missing variant) -- callers should catch this and show the message
    rather than letting a 500 through."""


# Real incident this exists to catch: a profile sat for 5 days across 8
# separate manual saves with a completely missing second degree and
# zero certifications -- never flagged, never noticed, because a raw
# JSON textarea gives no signal that a section shrank or vanished.
# These two checks are mechanical (no LLM), same posture as the
# tailoring fabrication safeguard elsewhere in this app: never trust a
# single unverified pass over structured data:
#   - detect_profile_regressions: compares a NEW profile snapshot
#     against the CURRENTLY ACTIVE one and flags anything that shrank.
#     Applies to any path that replaces the whole profile wholesale
#     (raw JSON paste, portfolio sync, LinkedIn AI-diff) -- the paths
#     where an accidental omission is actually possible.
#   - profile_completeness_warnings: flags sections that are just
#     empty, full stop, independent of any prior version -- catches the
#     case (like the real one) where something was NEVER there, so
#     there was never a "shrink" to detect in the first place.
_SECTION_LABELS = {
    "experience": "work experience entries",
    "projects": "projects",
    "education": "education entries",
    "certifications": "certifications",
}


def _bullet_total(entries) -> int:
    """Sum of bullets across every experience/project entry. Entry-count
    alone can't catch the failure mode that actually matters for
    matching/tailoring quality (see match_score's own history of only
    seeing titles+skills, not bullets) -- an entry can survive a paste
    with its role/company intact but its bullets wiped, which a plain
    len(experience) comparison would never notice."""
    if not isinstance(entries, list):
        return 0
    total = 0
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("bullets"), list):
            total += len(entry["bullets"])
    return total


def _section_counts(content: dict) -> dict[str, int]:
    counts = {}
    for key in _SECTION_LABELS:
        value = content.get(key)
        counts[key] = len(value) if isinstance(value, list) else 0
    skills = content.get("skills")
    if isinstance(skills, dict):
        counts["skills"] = sum(len(v) for v in skills.values() if isinstance(v, list))
    elif isinstance(skills, list):
        counts["skills"] = len(skills)
    else:
        counts["skills"] = 0
    counts["experience_bullets"] = _bullet_total(content.get("experience"))
    counts["projects_bullets"] = _bullet_total(content.get("projects"))
    return counts


_ALL_COUNT_LABELS = {
    **_SECTION_LABELS,
    "skills": "skills",
    "experience_bullets": "total experience bullets",
    "projects_bullets": "total project bullets",
}


def detect_profile_regressions(old_content: dict, new_content: dict) -> list[str]:
    """Plain-English warnings for any section that shrank going from
    old_content to new_content. Empty list means nothing looks wrong."""
    old_counts = _section_counts(old_content)
    new_counts = _section_counts(new_content)
    warnings = []
    for key, label in _ALL_COUNT_LABELS.items():
        old_n, new_n = old_counts.get(key, 0), new_counts.get(key, 0)
        if new_n < old_n:
            warnings.append(f"{label}: {old_n} -> {new_n}")
    return warnings


def profile_completeness_warnings(content: dict) -> list[str]:
    """Flags sections that are just empty, independent of history --
    catches a gap that was never there to begin with, which a
    shrink-only check can't. Deliberately narrow (only the sections
    genuinely worth flagging by default) to avoid nagging over things
    that are legitimately fine to leave blank for many candidates --
    certifications/projects can be genuinely absent for a real
    candidate, but a listed experience/project entry with zero bullets
    is never intentional, it's a paste that lost its content mid-entry,
    and match_score/tailoring only ever look at bullets, never bare
    role/company/project names -- an entry stripped down to just a
    title is invisible to scoring even though it looks present here."""
    counts = _section_counts(content)
    warnings = []
    if counts["certifications"] == 0:
        warnings.append("No certifications listed -- if you have any, add them on the Profile page.")
    if counts["education"] == 0:
        warnings.append("No education listed -- add at least one degree on the Profile page.")
    if counts["experience"] == 0:
        warnings.append("No work experience listed yet.")
    if counts["skills"] == 0:
        warnings.append("No skills listed -- scoring and tailoring both rely on this being filled in.")

    experience = content.get("experience")
    if isinstance(experience, list):
        empty_roles = [
            e.get("role") or e.get("company") or "an experience entry"
            for e in experience
            if isinstance(e, dict) and not e.get("bullets")
        ]
        if empty_roles:
            warnings.append(
                f"{len(empty_roles)} experience entry has no bullets listed ({', '.join(empty_roles)})"
                if len(empty_roles) == 1
                else f"{len(empty_roles)} experience entries have no bullets listed ({', '.join(empty_roles)})"
            )

    projects = content.get("projects")
    if isinstance(projects, list):
        empty_projects = [
            p.get("name") or "a project"
            for p in projects
            if isinstance(p, dict) and not p.get("bullets")
        ]
        if empty_projects:
            warnings.append(
                f"{len(empty_projects)} project has no bullets listed ({', '.join(empty_projects)})"
                if len(empty_projects) == 1
                else f"{len(empty_projects)} projects have no bullets listed ({', '.join(empty_projects)})"
            )

    return warnings


def get_default_profile_content(db: Session) -> dict | None:
    """The default variant's active profile content, or None if there
    isn't one yet -- for callers outside an application context (e.g.
    intake targeting) that need a graceful "nothing to work with yet"
    rather than an exception."""
    variant = db.query(ProfileVariant).filter(ProfileVariant.is_default == True).first()  # noqa: E712
    if not variant:
        return None
    version = (
        db.query(ProfileVersion)
        .filter(ProfileVersion.variant_id == variant.id, ProfileVersion.is_active == True)  # noqa: E712
        .first()
    )
    if not version:
        return None
    return json.loads(version.content_json)


def _get_variant_or_raise(db: Session, variant_id: int) -> ProfileVariant:
    variant = db.query(ProfileVariant).filter(ProfileVariant.id == variant_id).first()
    if not variant:
        raise ProfileServiceError(f"Profile variant {variant_id} not found.")
    return variant


def get_active_version(db: Session, variant_id: int) -> ProfileVersion | None:
    return (
        db.query(ProfileVersion)
        .filter(ProfileVersion.variant_id == variant_id, ProfileVersion.is_active == True)  # noqa: E712
        .first()
    )


def create_variant(db: Session, name: str, is_default: bool = False) -> ProfileVariant:
    name = name.strip()
    if not name:
        raise ProfileServiceError("Variant name cannot be empty.")
    existing = db.query(ProfileVariant).filter(ProfileVariant.name == name).first()
    if existing:
        raise ProfileServiceError(f"A variant named '{name}' already exists.")

    if is_default:
        db.query(ProfileVariant).update({ProfileVariant.is_default: False})

    variant = ProfileVariant(name=name, is_default=is_default)
    db.add(variant)
    db.commit()
    db.refresh(variant)
    log_activity(db, f"Created profile variant '{name}'.")
    return variant


def set_default_variant(db: Session, variant_id: int) -> ProfileVariant:
    variant = _get_variant_or_raise(db, variant_id)
    db.query(ProfileVariant).update({ProfileVariant.is_default: False})
    variant.is_default = True
    db.commit()
    log_activity(db, f"Set '{variant.name}' as the default profile variant.")
    return variant


def delete_variant(db: Session, variant_id: int) -> None:
    variant = _get_variant_or_raise(db, variant_id)
    name = variant.name
    db.delete(variant)  # cascades to ProfileVersion rows
    db.commit()
    log_activity(db, f"Deleted profile variant '{name}'.")


def _deactivate_siblings(db: Session, variant_id: int) -> None:
    db.query(ProfileVersion).filter(
        ProfileVersion.variant_id == variant_id, ProfileVersion.is_active == True  # noqa: E712
    ).update({ProfileVersion.is_active: False})


def create_manual_version(
    db: Session, variant_id: int, content_json_text: str, check_for_regressions: bool = True
) -> tuple[ProfileVersion, list[str]]:
    """Bootstrap or hand-edit a variant's profile content by pasting raw
    JSON directly. This is the only way to get a first version into a
    variant before either sync source (portfolio, LinkedIn) has anything
    to work from.

    Returns (version, regression_warnings) -- check_for_regressions=False
    for callers doing a narrow, deliberate merge (a single add/remove
    through update_structured_fields) where a size change is exactly
    what the user's own click asked for, not something to flag. Never
    blocks on a regression -- a raw JSON paste that shrinks something is
    still saved (rejecting it would silently discard whatever the user
    just typed), but the warning is returned so the caller can surface
    it loudly instead of the silence that let this go unnoticed for 5
    days across 8 saves in the real incident this exists to catch."""
    variant = _get_variant_or_raise(db, variant_id)
    try:
        parsed = json.loads(content_json_text)
    except json.JSONDecodeError as e:
        raise ProfileServiceError(f"That isn't valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ProfileServiceError("Profile content must be a JSON object.")

    warnings = []
    if check_for_regressions:
        previous = get_active_version(db, variant_id)
        if previous:
            warnings = detect_profile_regressions(json.loads(previous.content_json), parsed)

    _deactivate_siblings(db, variant_id)
    version = ProfileVersion(
        variant_id=variant.id,
        content_json=json.dumps(parsed, indent=2),
        source="manual",
        change_summary="Manually edited/seeded.",
        is_active=True,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    if warnings:
        log_activity(
            db,
            f"Manual profile edit for '{variant.name}' shrank: {'; '.join(warnings)}. "
            "If unintentional, review and fix on the Profile page.",
            "WARNING",
        )
    else:
        log_activity(db, f"Saved manual profile edit for variant '{variant.name}'.")
    return version, warnings


def update_structured_fields(db: Session, variant_id: int, updates: dict) -> ProfileVersion:
    """Merges top-level keys (e.g. {"eeo": {...}, "application_preferences":
    {...}}) into the variant's current active content, preserving
    everything else (experience, projects, etc.) untouched, and saves the
    result as a new active version -- same versioning mechanism as
    create_manual_version(), just merge-based so the Profile page's
    structured preferences/education/certifications forms don't need the
    user to paste the whole profile JSON back in just to change a few
    fields. Skips the regression check -- a narrow, deliberate merge
    (e.g. removing one certification the user just clicked "Remove" on)
    is exactly what was asked for, not a size change worth flagging."""
    variant = _get_variant_or_raise(db, variant_id)
    version = get_active_version(db, variant_id)
    content = json.loads(version.content_json) if version else {}
    content.update(updates)
    version, _warnings = create_manual_version(
        db, variant.id, json.dumps(content, indent=2), check_for_regressions=False
    )
    return version


def add_education_entry(db: Session, variant_id: int, degree: str, school: str, date: str) -> ProfileVersion:
    """Structured alternative to raw JSON paste for the exact section a
    real profile went 5 days and 8 saves with a missing degree in,
    completely unnoticed. See document_render_service.render_resume_pdf
    for the {degree, school, date} shape this must match."""
    degree, school, date = degree.strip(), school.strip(), date.strip()
    if not degree or not school:
        raise ProfileServiceError("Degree and school are both required.")
    version = get_active_version(db, variant_id)
    content = json.loads(version.content_json) if version else {}
    education = list(content.get("education") or [])
    education.append({"degree": degree, "school": school, "date": date})
    return update_structured_fields(db, variant_id, {"education": education})


def remove_education_entry(db: Session, variant_id: int, index: int) -> ProfileVersion:
    version = get_active_version(db, variant_id)
    content = json.loads(version.content_json) if version else {}
    education = list(content.get("education") or [])
    if index < 0 or index >= len(education):
        raise ProfileServiceError("That education entry no longer exists -- refresh and try again.")
    education.pop(index)
    return update_structured_fields(db, variant_id, {"education": education})


def add_certification(db: Session, variant_id: int, text: str) -> ProfileVersion:
    text = text.strip()
    if not text:
        raise ProfileServiceError("Certification text cannot be empty.")
    version = get_active_version(db, variant_id)
    content = json.loads(version.content_json) if version else {}
    certifications = list(content.get("certifications") or [])
    certifications.append(text)
    return update_structured_fields(db, variant_id, {"certifications": certifications})


def remove_certification(db: Session, variant_id: int, index: int) -> ProfileVersion:
    version = get_active_version(db, variant_id)
    content = json.loads(version.content_json) if version else {}
    certifications = list(content.get("certifications") or [])
    if index < 0 or index >= len(certifications):
        raise ProfileServiceError("That certification no longer exists -- refresh and try again.")
    certifications.pop(index)
    return update_structured_fields(db, variant_id, {"certifications": certifications})


def _summarize_diff(old_content: dict, new_content: dict) -> str:
    """Ask the LLM for a short human-readable changelog between two
    profile JSON snapshots. Best-effort -- falls back to a generic note
    if the LLM call fails, since this is just a changelog label, not
    data that needs to be correct to be useful."""
    try:
        llm = get_llm_provider()
        summary = llm.complete_text(
            system="You write terse, factual changelog summaries for a candidate's resume/profile data. No fluff.",
            prompt=(
                "Compare these two versions of a candidate profile JSON and summarize what changed "
                "in 1-3 short bullet points (e.g. 'Added Acme Corp role', 'Updated skills list'). "
                "Only mention actual differences.\n\n"
                f"OLD:\n{json.dumps(old_content, indent=2)}\n\n"
                f"NEW:\n{json.dumps(new_content, indent=2)}"
            ),
            temperature=0.2,
            max_tokens=300,
        )
        return summary.strip()
    except Exception as e:
        return f"Profile updated (change summary unavailable: {e})"


def sync_from_portfolio(db: Session, variant_id: int) -> ProfileVersion:
    """Fetch the portfolio-hosted resume.json and make it the active
    version. This is the zero-risk sync source -- it's the user's own
    site -- so unlike LinkedIn paste-diff, it activates immediately
    rather than sitting in a pending/approval state."""
    variant = _get_variant_or_raise(db, variant_id)
    url = os.getenv("PORTFOLIO_RESUME_URL")
    if not url:
        raise ProfileServiceError(
            "Portfolio sync isn't set up yet -- add your resume.json URL to your environment "
            "settings to enable this, or use the manual JSON paste or LinkedIn import options instead."
        )

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        log_activity(db, f"Portfolio sync failed for '{variant.name}': {e}", "ERROR")
        raise ProfileServiceError(
            f"Could not fetch {url}: {e}. The portfolio site may not have a resume.json endpoint yet."
        ) from e

    try:
        new_content = response.json()
    except ValueError as e:
        raise ProfileServiceError(f"{url} did not return valid JSON: {e}") from e
    if not isinstance(new_content, dict):
        raise ProfileServiceError(f"{url} must return a JSON object.")

    previous = get_active_version(db, variant_id)
    if previous:
        old_content = json.loads(previous.content_json)
        regressions = detect_profile_regressions(old_content, new_content)
        if regressions:
            raise ProfileServiceError(
                "Portfolio sync would shrink: " + "; ".join(regressions) + ". "
                "This activates immediately with no review step, so it's blocked rather than silently "
                "losing data -- update your portfolio's resume.json first, or use manual edit if this is intentional."
            )
        change_summary = _summarize_diff(old_content, new_content)
    else:
        change_summary = "Initial profile import from portfolio sync."

    _deactivate_siblings(db, variant_id)
    version = ProfileVersion(
        variant_id=variant.id,
        content_json=json.dumps(new_content, indent=2),
        source="portfolio_sync",
        change_summary=change_summary,
        is_active=True,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    log_activity(db, f"Synced profile variant '{variant.name}' from portfolio ({url}).")
    return version


def propose_linkedin_diff(db: Session, variant_id: int, pasted_text: str) -> ProfileVersion:
    """Diff a pasted LinkedIn profile export against the variant's
    current active content via the LLM, and store the proposed result as
    a PENDING version (is_active=False). Never auto-activates -- the user
    must call approve_version() first."""
    variant = _get_variant_or_raise(db, variant_id)
    pasted_text = pasted_text.strip()
    if not pasted_text:
        raise ProfileServiceError("Pasted LinkedIn text is empty.")

    previous = get_active_version(db, variant_id)
    existing_content = json.loads(previous.content_json) if previous else {}

    try:
        llm = get_llm_provider()
        raw = llm.complete_json(
            system=(
                "You maintain a candidate's structured resume/profile JSON. You merge new information from "
                "a pasted LinkedIn profile export into the existing profile JSON. Never fabricate anything -- "
                "only incorporate details explicitly present in the pasted text or already in the existing "
                "profile. Preserve any existing field the paste doesn't mention."
            ),
            prompt=(
                "Existing profile JSON (may be empty if this is a first import):\n"
                f"{json.dumps(existing_content, indent=2)}\n\n"
                "Pasted LinkedIn export text:\n"
                f"{pasted_text}\n\n"
                "Respond in EXACTLY this JSON shape:\n"
                "{\n"
                '  "updated_profile": { ...merged profile object, same shape as the existing profile... },\n'
                '  "change_summary": "1-3 short bullet points describing what changed"\n'
                "}\n"
                "Do not wrap the output in markdown code fences."
            ),
            temperature=0.2,
        )
    except Exception as e:
        raise ProfileServiceError(
            f"AI diff request failed: {e}. Check your LLM_PROVIDER config in .env."
        ) from e

    try:
        parsed = parse_json_response(raw)
        updated_profile = parsed["updated_profile"]
        change_summary = parsed.get("change_summary", "LinkedIn import merged.")
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise ProfileServiceError(f"AI diff response was not in the expected format: {e}") from e
    if not isinstance(updated_profile, dict):
        raise ProfileServiceError("AI diff response's updated_profile must be a JSON object.")

    version = ProfileVersion(
        variant_id=variant.id,
        content_json=json.dumps(updated_profile, indent=2),
        source="linkedin_diff",
        change_summary=change_summary,
        is_active=False,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    log_activity(db, f"Proposed LinkedIn profile diff for '{variant.name}' (pending approval).")
    return version


def approve_version(db: Session, version_id: int) -> ProfileVersion:
    version = db.query(ProfileVersion).filter(ProfileVersion.id == version_id).first()
    if not version:
        raise ProfileServiceError(f"Profile version {version_id} not found.")
    _deactivate_siblings(db, version.variant_id)
    version.is_active = True
    db.commit()
    log_activity(db, f"Approved pending profile version {version_id}.")
    return version


def reject_version(db: Session, version_id: int) -> None:
    version = db.query(ProfileVersion).filter(ProfileVersion.id == version_id).first()
    if not version:
        raise ProfileServiceError(f"Profile version {version_id} not found.")
    if version.is_active:
        raise ProfileServiceError("Cannot reject the currently active version.")
    db.delete(version)
    db.commit()
    log_activity(db, f"Rejected pending profile version {version_id}.")
