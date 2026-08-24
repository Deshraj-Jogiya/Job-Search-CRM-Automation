"""Fills a real Ashby application form (`jobs.ashbyhq.com/<company>/<id>/
application`) in a real, visible Playwright browser page, then stops.
Same never-clicks-submit, never-touches-CAPTCHA contract as
`greenhouse_autofill.py`.

Ashby has no cross-field-reset quirk like Greenhouse's. Standard fields
use stable ids (`#_systemfield_name`, `#_systemfield_email`,
`#_systemfield_resume`). Every question -- standard or custom -- is
wrapped in a stable, semantic (non-hashed) class
`ashby-application-form-field-entry`, reliable to enumerate generically
across postings. Yes/No questions are not real checkboxes/radios --
they're `<button data-option="yes/no" aria-pressed="...">` toggle pairs
backed by a hidden checkbox, so a plain `.click()` on the button is the
real interaction. Multi-select questions (e.g. office location) use
real `<input type=checkbox>` with the option text itself as `name`.
Single-select "pick one of several" questions (distinct from the
yes/no toggles) use real `<input type=radio>` sharing one `name`.
Resume upload is a custom dropzone needing the same
`expect_file_chooser()` fix as Greenhouse/Lever, triggered via the
"Upload File" button.
"""

import json

from playwright.sync_api import Page

from ..llm import get_llm_provider, parse_json_response
from .common_answers import is_referral_source_question, mechanical_common_answer, referral_source_answer


def _standard_field_values(profile: dict) -> dict:
    contact = profile.get("contact") or {}
    values = {}
    if profile.get("name"):
        values["_systemfield_name"] = profile["name"]
    if contact.get("email"):
        values["_systemfield_email"] = contact["email"]
    return values


def _upload_resume(page: Page, resume_path: str) -> bool:
    # Playwright's text engine (`text=`), not get_by_text -- get_by_text
    # resolves to a non-reliably-clickable inner node here, which fails
    # to trigger the real file chooser without raising an exception.
    upload_trigger = page.locator('text="Upload File"').first
    if upload_trigger.count() == 0:
        return False
    try:
        with page.expect_file_chooser(timeout=5000) as fc_info:
            upload_trigger.click()
        fc_info.value.set_files(resume_path)
        return True
    except Exception:
        return False


def _detect_custom_questions(page: Page) -> list[dict]:
    """Enumerates every question generically via the stable
    `ashby-application-form-field-entry` class, which holds across
    postings. Skips the standard name/email/resume entries (handled
    separately). field_type: 'yesno' | 'radio' | 'checkbox_group' |
    'text' | 'textarea' | 'url'."""
    return page.evaluate(
        """
        () => {
            const results = [];
            document.querySelectorAll('.ashby-application-form-field-entry').forEach(entry => {
                if (entry.querySelector('#_systemfield_name, #_systemfield_email, #_systemfield_resume')) return;

                const titleEl = entry.querySelector('.ashby-application-form-question-title');
                const label = titleEl ? titleEl.innerText.trim() : entry.innerText.split('\\n')[0].trim();
                const fieldPath = entry.getAttribute('data-field-path');

                if (entry.querySelector('.ashby-application-form-input-yesno')) {
                    results.push({field_path: fieldPath, label, field_type: 'yesno'});
                    return;
                }
                const radios = entry.querySelectorAll('input[type="radio"]');
                if (radios.length > 0) {
                    const options = Array.from(radios).map(r => {
                        const lbl = document.querySelector(`label[for="${r.id}"]`);
                        return lbl ? lbl.innerText.trim() : r.value;
                    });
                    results.push({field_path: fieldPath, label, field_type: 'radio', options, name: radios[0].name});
                    return;
                }
                const checkboxes = entry.querySelectorAll('input[type="checkbox"]');
                if (checkboxes.length > 0) {
                    const options = Array.from(checkboxes).map(c => c.name);
                    results.push({field_path: fieldPath, label, field_type: 'checkbox_group', options});
                    return;
                }
                const textarea = entry.querySelector('textarea');
                if (textarea) {
                    results.push({field_path: fieldPath, label, field_type: 'textarea', input_id: textarea.id});
                    return;
                }
                const urlInput = entry.querySelector('input[type="url"]');
                if (urlInput) {
                    results.push({field_path: fieldPath, label, field_type: 'url', input_id: urlInput.id});
                    return;
                }
                const textInput = entry.querySelector('input[type="text"], input[type="email"]');
                if (textInput) {
                    results.push({field_path: fieldPath, label, field_type: 'text', input_id: textInput.id});
                }
            });
            return results;
        }
        """
    )


def _mechanical_answers(questions: list[dict], profile: dict, posting_source: str) -> tuple[dict, list[dict]]:
    """Resolves EEO/referral-source questions directly from the profile
    and posting source, without an LLM call -- see common_answers.py.
    Same caveat as Lever's equivalent: Ashby's EEO block hasn't been
    separately live-verified here, it's picked up via the same generic
    `ashby-application-form-field-entry` detection everything else uses;
    if that assumption doesn't hold for a given posting, the mechanical
    match simply finds nothing and the question falls back to the
    LLM/leave-for-human path exactly as before."""
    answers = {}
    remaining = []
    for q in questions:
        label = q.get("label", "")
        value = referral_source_answer(posting_source) if is_referral_source_question(label) else mechanical_common_answer(label, profile)
        if value is None:
            remaining.append(q)
        else:
            answers[q["field_path"]] = value
    return answers, remaining


def _draft_custom_answers(questions: list[dict], profile: dict, jd_text: str, company_name: str) -> dict:
    """Same honest-only-from-profile contract as the Greenhouse/Lever
    drafting calls. For yesno/radio/checkbox_group questions the answer
    is matched (not trusted verbatim) against the real option set at
    fill time -- see `_match_option`."""
    if not questions:
        return {}

    lines = []
    for i, q in enumerate(questions):
        if q["field_type"] == "yesno":
            lines.append(f"{i + 1}. {q['label']} (answer exactly 'Yes' or 'No')")
        elif q["field_type"] in ("radio", "checkbox_group") and q.get("options"):
            opts = "; ".join(q["options"])
            lines.append(f"{i + 1}. {q['label']} (choose exactly one of: {opts})")
        else:
            lines.append(f"{i + 1}. {q['label']}")

    llm = get_llm_provider()
    raw = llm.complete_json(
        system=(
            "You are a careful, honest job-application assistant filling out a real application on a "
            "real candidate's behalf. You return only raw JSON."
        ),
        prompt=(
            "Draft a short, honest answer to each screening question below, grounded ONLY in the "
            "candidate's real profile. Never invent experience, credentials, dates, or facts not present "
            "in the profile. If a question offers a fixed list of choices, answer with the text of exactly "
            "one of those choices, as close to verbatim as possible. If a question genuinely can't be "
            "answered honestly from the profile, answer with exactly '[REVIEW NEEDED]' instead of "
            "guessing.\n\n"
            f"Candidate profile:\n{json.dumps(profile, indent=2)}\n\n"
            f"Target company: {company_name}\n"
            f"Job description:\n{jd_text[:3000]}\n\n"
            f"Questions (answer in this exact order):\n"
            + "\n".join(lines)
            + "\n\n"
            'Respond with EXACTLY this JSON shape: {"answers": ["answer 1", "answer 2", ...]}\n'
            "Do not wrap the output in markdown code fences."
        ),
        temperature=0.4,
    )
    parsed = parse_json_response(raw)
    answers = parsed.get("answers", [])
    return {q["field_path"]: answers[i] for i, q in enumerate(questions) if i < len(answers) and answers[i]}


def _match_option(value: str, options: list[str]) -> int | None:
    """Same conservative match-or-leave-for-human policy as the
    Greenhouse/Lever combobox fillers -- never guesses."""
    if not value or value == "[REVIEW NEEDED]":
        return None
    target = value.strip().lower()
    for i, opt in enumerate(options):
        if opt.strip().lower() == target:
            return i
    for i, opt in enumerate(options):
        o = opt.strip().lower()
        if target in o or o in target:
            return i
    return None


def _fill_yesno(page: Page, field_path: str, value: str) -> bool:
    if not value or value.strip().lower() not in ("yes", "no"):
        return False
    option = value.strip().lower()
    entry = page.locator(f'[data-field-path="{field_path}"]')
    button = entry.locator(f'button[data-option="{option}"]')
    if button.count() == 0:
        return False
    try:
        button.click()
        return True
    except Exception:
        return False


def _fill_radio(page: Page, field_path: str, name: str, options: list[str], value: str) -> bool:
    idx = _match_option(value, options)
    if idx is None:
        return False
    entry = page.locator(f'[data-field-path="{field_path}"]')
    try:
        entry.locator('input[type="radio"]').nth(idx).check(force=True)
        return True
    except Exception:
        return False


def _fill_checkbox_group(page: Page, field_path: str, options: list[str], value: str) -> bool:
    idx = _match_option(value, options)
    if idx is None:
        return False
    entry = page.locator(f'[data-field-path="{field_path}"]')
    try:
        entry.locator('input[type="checkbox"]').nth(idx).check(force=True)
        return True
    except Exception:
        return False


def _fill_text_like(page: Page, input_id: str, value: str) -> bool:
    if not value or value == "[REVIEW NEEDED]":
        return False
    # Attribute selector, not a raw #id -- Ashby's custom-question ids are
    # UUIDs and a CSS id selector is invalid syntax if the id starts with
    # a digit (e.g. "#6afb8872-...").
    locator = page.locator(f'[id="{input_id}"]')
    if locator.count() == 0:
        return False
    try:
        locator.fill(value)
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


def autofill_ashby_application(
    page: Page,
    profile: dict,
    resume_pdf_path: str,
    cover_letter_pdf_path: str | None,
    jd_text: str,
    company_name: str,
    posting_source: str = "",
) -> dict:
    """Fills a real, already-navigated Ashby application page. Never
    clicks submit, never interacts with a CAPTCHA. `cover_letter_pdf_path`
    is accepted for interface parity with the Greenhouse/Lever modules
    but Ashby's standard application form has no separate cover-letter
    upload slot."""
    resume_uploaded = _upload_resume(page, resume_pdf_path)

    standard_values = _standard_field_values(profile)
    standard_filled = set()
    for field_id, value in standard_values.items():
        if _fill_text_like(page, field_id, value):
            standard_filled.add(field_id)

    questions = _detect_custom_questions(page)
    mechanical, llm_questions = _mechanical_answers(questions, profile, posting_source)
    answers = _draft_custom_answers(llm_questions, profile, jd_text, company_name)
    answers.update(mechanical)

    question_filled = set()
    for q in questions:
        value = answers.get(q["field_path"])
        if value is None:
            continue
        ok = False
        if q["field_type"] == "yesno":
            ok = _fill_yesno(page, q["field_path"], value)
        elif q["field_type"] == "radio":
            ok = _fill_radio(page, q["field_path"], q["name"], q.get("options", []), value)
        elif q["field_type"] == "checkbox_group":
            ok = _fill_checkbox_group(page, q["field_path"], q.get("options", []), value)
        elif q["field_type"] in ("text", "textarea", "url"):
            ok = _fill_text_like(page, q["input_id"], value)
        if ok:
            question_filled.add(q["field_path"])

    result = {
        "standard_fields": list(standard_filled),
        "files": ["resume"] if resume_uploaded else [],
        "custom_questions": list(question_filled),
        "custom_questions_detected": len(questions),
    }

    summary = (
        f"Filled {len(result['standard_fields'])} contact field(s), "
        f"{len(result['files'])} file(s), "
        f"{len(result['custom_questions'])}/{result['custom_questions_detected']} question(s)."
    )
    _inject_review_banner(page, summary)
    return result
