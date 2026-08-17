"""
Phase 1: living profile. Manages ProfileVariant/ProfileVersion records --
creating variants, seeding/updating their content from a portfolio-hosted
resume.json, or diffing in a pasted LinkedIn export via the LLM.

Nothing here auto-activates an AI-proposed change: portfolio syncs replace
the active version directly (it's the user's own site, zero-risk per
CLAUDE.md), but LinkedIn paste-diffs are created as a pending
(is_active=False) version that a human has to explicitly approve or
reject -- see approve_version() / reject_version().
"""

import os
import json
import requests

from sqlalchemy.orm import Session

from ..models import ProfileVariant, ProfileVersion
from .llm import get_llm_provider, parse_json_response
from .activity_logger import log_activity

DEFAULT_PORTFOLIO_RESUME_URL = "https://deshraj-jogiya.github.io/resume.json"


class ProfileServiceError(Exception):
    """Raised for user-facing failures (bad JSON, unreachable portfolio,
    missing variant) -- callers should catch this and show the message
    rather than letting a 500 through."""


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


def create_manual_version(db: Session, variant_id: int, content_json_text: str) -> ProfileVersion:
    """Bootstrap or hand-edit a variant's profile content by pasting raw
    JSON directly. This is the only way to get a first version into a
    variant before either sync source (portfolio, LinkedIn) has anything
    to work from."""
    variant = _get_variant_or_raise(db, variant_id)
    try:
        parsed = json.loads(content_json_text)
    except json.JSONDecodeError as e:
        raise ProfileServiceError(f"That isn't valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ProfileServiceError("Profile content must be a JSON object.")

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
    log_activity(db, f"Saved manual profile edit for variant '{variant.name}'.")
    return version


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
    version. This is the zero-risk sync source per CLAUDE.md -- it's the
    user's own site -- so unlike LinkedIn paste-diff, it activates
    immediately rather than sitting in a pending/approval state."""
    variant = _get_variant_or_raise(db, variant_id)
    url = os.getenv("PORTFOLIO_RESUME_URL", DEFAULT_PORTFOLIO_RESUME_URL)

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
    must call approve_version() first. See CLAUDE.md: 'gets AI-diffed
    into the profile with user approval, versioned.'"""
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
