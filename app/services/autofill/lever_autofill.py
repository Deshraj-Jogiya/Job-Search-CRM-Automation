"""Fills a real Lever application form (`jobs.lever.co/<company>/<id>/
apply`) in a real, visible Playwright browser page, then stops. Same
never-clicks-submit, never-touches-CAPTCHA contract as
`greenhouse_autofill.py`.

Lever's form is plain HTML, not React-controlled -- no cross-field-reset
quirk like Greenhouse's, and no react-select-style combobox interaction
either. Standard fields have no `id`, only a `name` attribute
(`name="name"`, `name="email"`, `name="phone"`). URL fields are
posting-specific (`name="urls[LinkedIn (optional)]"` etc., the label
text is baked into the name itself) so they're detected from the DOM
rather than hardcoded. Custom questions live in
`cards[<uuid>][field<n>]`-named fields, generically enumerable via the
stable `li.application-question.custom-question` wrapper with a
`.application-label` child -- each is a plain text input, a textarea, a
native `<select>` (real `<option>` elements, no custom widget), or a
group of real `<input type=checkbox>` sharing one `name` with distinct
`value`s (Yes/No and Likert-style questions). Resume upload is a custom
JS dropzone like Greenhouse's, needing the same `expect_file_chooser()`
fix, triggered via the visible `a.visible-resume-upload` link.
"""

import json

from playwright.sync_api import Page

from ..llm import get_llm_provider, parse_json_response
from .common_answers import is_referral_source_question, mechanical_common_answer, referral_source_answer


def _standard_field_values(page: Page, profile: dict) -> dict:
    """Unlike Greenhouse, Lever's URL-type fields (LinkedIn/GitHub/
    portfolio) have posting-specific `name`s with the label baked in
    (e.g. `urls[LinkedIn (optional)]`) -- detected from the real DOM
    rather than hardcoded, then matched against the profile by keyword.

    Deliberately does NOT fill `location` -- it's a real autocomplete
    widget (backed by a hidden `selectedLocation` field) that discards a
    plain typed value on an async timer rather than a plain text input.
    It isn't required on the standard apply form either, so it's left
    for the human rather than building a third distinct combobox
    mechanism just for this one optional field."""
    contact = profile.get("contact") or {}
    values = {}
    if profile.get("name"):
        values["name"] = profile["name"]
    if contact.get("email"):
        values["email"] = contact["email"]
    if contact.get("phone"):
        values["phone"] = contact["phone"]

    url_field_names = page.evaluate(
        """() => Array.from(document.querySelectorAll('input[name^="urls["]')).map(el => el.name)"""
    )
    for field_name in url_field_names:
        lower = field_name.lower()
        if "linkedin" in lower and contact.get("linkedin"):
            values[field_name] = contact["linkedin"]
        elif "github" in lower and contact.get("github"):
            values[field_name] = contact["github"]
        elif ("portfolio" in lower or "website" in lower) and contact.get("portfolio"):
            values[field_name] = contact["portfolio"]

    return values


def _upload_resume(page: Page, resume_path: str) -> bool:
    attach_link = page.locator("a.visible-resume-upload").first
    if attach_link.count() == 0:
        return False
    try:
        with page.expect_file_chooser(timeout=5000) as fc_info:
            attach_link.click()
        fc_info.value.set_files(resume_path)
        return True
    except Exception:
        return False


def _detect_custom_questions(page: Page) -> list[dict]:
    """Enumerates every `cards[...]` question generically via the stable
    `li.application-question.custom-question` wrapper, which holds
    across postings/companies unlike the per-posting `cards[<uuid>]`
    names themselves."""
    return page.evaluate(
        """
        () => {
            const results = [];
            document.querySelectorAll('li.application-question.custom-question').forEach(li => {
                const labelEl = li.querySelector('.application-label');
                const label = labelEl ? labelEl.innerText.replace(/[\\u2731*]/g, '').trim() : '';
                const select = li.querySelector('select');
                const checkboxes = li.querySelectorAll('input[type="checkbox"]');
                const textarea = li.querySelector('textarea');
                const textInput = li.querySelector('input[type="text"], input[type="email"]');
                if (select) {
                    // Keep every real <option> including any placeholder --
                    // indices here must line up 1:1 with select_option(index=)
                    // on the Python side, so nothing gets filtered out.
                    results.push({
                        name: select.name, label, field_type: 'select',
                        options: Array.from(select.options).map(o => o.text.trim())
                    });
                } else if (checkboxes.length > 0) {
                    results.push({
                        name: checkboxes[0].name, label, field_type: 'checkbox_group',
                        options: Array.from(checkboxes).map(c => c.value)
                    });
                } else if (textarea) {
                    results.push({name: textarea.name, label, field_type: 'textarea'});
                } else if (textInput) {
                    results.push({name: textInput.name, label, field_type: 'text'});
                }
            });
            return results;
        }
        """
    )


def _mechanical_answers(questions: list[dict], profile: dict, posting_source: str) -> tuple[dict, list[dict]]:
    """Resolves EEO/referral-source questions directly from the profile
    and posting source, without an LLM call -- see common_answers.py.
    Lever's EEO block hasn't been separately live-verified the way
    Greenhouse's was (it's picked up here via the same generic
    `li.application-question.custom-question` detection everything else
    uses, on the assumption it's structured the same way as any other
    question); if that assumption is wrong for a given posting, this
    mechanical match simply finds nothing and the question falls back
    to the LLM/leave-for-human path exactly as before -- no regression
    either way. Returns (answers, remaining questions still needing the
    per-JD LLM draft)."""
    answers = {}
    remaining = []
    for q in questions:
        label = q.get("label", "")
        value = referral_source_answer(posting_source) if is_referral_source_question(label) else mechanical_common_answer(label, profile)
        if value is None:
            remaining.append(q)
        else:
            answers[q["name"]] = value
    return answers, remaining


def _draft_custom_answers(questions: list[dict], profile: dict, jd_text: str, company_name: str) -> dict:
    """Same honest-only-from-profile contract as Greenhouse's drafting
    call. For select/checkbox_group questions the prompt supplies the
    real option list and the answer is matched (not trusted verbatim)
    against it at fill time -- see `_match_option`."""
    if not questions:
        return {}

    lines = []
    for i, q in enumerate(questions):
        if q["field_type"] in ("select", "checkbox_group") and q.get("options"):
            real_opts = [o for o in q["options"] if o.strip().lower() not in ("select...", "")]
            opts = "; ".join(real_opts)
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
    return {q["name"]: answers[i] for i, q in enumerate(questions) if i < len(answers) and answers[i]}


def _match_option(value: str, options: list[str]) -> int | None:
    """Matches a drafted answer against a real rendered option list,
    returning its index -- never trusts the LLM's text verbatim as a
    selector value. Exact case-insensitive match preferred, then
    substring either direction; None (leave for human) if nothing
    matches, same conservative policy as Greenhouse's combobox filler."""
    if not value or value == "[REVIEW NEEDED]":
        return None
    target = value.strip().lower()
    placeholders = {"select...", ""}
    for i, opt in enumerate(options):
        if opt.strip().lower() in placeholders:
            continue
        if opt.strip().lower() == target:
            return i
    for i, opt in enumerate(options):
        o = opt.strip().lower()
        if o in placeholders:
            continue
        if target in o or o in target:
            return i
    return None


def _fill_text_fields(page: Page, values: dict) -> list[str]:
    """No React-reset quirk on Lever -- filling one field doesn't undo
    an earlier one, so a single `.fill()` pass per field is trustworthy,
    unlike Greenhouse."""
    filled = []
    for name, value in values.items():
        locator = page.locator(f'[name="{name}"]').first
        if locator.count() == 0:
            continue
        try:
            locator.fill(value)
            filled.append(name)
        except Exception:
            pass
    return filled


def _fill_choice_fields(page: Page, questions: list[dict], answers: dict) -> list[str]:
    """Fills native `<select>` and checkbox-group questions -- both real
    HTML controls, no custom widget interaction needed."""
    filled = []
    for q in questions:
        if q["field_type"] not in ("select", "checkbox_group"):
            continue
        value = answers.get(q["name"])
        idx = _match_option(value, q.get("options", []))
        if idx is None:
            continue
        try:
            if q["field_type"] == "select":
                page.locator(f'select[name="{q["name"]}"]').select_option(index=idx)
            else:
                page.locator(f'input[type="checkbox"][name="{q["name"]}"]').nth(idx).check()
            filled.append(q["name"])
        except Exception:
            pass
    return filled


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


def autofill_lever_application(
    page: Page,
    profile: dict,
    resume_pdf_path: str,
    cover_letter_pdf_path: str | None,
    jd_text: str,
    company_name: str,
    posting_source: str = "",
) -> dict:
    """Fills a real, already-navigated Lever `/apply` page. Never clicks
    submit, never interacts with a CAPTCHA. `cover_letter_pdf_path` is
    accepted for interface parity with the Greenhouse module but Lever's
    standard apply form has no separate cover-letter upload slot."""
    resume_uploaded = _upload_resume(page, resume_pdf_path)

    standard_values = _standard_field_values(page, profile)
    questions = _detect_custom_questions(page)
    mechanical, llm_questions = _mechanical_answers(questions, profile, posting_source)
    answers = _draft_custom_answers(llm_questions, profile, jd_text, company_name)
    answers.update(mechanical)

    text_like_values = dict(standard_values)
    text_like_values.update(
        {q["name"]: answers[q["name"]] for q in questions if q["field_type"] in ("text", "textarea") and q["name"] in answers}
    )
    text_filled = set(_fill_text_fields(page, text_like_values))
    choice_filled = set(_fill_choice_fields(page, questions, answers))
    all_filled = text_filled | choice_filled

    result = {
        "standard_fields": [f for f in standard_values if f in all_filled],
        "files": ["resume"] if resume_uploaded else [],
        "custom_questions": [q["name"] for q in questions if q["name"] in all_filled],
        "custom_questions_detected": len(questions),
    }

    summary = (
        f"Filled {len(result['standard_fields'])} contact field(s), "
        f"{len(result['files'])} file(s), "
        f"{len(result['custom_questions'])}/{result['custom_questions_detected']} question(s)."
    )
    _inject_review_banner(page, summary)
    return result
