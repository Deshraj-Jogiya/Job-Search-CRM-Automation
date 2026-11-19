"""
Profile-level interview prep page (/interview-prep): the general prep for any
call or interview, not tied to an application. See
app/services/profile_prep_service.py for what it contains and why, and for how
it is kept in step with the profile.
"""

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..app_mode import is_showcase_mode
from ..database import get_db
from ..models import get_or_create_settings
from ..services.llm import AI_REQUIRED_MESSAGE, llm_available
from ..services import prep_content, prep_practice_service, profile_prep_service, tailoring_service
from ..services.profile_prep_service import ProfilePrepServiceError
from ..templating import render

router = APIRouter(prefix="/interview-prep", tags=["profile-interview-prep"])


@router.get("", response_class=HTMLResponse)
def profile_prep_page(request: Request, db: Session = Depends(get_db)):
    try:
        profile, variant = profile_prep_service.load_profile(db)
    except ProfilePrepServiceError as e:
        return render(request, "profile_prep.html", {"setup_error": str(e), "message": None, "error": None})

    # Opening the page with nothing drafted for the current profile starts the work (the same catch-up the
    # hourly scheduler runs), so the first visit after setup, or after a profile change, never sits empty
    # waiting for a click. Honours the auto-refresh switch and the failure cooldown.
    prep_status = profile_prep_service.status(db)
    if prep_status["state"] != "current" and not prep_status["generating"]:
        profile_prep_service.catch_up_in_background()
        prep_status = profile_prep_service.status(db)

    prep, drafted = profile_prep_service.get_active_prep(db, variant.id)
    return render(
        request,
        "profile_prep.html",
        {
            "setup_error": None,
            "reference": profile_prep_service.build_reference(db, profile, variant.id),
            "prep": prep,
            "drafted": drafted,
            "prep_status": prep_status,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/generate")
def generate_profile_prep_now(db: Session = Depends(get_db)):
    """The manual Refresh button: always runs (ignores the failure cooldown and the auto-refresh switch)."""
    if not llm_available() and is_showcase_mode():
        try:
            profile_prep_service.generate_profile_prep(db, "manual")
        except ProfilePrepServiceError as e:
            return RedirectResponse(url=f"/interview-prep?error={quote(str(e))}", status_code=303)
        return RedirectResponse(url="/interview-prep?message=" + quote(
            "Rebuilt by rules from the latest profile and confirmed stories."), status_code=303)
    if not llm_available():
        return RedirectResponse(url=f"/interview-prep?error={quote(AI_REQUIRED_MESSAGE)}", status_code=303)
    try:
        profile_prep_service.load_profile(db)
    except ProfilePrepServiceError as e:
        return RedirectResponse(url=f"/interview-prep?error={quote(str(e))}", status_code=303)
    started = profile_prep_service.start_background_generation("manual")
    message = (
        "Refreshing from your latest profile -- one AI pass over your profile and confirmed stories. "
        "Usually under a minute or two. Refresh this page in a bit."
        if started else "A refresh is already running -- check back in a minute."
    )
    return RedirectResponse(url="/interview-prep?message=" + quote(message), status_code=303)


# ---------------------------------------------------------------------------
# Deep dives, question bank, coding and practice (see app/services/prep_content.py)
# ---------------------------------------------------------------------------

def _context(db: Session):
    profile, variant = profile_prep_service.load_profile(db)
    _, drafted = profile_prep_service.get_active_prep(db, variant.id)
    return profile, variant, drafted or {}


def _practice(db: Session, variant_id: int) -> dict:
    # The demo is shared by every visitor, so nothing is stored server side there; the page keeps it in the browser.
    return {} if is_showcase_mode() else prep_practice_service.progress(db, variant_id)


def _setup_error(request: Request, template: str, e: Exception):
    return render(request, template, {"setup_error": str(e), "message": None, "error": None})


def _back(path: str, message: str | None = None, error: str | None = None) -> RedirectResponse:
    query = f"?message={quote(message)}" if message else f"?error={quote(error)}" if error else ""
    return RedirectResponse(url=path + query, status_code=303)


@router.get("/deep-dives", response_class=HTMLResponse)
def deep_dives_page(request: Request, db: Session = Depends(get_db)):
    try:
        profile, variant, drafted = _context(db)
    except ProfilePrepServiceError as e:
        return _setup_error(request, "prep_deep_dives.html", e)
    cards = prep_content.build_cards(profile, drafted.get("deep_dives"))
    return render(request, "prep_deep_dives.html", {
        "setup_error": None, "cards": cards, "guide": prep_content.deep_dive_guide(),
        "orphans": prep_content.orphan_cards(profile, drafted.get("deep_dives")),
        "practice": _practice(db, variant.id), "ratings": prep_practice_service.RATINGS,
        "message": request.query_params.get("message"), "error": request.query_params.get("error"),
    })


@router.get("/deep-dives/{key}/edit", response_class=HTMLResponse)
def deep_dive_edit_page(key: str, request: Request, db: Session = Depends(get_db)):
    if is_showcase_mode():
        return _back("/interview-prep/deep-dives", error="Editing is turned off in this public demo.")
    try:
        profile, variant, drafted = _context(db)
    except ProfilePrepServiceError as e:
        return _setup_error(request, "prep_deep_dive_edit.html", e)
    card = next((c for c in prep_content.build_cards(profile, drafted.get("deep_dives")) if c["key"] == key), None)
    if card is None:
        return _back("/interview-prep/deep-dives", error="That item is no longer in your profile.")
    guide = prep_content.deep_dive_guide()
    return render(request, "prep_deep_dive_edit.html", {
        "setup_error": None, "card": card, "form": prep_content.card_to_form(card),
        "layers": guide["cert_layers"] if card["kind"] == "certification" else guide["layers"],
        "pair_fields": prep_content.CARD_PAIR_FIELDS, "line_fields": prep_content.CARD_LINE_FIELDS,
        "message": None, "error": request.query_params.get("error"),
    })


@router.post("/deep-dives/{key}/edit")
async def deep_dive_save(key: str, request: Request, db: Session = Depends(get_db)):
    if is_showcase_mode():
        return _back("/interview-prep/deep-dives", error="Editing is turned off in this public demo.")
    profile, variant, drafted = _context(db)
    source_hash = prep_content.fingerprint_for(profile, key)
    if source_hash is None:
        return _back("/interview-prep/deep-dives", error="That item is no longer in your profile.")
    form = dict(await request.form())
    card = prep_content.card_from_form(key, form, source_hash)
    cards = [c for c in drafted.get("deep_dives") or [] if c.get("key") != key] + [card]
    profile_prep_service.save_general_section(db, variant.id, "deep_dives", cards)
    facts = json.dumps(profile, ensure_ascii=False)
    stray = tailoring_service.unsupported_numbers(prep_content.card_text(card), facts)
    note = (f" Check these numbers, they are not in your profile: {', '.join(stray)}." if stray else "")
    return _back(f"/interview-prep/deep-dives", message="Saved." + note)


@router.post("/deep-dives/{key}/delete")
def deep_dive_delete(key: str, db: Session = Depends(get_db)):
    if is_showcase_mode():
        return _back("/interview-prep/deep-dives", error="Editing is turned off in this public demo.")
    _, variant, drafted = _context(db)
    cards = [c for c in drafted.get("deep_dives") or [] if c.get("key") != key]
    profile_prep_service.save_general_section(db, variant.id, "deep_dives", cards)
    return _back("/interview-prep/deep-dives", message="Removed the written card; the outline from your profile shows again.")


@router.get("/questions", response_class=HTMLResponse)
def questions_page(request: Request, db: Session = Depends(get_db)):
    try:
        profile, variant, drafted = _context(db)
    except ProfilePrepServiceError as e:
        return _setup_error(request, "prep_questions.html", e)
    return render(request, "prep_questions.html", {
        "setup_error": None, "bank": prep_content.question_bank(), "examples": drafted.get("question_examples") or {},
        "practice": _practice(db, variant.id), "ratings": prep_practice_service.RATINGS,
        "message": request.query_params.get("message"), "error": request.query_params.get("error"),
    })


@router.post("/questions/{qid}/example")
def question_example_save(qid: str, example: str = Form(""), db: Session = Depends(get_db)):
    if is_showcase_mode():
        return _back("/interview-prep/questions", error="Editing is turned off in this public demo.")
    if prep_content.question_by_id(qid) is None:
        return _back("/interview-prep/questions", error="Unknown question.")
    profile, variant, drafted = _context(db)
    examples = dict(drafted.get("question_examples") or {})
    if example.strip():
        examples[qid] = example.strip()
    else:
        examples.pop(qid, None)
    profile_prep_service.save_general_section(db, variant.id, "question_examples", examples)
    stray = tailoring_service.unsupported_numbers(example, json.dumps(profile, ensure_ascii=False))
    note = f" Check these numbers, they are not in your profile: {', '.join(stray)}." if stray else ""
    return RedirectResponse(url=f"/interview-prep/questions?message={quote('Saved.' + note)}#q-{qid}", status_code=303)


@router.get("/coding", response_class=HTMLResponse)
def coding_page(request: Request, db: Session = Depends(get_db)):
    try:
        _, variant, _ = _context(db)
    except ProfilePrepServiceError as e:
        return _setup_error(request, "prep_coding.html", e)
    return render(request, "prep_coding.html", {
        "setup_error": None, "coding": prep_content.coding_practice(), "problems": prep_content.all_problems(),
        "practice": _practice(db, variant.id), "ratings": prep_practice_service.RATINGS,
        "message": request.query_params.get("message"), "error": request.query_params.get("error"),
    })


@router.get("/practice", response_class=HTMLResponse)
def practice_page(request: Request, db: Session = Depends(get_db)):
    try:
        profile, variant, _ = _context(db)
    except ProfilePrepServiceError as e:
        return _setup_error(request, "prep_practice.html", e)
    keys = prep_content.all_card_keys(profile)
    rows = _practice(db, variant.id)
    due = [] if is_showcase_mode() else prep_practice_service.due(db, variant.id, set(keys))
    groups = {
        "Deep dives": [k for k in keys if k.startswith("dd:")],
        "Questions": [k for k in keys if k.startswith("qb:")],
        "Coding patterns": [k for k in keys if k.startswith("pat:")],
        "Coding problems": [k for k in keys if k.startswith("code:")],
    }
    return render(request, "prep_practice.html", {
        "setup_error": None,
        "due": [dict(prep_content.describe_key(r.card_key, profile), key=r.card_key, row=r) for r in due],
        "summary": {name: prep_practice_service.summary(rows, ks) for name, ks in groups.items()},
        "all_items": [dict(prep_content.describe_key(k, profile), key=k) for k in keys],
        "ratings": prep_practice_service.RATINGS, "intervals": prep_practice_service.INTERVAL_DAYS,
        "message": request.query_params.get("message"), "error": request.query_params.get("error"),
    })


@router.post("/practice/rate")
def practice_rate(card_key: str = Form(...), rating: int = Form(...), back: str = Form("/interview-prep/practice"),
                  db: Session = Depends(get_db)):
    if not back.startswith("/interview-prep"):
        back = "/interview-prep/practice"
    if is_showcase_mode():
        return _back(back.split("#")[0], error="In this demo, practice is kept in your browser only.")
    _, variant, _ = _context(db)
    try:
        row = prep_practice_service.rate(db, variant.id, card_key, rating)
    except prep_practice_service.PrepPracticeError as e:
        return _back(back.split("#")[0], error=str(e))
    days = prep_practice_service.INTERVAL_DAYS[row.box]
    path, _, anchor = back.partition("#")
    return RedirectResponse(url=f"{path}?message={quote(f'Next review in {days} day' + ('s' if days != 1 else '') + '.')}"
                            + (f"#{anchor}" if anchor else ""), status_code=303)


@router.post("/auto-refresh")
def set_auto_refresh(auto_refresh: bool = Form(False), db: Session = Depends(get_db)):
    settings = get_or_create_settings(db)
    settings.profile_prep_auto_refresh = auto_refresh
    db.commit()
    return RedirectResponse(
        url="/interview-prep?message=" + quote(
            "Auto-refresh is on: drafted answers follow every profile change." if auto_refresh
            else "Auto-refresh is off: use the Refresh button when you want it updated."
        ),
        status_code=303,
    )
