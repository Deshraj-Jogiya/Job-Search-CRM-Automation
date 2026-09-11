"""
b1.1: the one-page resume-fit auto-reduction loop. render → measure →
pick the next reduction by content_value_weight / measured_lines_saved
→ re-render → repeat, until the resume fits config's max_pages or
every reduction is exhausted (FAIL LOUDLY, never a silently-emitted
2-page resume -- see PageFitExhaustedError).

Two kinds of reduction, both declared in config/resume_rules.yaml's
page_fit.reduction_catalog:
  - CONTENT reductions (drop_weakest_bullet_per_role, shorten_summary,
    drop_third_project) mutate a COPY of resume_content before it's
    ever handed to the renderer -- see _apply_content_reduction.
  - VISUAL reductions (tighten_line_spacing, shrink_margins_one_step,
    shrink_body_font_one_step) are render PARAMETERS
    (document_render_service.render_resume_pdf's font/leading/margin
    kwargs) -- see _apply_visual_reduction. Neither ever touches
    Education, and drop_weakest_bullet_per_role never touches an
    entry's first bullet -- both hard rules enforced here, not left to
    convention.

measured_lines_saved is genuinely measured, not guessed: building the
exact same flow against one oversized reportlab frame reports the real
height a candidate resume/font/margin combination would need (see
_measure_flow_height_pt), so "how much did that reduction actually
save" is a real before/after delta in points, converted to lines via
the current line-leading. Each
observation updates a running average per reduction id via
adaptation_service (b1.4-style AdaptiveParameterValue pair: sum and
count, so the mean is exact, not an approximation) -- cold start (no
observations yet, anywhere) falls back to the catalog's own literal
YAML order, exactly as that file's own comment documents.
"""

import copy
import io
import json
import re
from dataclasses import dataclass, field

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate

from . import adaptation_service, document_render_service, resume_rules

_TALL_PAGE_HEIGHT_IN = 100  # an arbitrarily tall single "page" for height measurement, never actually rendered


class PageFitError(Exception):
    """Base class -- callers that only care about final success/failure
    can catch this alone."""


class PageFitExhaustedError(PageFitError):
    """Every reduction hit its floor (or was already applied) and the
    resume still overflows max_pages. Carries everything a human needs
    to manually trim: current estimated line count, the target, which
    reductions were tried, and the longest source bullets ranked so the
    obvious cut is obvious."""

    def __init__(self, lines_over: float, target_lines: float, reductions_applied: list[str], longest_bullets: list[str]):
        self.lines_over = lines_over
        self.target_lines = target_lines
        self.reductions_applied = reductions_applied
        self.longest_bullets = longest_bullets
        super().__init__(
            f"Resume still overflows by ~{lines_over:.1f} line(s) after every page-fit reduction was exhausted "
            f"(target ~{target_lines:.1f} lines/page). Reductions already applied: {reductions_applied or 'none'}. "
            f"Longest source bullets (trim one of these manually): {longest_bullets}"
        )


@dataclass
class _RenderParams:
    body_font_pt: float
    line_leading_pt: float
    margin_top_in: float
    margin_bottom_in: float
    margin_side_in: float
    applied_counts: dict = field(default_factory=dict)  # reduction_id -> times applied THIS run


def _measure_flow_height_pt(resume_content: dict, params: _RenderParams) -> float:
    """Builds the exact same flow render_resume_pdf would, against one
    arbitrarily tall frame, and reports the real height it consumed --
    a BaseDocTemplate build against an oversized single frame is a
    standard reportlab technique for "how tall would this content
    actually be" without needing real multi-page pagination to answer
    that question. Empirically verified (not assumed) that the built
    frame's own _y attribute after doc.build() reports exactly this."""
    content_width = letter[0] - 2 * params.margin_side_in * inch
    tall_height = _TALL_PAGE_HEIGHT_IN * inch
    buf = io.BytesIO()
    frame = Frame(
        0, 0, content_width, tall_height,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id="measure",
    )
    doc = BaseDocTemplate(buf, pagesize=(content_width, tall_height), pageTemplates=[PageTemplate(id="measure", frames=[frame])])
    flow = document_render_service.build_resume_flow(
        resume_content, body_font_pt=params.body_font_pt, line_leading_pt=params.line_leading_pt,
        content_width=content_width,
    )
    doc.build(flow)
    # BaseDocTemplate tracks consumed frame height via the frame's own
    # remaining space after the single build pass -- for a frame this
    # tall, everything fits on "page 1", so frame._y (the y-coordinate
    # writing stopped at, reportlab's bottom-up coordinate system) is
    # exactly tall_height minus however much content used.
    return tall_height - frame._y


def _usable_page_height_pt(params: _RenderParams) -> float:
    page_h = letter[1]
    return page_h - (params.margin_top_in + params.margin_bottom_in) * inch


def _lines_over(overflow_pt: float, params: _RenderParams) -> float:
    return overflow_pt / params.line_leading_pt


def _shorten_summary_text(summary: str, max_chars: int) -> str:
    """Keeps the first sentence only when there's more than one --
    never invents new text, just trims to what's already there. A
    single long run-on sentence falls back to a word-boundary cut near
    max_chars."""
    sentences = re.split(r"(?<=[.!?])\s+", summary.strip())
    if len(sentences) > 1:
        return sentences[0]
    if len(summary) <= max_chars:
        return summary
    truncated = summary[:max_chars].rsplit(" ", 1)[0]
    return truncated.rstrip(".,;: ") + "."


_CONTENT_REDUCTION_IDS = {"drop_weakest_bullet_per_role", "shorten_summary", "drop_third_project"}
_VISUAL_REDUCTION_IDS = {"tighten_line_spacing", "shrink_margins_one_step", "shrink_body_font_one_step"}


def _apply_content_reduction(resume_content: dict, reduction_id: str, config: dict) -> dict:
    """Returns a NEW resume_content dict with one content-level
    reduction applied on top -- never mutates the input, never touches
    Education, drop_weakest_bullet_per_role never touches an entry's
    FIRST bullet."""
    content = copy.deepcopy(resume_content)
    if reduction_id == "drop_weakest_bullet_per_role":
        for entry in content.get("experience", []):
            bullets = entry.get("bullets", [])
            if len(bullets) > 1:
                entry["bullets"] = bullets[:-1]  # the weakest bullet is the LAST one, never the first
    elif reduction_id == "shorten_summary":
        summary = content.get("summary") or ""
        if summary:
            content["summary"] = _shorten_summary_text(summary, config["page_fit"]["summary_shorten_max_chars"])
    elif reduction_id == "drop_third_project":
        projects = content.get("projects", [])
        if len(projects) > 2:
            content["projects"] = projects[:2]
    return content


def _apply_visual_reduction(params: _RenderParams, reduction_id: str, config: dict) -> _RenderParams:
    """Returns a NEW _RenderParams with one visual reduction applied,
    floor-bounded by config's page_fit section."""
    pf = config["page_fit"]
    p = _RenderParams(**{**params.__dict__, "applied_counts": dict(params.applied_counts)})
    if reduction_id == "tighten_line_spacing":
        floor = p.body_font_pt + 0.3  # leading below font size would visually overlap lines
        p.line_leading_pt = max(floor, p.line_leading_pt - pf["line_spacing_step_pt"])
    elif reduction_id == "shrink_margins_one_step":
        floor = pf["min_margin_in"]
        step = pf["margin_step_in"]
        p.margin_top_in = max(floor, p.margin_top_in - step)
        p.margin_bottom_in = max(floor, p.margin_bottom_in - step)
        p.margin_side_in = max(floor, p.margin_side_in - step)
    elif reduction_id == "shrink_body_font_one_step":
        floor = pf["min_body_font_pt"]
        step = pf["font_step_pt"]
        new_font = max(floor, p.body_font_pt - step)
        p.line_leading_pt = p.line_leading_pt - (p.body_font_pt - new_font)
        p.body_font_pt = new_font
    return p


def _is_reduction_exhausted(reduction_id: str, resume_content: dict, params: _RenderParams, config: dict) -> bool:
    pf = config["page_fit"]
    if reduction_id == "tighten_line_spacing":
        return params.line_leading_pt <= params.body_font_pt + 0.3
    if reduction_id == "shrink_margins_one_step":
        floor = pf["min_margin_in"]
        return params.margin_top_in <= floor and params.margin_bottom_in <= floor and params.margin_side_in <= floor
    if reduction_id == "shrink_body_font_one_step":
        return params.body_font_pt <= pf["min_body_font_pt"]
    if reduction_id == "drop_weakest_bullet_per_role":
        return not any(len(e.get("bullets", [])) > 1 for e in resume_content.get("experience", []))
    if reduction_id == "shorten_summary":
        return params.applied_counts.get(reduction_id, 0) >= 1  # single-shot
    if reduction_id == "drop_third_project":
        return len(resume_content.get("projects", [])) <= 2
    return True


def _predicted_lines_saved(db, reduction_id: str, config: dict) -> float | None:
    """Learned average lines-saved for one application of this
    reduction, across every past run that ever applied it -- None on
    cold start (never observed), which is what makes the caller fall
    back to the catalog's own literal YAML order for a type it has no
    history for yet (b3.4)."""
    count = adaptation_service.get_current_value(db, f"page_fit_sample_count:{reduction_id}", 0.0)
    if count <= 0:
        return None
    return adaptation_service.get_current_value(db, f"page_fit_avg_lines_saved:{reduction_id}", 0.0)


def _record_observed_savings(db, reduction_id: str, lines_saved: float) -> None:
    """Incremental running mean -- exact, not approximated, and cheap
    to update with just two persisted floats (sum-equivalent via
    count+average) per reduction id."""
    count = adaptation_service.get_current_value(db, f"page_fit_sample_count:{reduction_id}", 0.0)
    avg = adaptation_service.get_current_value(db, f"page_fit_avg_lines_saved:{reduction_id}", 0.0)
    new_count = count + 1
    new_avg = avg + (lines_saved - avg) / new_count
    adaptation_service.set_current_value(db, f"page_fit_sample_count:{reduction_id}", new_count)
    adaptation_service.set_current_value(db, f"page_fit_avg_lines_saved:{reduction_id}", new_avg)
    adaptation_service.log_adaptation(
        db, "tier1", "page_fit", reduction_id, avg, new_avg,
        triggering_evidence={"lines_saved_this_time": lines_saved}, sample_size=int(new_count), status="applied",
    )


def _longest_bullets(resume_content: dict, top_n: int = 5) -> list[str]:
    bullets = []
    for entry in resume_content.get("experience", []):
        bullets += entry.get("bullets", [])
    for proj in resume_content.get("projects", []):
        bullets += proj.get("bullets", [])
    return sorted(bullets, key=len, reverse=True)[:top_n]


def render_resume_pdf_with_fit(db, resume_content: dict, config: dict | None = None) -> dict:
    """The real page-fit loop. Returns {"pdf_bytes", "reductions_applied",
    "page_fit_ok"} on success. Raises PageFitExhaustedError (never
    returns a silently-overflowing PDF) once every reduction catalog
    entry is exhausted and the resume still doesn't fit."""
    if isinstance(resume_content, str):
        resume_content = json.loads(resume_content)
    config = config or resume_rules.get_config()
    pf = config["page_fit"]
    catalog = pf["reduction_catalog"]
    catalog_order = [entry["id"] for entry in catalog]
    weight_by_id = {entry["id"]: entry["content_value_weight"] for entry in catalog}

    content = copy.deepcopy(resume_content)
    params = _RenderParams(
        body_font_pt=8.7, line_leading_pt=10.3, margin_top_in=0.4, margin_bottom_in=0.35, margin_side_in=0.55,
    )
    reductions_applied: list[str] = []
    max_iterations = len(catalog) * 20  # generous cap -- guards against a logic error looping forever, not a real limit

    for _ in range(max_iterations):
        height_pt = _measure_flow_height_pt(content, params)
        overflow_pt = height_pt - _usable_page_height_pt(params) * pf["max_pages"]
        if overflow_pt <= 0:
            break

        candidates = [
            rid for rid in catalog_order
            if not _is_reduction_exhausted(rid, content, params, config)
        ]
        if not candidates:
            lines_over = _lines_over(overflow_pt, params)
            target_lines = _usable_page_height_pt(params) * pf["max_pages"] / params.line_leading_pt
            raise PageFitExhaustedError(lines_over, target_lines, reductions_applied, _longest_bullets(content))

        # Cold start (b3.4): no reduction anywhere has a learned average
        # yet -> fall back to the catalog's own literal order, exactly
        # as its own comment documents. Once ANY candidate has history,
        # pick by best (lowest) content_value_weight / predicted_lines_saved
        # ratio among candidates that HAVE history; a candidate with no
        # history yet still only gets picked once nothing with history
        # remains eligible, so "try it once to learn" naturally happens
        # in catalog order too.
        scored = []
        for rid in candidates:
            predicted = _predicted_lines_saved(db, rid, config)
            if predicted and predicted > 0:
                scored.append((weight_by_id[rid] / predicted, rid))
        if scored:
            scored.sort(key=lambda pair: pair[0])
            next_id = scored[0][1]
        else:
            next_id = candidates[0]  # first untried-anywhere candidate, in catalog order

        if next_id in _CONTENT_REDUCTION_IDS:
            new_content = _apply_content_reduction(content, next_id, config)
            new_params = params
        else:
            new_content = content
            new_params = _apply_visual_reduction(params, next_id, config)

        new_height_pt = _measure_flow_height_pt(new_content, new_params)
        lines_saved = (height_pt - new_height_pt) / params.line_leading_pt
        _record_observed_savings(db, next_id, max(0.0, lines_saved))

        new_params.applied_counts[next_id] = new_params.applied_counts.get(next_id, 0) + 1
        content, params = new_content, new_params
        reductions_applied.append(next_id)

    else:
        lines_over = _lines_over(overflow_pt, params)
        target_lines = _usable_page_height_pt(params) * pf["max_pages"] / params.line_leading_pt
        raise PageFitExhaustedError(lines_over, target_lines, reductions_applied, _longest_bullets(content))

    pdf_bytes = document_render_service.render_resume_pdf(
        content, body_font_pt=params.body_font_pt, line_leading_pt=params.line_leading_pt,
        margin_top_in=params.margin_top_in, margin_bottom_in=params.margin_bottom_in, margin_side_in=params.margin_side_in,
    )
    return {"pdf_bytes": pdf_bytes, "reductions_applied": reductions_applied, "page_fit_ok": True}
