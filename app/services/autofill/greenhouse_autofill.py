"""
Fills a real Greenhouse application form in a real, visible Playwright
browser page, then stops. Never clicks submit and never touches a
CAPTCHA -- both are left for the human present in that browser session,
since Greenhouse forms carry a real reCAPTCHA that only a present human
can clear anyway.

Field IDs (first_name, last_name, email, phone, resume, cover_letter,
school--0, degree--0, ...) are consistent across every company on
Greenhouse's standard hosted board (job-boards.greenhouse.io/<company>).
Custom per-company screening questions (id="question_<n>") vary by
company and are answered with profile-grounded LLM drafts rather than
fixed logic, since a generic answer would either be wrong or fabricated.

Greenhouse's form is React-controlled: filling one text field can
silently reset an already-filled earlier field back to empty (e.g.
filling last_name can empty out a first_name that was just filled). A
single `.fill()` pass per field is therefore not trustworthy on its
own -- every text field is filled together, then re-verified and
re-filled in convergence passes so a later fill can't silently undo an
earlier one. See `_fill_text_fields_with_reconciliation`.

The resume/cover-letter upload widgets are custom JS dropzones, not
plain file inputs -- attaching a file directly to the underlying
`<input type=file>` leaves the widget's own JS in a broken state, since
it expects the file to arrive through a real click-triggered file
chooser. Fixed via Playwright's `expect_file_chooser()` pattern: click
the real "Attach" button, catch the resulting `FileChooser`, and call
`.set_files()` on that instead. See `_upload_via_file_chooser`.

Education fields (school--0/degree--0) and some custom questions render
as react-select comboboxes rather than plain text inputs -- `.fill()`
sets the raw input's DOM value but never commits a real selection in
react-select's internal state. These need a click-to-open,
type-to-filter, match-and-click-the-option interaction instead. See
`_fill_react_select_field`, which deliberately leaves a field untouched
rather than guess when nothing rendered matches the target value.
"""

import json

from playwright.sync_api import Page

from ..llm import get_llm_provider, parse_json_response
from .common_answers import is_referral_source_question, mechanical_common_answer, referral_source_answer

_RECONCILE_PASSES = 4

# Greenhouse's Voluntary Self-Identification / EEO block sits OUTSIDE
# the question_<n> custom-question sweep, on its own stable ids --
# confirmed via live DOM inspection of a real posting (job-boards.
# greenhouse.io/hasbro/jobs/4267480009), consistent with every other
# standard-field id on Greenhouse's hosted board being fixed regardless
# of company. Each is a react-select combobox, same interaction as the
# education fields -- label text here is what mechanical_common_answer
# matches against, not what's shown to the user.
_EEO_FIELD_LABELS = {
    "gender": "Gender",
    "hispanic_ethnicity": "Are you Hispanic/Latino?",
    "veteran_status": "Veteran Status",
    "disability_status": "Disability Status",
}


def _standard_field_values(profile: dict) -> dict:
    name_parts = (profile.get("name") or "").split()
    contact = profile.get("contact") or {}
    values = {
        "first_name": name_parts[0] if name_parts else "",
        "last_name": " ".join(name_parts[1:]) if len(name_parts) > 1 else "",
        "email": contact.get("email", ""),
        "phone": contact.get("phone", ""),
    }
    return {k: v for k, v in values.items() if v}


def _education_field_values(profile: dict) -> dict:
    """Best-effort only -- school/degree map cleanly onto Greenhouse's
    school--0/degree--0 text fields, but this profile schema doesn't
    carry a real structured start/end date or a separate "discipline"
    (major), so those specific sub-fields are deliberately left for the
    human rather than guessed at."""
    education = profile.get("education") or []
    if not education:
        return {}
    edu = education[0]
    values = {"school--0": edu.get("school"), "degree--0": edu.get("degree")}
    return {k: v for k, v in values.items() if v}


def _eeo_field_values(page: Page, profile: dict) -> dict:
    """Only includes an EEO field if it's actually present on this
    posting's form AND the profile has a stored answer for it -- some
    companies' Greenhouse boards omit the EEO block entirely, and a
    profile with no `eeo` section simply leaves these for the human,
    same as before."""
    values = {}
    for field_id, label in _EEO_FIELD_LABELS.items():
        if page.locator(f"#{field_id}").count() == 0:
            continue
        answer = mechanical_common_answer(label, profile)
        if answer:
            values[field_id] = answer
    return values


def _upload_via_file_chooser(page: Page, attach_button_selector: str, file_path: str) -> bool:
    attach_button = page.locator(attach_button_selector)
    if attach_button.count() == 0:
        return False
    try:
        with page.expect_file_chooser(timeout=5000) as fc_info:
            attach_button.click()
        fc_info.value.set_files(file_path)
        return True
    except Exception:
        return False


def _upload_files(page: Page, resume_path: str, cover_letter_path: str | None) -> list[str]:
    uploaded = []
    if _upload_via_file_chooser(
        page, '[aria-labelledby="upload-label-resume"] button:has-text("Attach")', resume_path
    ):
        uploaded.append("resume")
    if cover_letter_path and _upload_via_file_chooser(
        page, '[aria-labelledby="upload-label-cover_letter"] button:has-text("Attach")', cover_letter_path
    ):
        uploaded.append("cover_letter")
    return uploaded


def _detect_custom_questions(page: Page) -> list[dict]:
    """field_type distinguishes plain text/textarea questions (filled via
    the reconciliation pass) from react-select comboboxes (filled via
    `_fill_react_select_field`) -- these need genuinely different
    interactions, not just different selectors."""
    return page.evaluate(
        """
        () => {
            const results = [];
            document.querySelectorAll('[id^="question_"]').forEach(el => {
                const tag = el.tagName.toLowerCase();
                if (tag !== 'input' && tag !== 'textarea') return;
                if (el.type === 'file') return;
                let label = '';
                const l = document.querySelector(`label[for="${el.id}"]`);
                if (l) label = l.innerText.trim();
                const field_type = el.getAttribute('role') === 'combobox'
                    ? 'combobox' : (tag === 'textarea' ? 'textarea' : 'text');
                results.push({id: el.id, label: label, field_type: field_type});
            });
            return results;
        }
        """
    )


def _draft_custom_answers(questions: list[dict], profile: dict, jd_text: str, company_name: str) -> dict:
    """One real LLM call drafting an honest answer to every detected
    custom question, grounded only in the real profile -- never
    fabricated. Returns {question_id: draft_answer_text}."""
    if not questions:
        return {}

    llm = get_llm_provider()
    raw = llm.complete_json(
        system=(
            "You are a careful, honest job-application assistant filling out a real application on a "
            "real candidate's behalf. You return only raw JSON."
        ),
        prompt=(
            "Draft a short, honest answer to each screening question below, grounded ONLY in the "
            "candidate's real profile. Never invent experience, credentials, dates, or facts not present "
            "in the profile. If a question genuinely can't be answered honestly from the profile (e.g. a "
            "yes/no about something the profile doesn't state), answer with exactly '[REVIEW NEEDED]' "
            "instead of guessing.\n\n"
            f"Candidate profile:\n{json.dumps(profile, indent=2)}\n\n"
            f"Target company: {company_name}\n"
            f"Job description:\n{jd_text[:3000]}\n\n"
            f"Questions (answer in this exact order):\n"
            + "\n".join(f"{i + 1}. {q['label']}" for i, q in enumerate(questions))
            + "\n\n"
            'Respond with EXACTLY this JSON shape: {"answers": ["answer 1", "answer 2", ...]}\n'
            "Do not wrap the output in markdown code fences."
        ),
        temperature=0.4,
    )
    parsed = parse_json_response(raw)
    answers = parsed.get("answers", [])
    return {q["id"]: answers[i] for i, q in enumerate(questions) if i < len(answers) and answers[i]}


def _fill_text_fields_with_reconciliation(page: Page, values: dict) -> list[str]:
    """Fills every field once, then re-verifies and re-fills as many
    times as needed (see module docstring for why a single fill pass
    isn't trustworthy on this form). Returns the field_ids that ended
    up holding the correct value."""
    if not values:
        return []

    locators = {}
    for field_id in values:
        locator = page.locator(f"#{field_id}")
        if locator.count() > 0:
            locators[field_id] = locator

    for _ in range(_RECONCILE_PASSES):
        mismatched = {}
        for field_id, locator in locators.items():
            try:
                current = locator.input_value()
            except Exception:
                continue
            if current != values[field_id]:
                mismatched[field_id] = locator
        if not mismatched:
            break
        for field_id, locator in mismatched.items():
            try:
                locator.fill(values[field_id])
            except Exception:
                pass

    correct = []
    for field_id, locator in locators.items():
        try:
            if locator.input_value() == values[field_id]:
                correct.append(field_id)
        except Exception:
            pass
    return correct


_EDUCATION_COMBOBOX_FIELDS = {"school--0", "degree--0"}


def _fill_react_select_field(page: Page, field_id: str, value: str) -> bool:
    """Fills a Greenhouse react-select combobox (education school/degree,
    and any question_<n> rendered as a Yes/No or other enum dropdown
    instead of plain text). See module docstring -- `.fill()` never
    commits a real selection on these. Click to open -> type to filter ->
    match the rendered option text -> click it. Returns False (leaving
    the field untouched for the human) if the answer is a `[REVIEW
    NEEDED]` placeholder, the field isn't present, or nothing rendered
    actually matches the target value -- never guesses a close-but-wrong
    option."""
    if not value or value == "[REVIEW NEEDED]":
        return False

    locator = page.locator(f"#{field_id}")
    if locator.count() == 0:
        return False

    try:
        locator.click()
        locator.type(value, delay=25)
        # Wait for an actual rendered option, not just the listbox
        # container -- the container appears before react-select's
        # filtered options finish rendering, so waiting on the container
        # alone races and can read back zero options.
        page.wait_for_selector(f'[id^="react-select-{field_id}-listbox"] [role="option"]', timeout=6000)
        options = page.evaluate(
            """(fieldId) => {
                const lb = document.querySelector(`[id^="react-select-${fieldId}-listbox"]`);
                if (!lb) return [];
                return Array.from(lb.querySelectorAll('[role="option"]')).map(o => ({id: o.id, text: o.innerText}));
            }""",
            field_id,
        )
    except Exception:
        options = []

    target = value.strip().lower()
    match = next((o for o in options if o["text"].strip().lower() == target), None)
    if not match:
        match = next(
            (o for o in options if target in o["text"].strip().lower() or o["text"].strip().lower() in target),
            None,
        )

    if not match:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False

    try:
        page.locator(f'#{match["id"]}').click()
        page.wait_for_timeout(200)
        return True
    except Exception:
        return False


def _inject_review_banner(page: Page, summary: str) -> None:
    page.evaluate(
        """(summary) => {
            const banner = document.createElement('div');
            banner.style.cssText = 'position:sticky;top:0;z-index:999999;background:#fef3c7;'
                + 'color:#92400e;padding:12px 20px;font-family:sans-serif;font-size:14px;'
                + 'border-bottom:2px solid #f59e0b;';
            banner.innerHTML = '<strong>AI-prefilled draft -- review every field before submitting. '
                + 'Nothing has been submitted.</strong> ' + summary;
            document.body.prepend(banner);
        }""",
        summary,
    )


def autofill_greenhouse_application(
    page: Page,
    profile: dict,
    resume_pdf_path: str,
    cover_letter_pdf_path: str | None,
    jd_text: str,
    company_name: str,
    posting_source: str = "",
) -> dict:
    """Fills a real, already-navigated Greenhouse application page.
    Never clicks submit, never interacts with the CAPTCHA -- stops at a
    filled (or best-effort filled) form for the human to review and
    submit themselves."""
    files_result = _upload_files(page, resume_pdf_path, cover_letter_pdf_path)

    standard_values = _standard_field_values(profile)
    education_values = _education_field_values(profile)
    eeo_values = _eeo_field_values(page, profile)

    all_questions = _detect_custom_questions(page)
    # "How did you hear about us" is answered mechanically from which
    # intake source actually found this posting, not drafted by the
    # per-JD LLM call (which has no real basis to answer it and mostly
    # just left it blank) -- pulled out before the LLM ever sees it.
    referral_questions = [q for q in all_questions if is_referral_source_question(q["label"])]
    llm_questions = [q for q in all_questions if q not in referral_questions]

    answers = _draft_custom_answers(llm_questions, profile, jd_text, company_name)
    for q in referral_questions:
        answers[q["id"]] = referral_source_answer(posting_source)
    question_types = {q["id"]: q["field_type"] for q in all_questions}

    # Split every field this function tries to fill into plain
    # text/textarea (reconciliation-pass fill) vs react-select combobox
    # (click-type-match-click fill) -- see module docstring for why
    # these need genuinely different interactions.
    combobox_values = {k: v for k, v in education_values.items() if k in _EDUCATION_COMBOBOX_FIELDS}
    combobox_values.update({k: v for k, v in answers.items() if question_types.get(k) == "combobox"})
    combobox_values.update(eeo_values)  # EEO fields are react-select comboboxes too

    plain_values = dict(standard_values)
    plain_values.update({k: v for k, v in education_values.items() if k not in _EDUCATION_COMBOBOX_FIELDS})
    plain_values.update({k: v for k, v in answers.items() if question_types.get(k) != "combobox"})

    # Fill plain fields first (one combined reconciliation pass -- keeps
    # a later category's fills from silently undoing an earlier
    # category's, same reset quirk as within a single category), then
    # combobox fields -- each click/type/select interaction is its own
    # isolated action so combobox fills don't need reconciliation.
    plain_filled = set(_fill_text_fields_with_reconciliation(page, plain_values))
    combobox_filled = {
        field_id for field_id, value in combobox_values.items() if _fill_react_select_field(page, field_id, value)
    }
    all_filled = plain_filled | combobox_filled

    result = {
        "standard_fields": [f for f in standard_values if f in all_filled],
        "files": files_result,
        "education": [f for f in education_values if f in all_filled],
        "eeo": [f for f in eeo_values if f in all_filled],
        "custom_questions": [f for f in answers if f in all_filled],
        "custom_questions_detected": len(all_questions),
    }

    summary = (
        f"Filled {len(result['standard_fields'])} contact field(s), "
        f"{len(result['files'])} file(s), "
        f"{len(result['custom_questions'])}/{result['custom_questions_detected']} question(s)."
    )
    _inject_review_banner(page, summary)
    return result
