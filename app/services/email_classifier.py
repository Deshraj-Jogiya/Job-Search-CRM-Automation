"""
Sorts a job-search email into one kind by fixed rules, with no AI and no paid calls (email_sync_service.py).

Kinds: confirmation, rejection, interview, assessment, offer, reply (a person writing back), ignore.
The phrase lists come from the real mailbox (60 days to 2026-11-19: 1,987 messages, about 160 about applications).
Order matters:
  1. noise senders (bank alerts, newsletters, job-alert digests) and career-site account mail (codes, passwords)
     are ignored outright;
  2. offer, then rejection: rejections usually open with "thank you for applying", so they are checked before
     confirmations;
  3. assessment and interview only count from a sentence that asks for something now, never from the
     "if you are selected we will contact you for an interview" line most confirmations carry;
  4. confirmation;
  5. a person (not a no-reply address) at a tracked company, or someone already contacted, is a reply.
Text is matched with line breaks joined, because plain-text emails are hard-wrapped mid-phrase ("another candidate
they'd prefer to move forward with at / the moment"). Each result says which phrase decided it.

The same pass pulls out what the matcher needs: the company as the email names it, hints from the sender address
(a Workday or iCIMS tenant, the company's own domain), the role, a job ID and any deadline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .email_reader_service import RawEmail

# Applicant-tracking, scheduling, assessment and job-board senders. A message from one of these is a job email even
# when the subject is only "Thank You"; the company comes from the display name, the address or the text.
ATS_DOMAINS = (
    "greenhouse-mail.io", "greenhouse.io", "ashbyhq.com", "myworkday.com", "myworkdayjobs.com", "workday.com",
    "smartrecruiters.com", "icims.com", "workablemail.com", "workable.com", "successfactors.com", "successfactors.eu",
    "pinpoint.email", "pinpointhq.com", "paylocity.com", "rippling.com", "lever.co", "jobvite.com", "taleo.net",
    "taleo.com", "oraclecloud.com", "breezy.hr", "jazzhr.com", "applytojob.com", "bamboohr.com", "recruitee.com",
    "personio.de", "personio.com", "teamtailor.com", "dover.com", "gem.com", "eightfold.ai", "phenompeople.com",
    "avature.net", "ultipro.com", "ukg.com", "adp.com", "brassring.com", "csod.com", "dayforce.com", "silkroad.com",
    "coderbyte.com", "hackerrank.com", "codesignal.com", "codility.com", "testgorilla.com", "hirevue.com",
    "criteriacorp.com", "karat.com", "hackerearth.com", "goodtime.io", "calendly.com", "modernloop.io",
    "linkedin.com", "indeed.com", "indeedemail.com",
)
ASSESSMENT_DOMAINS = ("coderbyte.com", "hackerrank.com", "codesignal.com", "codility.com", "testgorilla.com",
                      "hirevue.com", "criteriacorp.com", "karat.com", "hackerearth.com")
# Oracle Cloud sends from workflow.mail.<region>.cloud.oracle.com with a pod code as the local part.
_ORACLE_RE = re.compile(r"(^|\.)workflow\.(e?mail)\.[\w-]+\.(oraclecloud\.com|cloud\.oracle\.com)$|fa\.sender", re.I)
# Senders that use job words ("application", "offer", "next steps") about something else.
NOISE_DOMAINS = (
    "capitalone.com", "healthcare.gov", "healthsherpa.com", "usps.com", "substack.com", "americanexpress.com",
    "jobscan.co", "breakinto.tech", "antidotehealth.com", "talentratech.com", "chase.com", "citi.com",
    "experian.com", "transunion.com", "creditkarma.com", "beehiiv.com", "jobright.ai", "optimhire.com",
    "skills.network", "coursera.org", "leetcode.com", "edureka.co", "walgreens.com", "stockx.com",
    "bankofamerica.com", "zoom.us", "github.com", "qemailserver.com", "jobs2web.com", "splendidtechitsolutions.com",
    "glassdoor.com", "ziprecruiter.com", "dice.com", "wellfound.com", "joinhandshake.com", "handshake.com",
    "usajobs.gov", "nurture.icims.com", "alert.jobs", "aubank.in", "reebok.com",
)
# Career-site account mail: one-time codes, password resets, account verification. Never a status change, and stored
# with no excerpt, since the text carries the codes.
ACCOUNT_RE = re.compile(r"\b(password|passcode|one-time|security code|access code|verification code|verify your|"
                        r"confirm your (identity|email)|multifactor|two-factor|sign-in code|login code|event id)\b", re.I)
# Job alerts and digests: about many jobs, not one application.
_DIGEST_RE = re.compile(
    r"\b(job alerts?|jobs? (you may|for you|picked|recommended)|new jobs?|new roles|is hiring|are hiring|just posted|"
    r"job opportunities at|weekly digest|daily digest|newsletter|shared with you|we'd love to hear|how has your|"
    r"application experience|survey|webinar|you're registered)\b", re.I)
_NOREPLY_RE = re.compile(r"(no-?reply|do-?not-?reply|donotreply|notif|mailer|system|^careers?$|^jobs$|^talent|"
                         r"recruiting|^hiring|^hr$|myhr|^apply|^team$|^info$|support|autoreply|sender)", re.I)

OFFER_PHRASES = (
    "pleased to offer you", "happy to offer you", "delighted to offer you", "excited to offer you",
    "extend an offer", "extend you an offer", "your offer letter", "attached offer letter", "formal offer",
    "verbal offer", "offer package", "offer of employment for the",
)
REJECTION_PHRASES = (
    "move forward with other candidates", "moving forward with other candidates", "move forward with another candidate",
    "moving forward with another candidate", "decided to move forward with", "pursue other candidates",
    "pursuing other candidates", "pursue another candidate", "decided not to move forward", "will not be moving forward",
    "won't be moving forward", "not be moving forward", "will not be progressing", "not be progressing",
    "not been selected", "were not selected", "was not selected", "not selected for", "not to proceed with your",
    "unable to offer you", "regret to inform", "sorry to inform", "we are unable to move", "different direction",
    "position is no longer", "role is no longer", "no longer considering", "no longer being considered",
    "recently filled", "has been filled", "have filled the position", "filled this position", "position has been closed",
    "requisition has been closed", "job has been closed", "candidates whose qualifications more closely",
    "closer match to the requirements", "not a fit at this time", "not the right fit", "decided to proceed with other",
    "chosen to proceed with other", "will not be considered", "do not meet the minimum", "does not meet the minimum",
    "won't be able to invite you", "will not be able to invite you", "unable to invite you", "not able to move forward",
    "won't be able to move forward", "not to move forward with your", "chosen not to move forward", "won't be proceeding",
    "will not be proceeding", "not be proceeding with", "not moving forward with your", "prefer to move forward with",
)
# Wording that varies too much for a fixed phrase.
_REJECTION_RES = (
    re.compile(r"\b(?:not|n't|unable to|no longer)\s+(?:be\s+|be able to\s+|able to\s+)?(?:move|moving|proceed|proceeding|"
               r"progress|progressing|advance|advancing)\s+(?:forward|further|with your)", re.I),
    re.compile(r"\b(?:another|other)\s+(?:candidate|applicant)s?\b.{0,40}?\b(?:prefer|selected|chosen|move forward|"
               r"moving forward|proceed)", re.I),
    re.compile(r"\b(?:prefer|chosen|decided|selected|elected)\s+to\s+(?:move forward|proceed|go)\s+with\s+(?:another|other|"
               r"a different)", re.I),
)
# Lines that also turn up in confirmations; alone they only make a low-confidence rejection.
WEAK_REJECTION_PHRASES = ("keep your resume on file", "keep your information on file", "unfortunately, we",
                          "unfortunately we", "more closely match", "better match")
ASSESSMENT_PHRASES = (
    "assessment", "coding challenge", "coding test", "online test", "technical test", "take-home", "take home",
    "hackerrank", "codesignal", "coderbyte", "codility", "testgorilla", "hirevue", "skills test", "cognitive test",
)
INTERVIEW_PHRASES = (
    "schedule an interview", "schedule your interview", "schedule a call", "schedule a time", "schedule a phone",
    "schedule a video", "schedule a virtual", "set up a time", "set up an interview", "set up a call", "find a time",
    "would love to talk", "would love to chat", "would like to talk", "would like to speak", "would love to speak",
    "invitation to interview", "interview invitation", "interview request", "interview is confirmed",
    "interview confirmed", "interview has been scheduled", "interview scheduled", "phone screen", "phone interview",
    "video interview", "recruiter screen", "your availability", "share your availability", "times that work",
    "select a time", "pick a time", "calendly.com", "goodtime.io", "modernloop", "next round", "onsite interview",
    "panel interview", "virtual onsite", "meet with the team",
)
_INVITE_RE = re.compile(r"\binvite you (?:to|for) (?:an? |the |your )?(?:interview|phone|video|virtual|call|conversation|"
                        r"chat|meet|speak|schedule|onsite|round|technical|first|initial|next step)", re.I)
CONFIRMATION_PHRASES = (
    "thank you for applying", "thanks for applying", "thank you for your application", "thanks for your application",
    "received your application", "receipt of your application", "application has been received",
    "application was received", "application has been submitted", "application was submitted",
    "successfully submitted", "successfully applied", "your application was sent", "application received",
    "thank you for submitting", "thanks for submitting", "thanks for your submission", "thank you for your submission",
    "thank you for your interest in", "thanks for your interest in", "we have received your", "we've received your",
    "we received your", "application submitted", "indeed application:", "application confirmation",
    "has been submitted", "for applying",
)
# Sentences that describe what *might* happen; they never make an email an interview or an assessment.
_CONDITIONAL_RE = re.compile(
    r"\b(if (you are|you're|your|we|selected|there|it|a|you)|should (you|your|we|there)|once (we|our)|in the event|"
    r"will (reach out|contact you|be in touch)|may (reach out|contact)|(may|can|could|might) (include|involve|consist)|"
    r"selection process|as part of (its|our|the) (selection|hiring|recruiting)|might|whether|qualified candidates|"
    r"selected candidates|candidates (who|that)|next steps? (would|will|may)|what happens next|typically|"
    r"our process|hiring process (includes|consists|may)|only be able|select few|in the meantime|keep an eye|"
    r"explore our|feel free|stages)\b", re.I)
# Reminders sent for an application that was started; they never say it was sent.
_UNFINISHED_RE = re.compile(
    r"\b(if you have (completed|finished|submitted) (the|your) application|still working on (the|your) application|"
    r"complete (it|your application) here|finish (your|the) application|your application is (not yet complete|incomplete))\b",
    re.I)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
# A rejection phrase inside "if you don't complete it by Friday we will not move forward" is a condition, not a decision.
_IF_RE = re.compile(r"\b(if|unless|should you|in the event|otherwise|submissions that|those who|anyone who|candidates who)\b", re.I)
_STATUS_LEAD_RE = re.compile(r"^(?:re:\s*|fwd?:\s*)?(?:update|updates|thank|thanks|regarding|your|status|important|news|"
                             r"application|follow|got it|we|action|next|keep|recent|congrat)\b", re.I)

_JOB_ID_RE = re.compile(
    r"\b(?:job\s*(?:id|number|no\.?|#|req(?:uisition)?)|req(?:uisition)?\s*(?:id|number|no\.?|#)?|"
    r"position\s*(?:id|number)|posting\s*id|reference\s*(?:number|no\.?|id))\s*[:#-]?\s*([A-Z]{0,4}[-_]?\d[\w-]{2,})",
    re.I)
_LINKEDIN_VIEW_JOB_RE = re.compile(r"\bView job:?\s*https?://(?:www\.)?linkedin\.com/(?:comm/)?jobs/view/(\d{8,})", re.I)
_PAREN_ID_RE = re.compile(r"\(\s*(?:ID|Job ID|Req|JR)\s*[:#]?\s*([A-Z]{0,4}\d[\w-]{2,})\s*\)", re.I)
_CODE_ID_RE = re.compile(r"\b((?:JR|R|REQ|JR-|R-|REQ-)\d{4,}(?:-\d+)?)\b")
_DUE_RE = re.compile(
    r"\b(?:within|in the next)\s+(\d{1,3})\s*(?:business\s+)?(days?|hours?)\b|"
    r"\b(?:by|before|due(?: by| on)?|deadline(?: is|:)?|expires?(?: on)?)\s+"
    r"((?:mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+)?((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?)",
    re.I)

_DISPLAY_TEAM_RE = re.compile(r"^(?:the\s+)?(.+?)\s+(?:hiring|recruiting|talent(?:\s+acquisition)?|careers?|recruitment|"
                              r"global talent acquisition|hr)\s*(?:team|teams|no reply)?$", re.I)
_DISPLAY_TEAM_AT_RE = re.compile(r"^(?:the\s+)?(?:recruiting|hiring|talent(?:\s+acquisition)?|careers?)\s+team\s+(?:at|@)\s+(.+)$", re.I)
_DISPLAY_JUNK_RE = re.compile(r"\b(my)?workday\b|\bhr\b(?=workday)|\bno ?reply\b|\bnotifications?\b|\bgreenhouse\b|"
                              r"\bashby\b|\bworkable\b|\bsmartrecruiters\b|\bicims\b|\blever\b|\bhuman resources\b|"
                              r"\bcandidate\b|\bsystem\b", re.I)
_NAME = r"([A-Z0-9][\w&.'’-]*(?:\s+(?:&\s+)?[A-Z0-9(][\w&.'’()-]*){0,6})"
_TEXT_COMPANY_RES = (
    re.compile(r"your application was sent to\s+(.+?)(?:\s*$|\s*\n)", re.I | re.M),
    re.compile(r"\b(?:applying|applied|apply|application)\s+(?:to|with|at)\s+(?:the\s+)?(?:team at\s+)?" + _NAME + r"(?=\s*(?:[!.,\n]|$|\s+(?:for|and|we|as|-|is|has)\b))"),
    re.compile(r"\binterest in\s+(?:joining\s+(?:the team at\s+|us at\s+)?|working (?:at|with|for)\s+)?" + _NAME + r"(?=\s*(?:[!.,\n]|$|\s+(?:for|and|we|as|-|is|has)\b))"),
    re.compile(r"\b(?:role|position)\s+(?:here\s+)?at\s+" + _NAME + r"(?=\s*(?:[!.,\n]|$|\s+(?:for|and|we|as|-|is|has)\b))"),
    re.compile(r"\bcareers? (?:at|with)\s+" + _NAME + r"(?=\s*(?:[!.,\n]|$))"),
)
_SUBJECT_COMPANY_RES = (
    re.compile(r"^\[?(.+?)\]?\s*(?:[|:–]|\s-\s)\s*(?:thank|thanks|your application|application|we've received|we have received)", re.I),
    re.compile(r"(?:applying|applied|application)\s+(?:to|with|at|from)\s+(?:the team at\s+)?(.+?)(?:\s*[!.]|\s+-\s|\s+for\b|$)", re.I),
    re.compile(r"\binterest in\s+(?:joining\s+(?:the team at\s+)?)?(.+?)(?:\s*[!.]|\s+-\s|$)", re.I),
    re.compile(r"\b(?:thank you|thanks)\s+from\s+(.+?)(?:\s+-\s|\s*[!.]|$)", re.I),
    re.compile(r"\bfollow-up from\s+(.+?)$", re.I),
    re.compile(r"\b(?:received|update) (?:by|from)\s+(.+?)$", re.I),
    re.compile(r"\bsent to\s+(.+?)$", re.I),
    re.compile(r"\s(?:at|@)\s+([A-Z][\w&.'’ -]{1,40})$"),
    re.compile(r"^(?:your\s+)?(.+?)\s+(?:application|careers)\b", re.I),
)
_ASSESSMENT_COMPANY_RE = re.compile(r"\b(?:from|by|with)\s+([A-Z][\w&.'’ -]{1,40}?)(?:\s+(?:general\s+)?assessment|\s*$|\s+at\b|\s+within\b)")
# "Quarry Labs has invited you to complete an assessment": how HackerRank and Coderbyte invitations often open.
_YOUR_ASSESSMENT_RE = re.compile(r"\byour\s+([A-Z][\w&.'’ -]{1,40}?)\s+(?:online\s+|coding\s+|technical\s+)?assessment\b")
_GENERIC_ONLY = {"services", "service", "workday", "talent", "acquisition", "hr", "recruiting", "careers", "jobs", "team",
                 "hiring", "notifications", "no", "reply", "noreply", "the", "human", "resources", "people"}
_INVITED_BY_RE = re.compile(r"(?:^|[.!\n]\s*|,\s+)([A-Z][\w&.'’ -]{1,40}?)\s+(?:has|have)\s+invited you\b")
_ROLE_RES = (
    re.compile(r"\b(?:for|to) the (?:role|position) of\s+[\"“']?(.+?)[\"”']?(?=\s+(?:with|at|here|-|\(|has|is|was|and|we)\b|\s*[.,!(\n]|$)", re.I),
    re.compile(r"\b(?:for|to|in) (?:the|our)\s+[\"“']?([A-Z][^\n.!]{2,90}?)[\"”']?\s+(?:position|role|opening|job)\b"),
    re.compile(r"\bapplication (?:for|to)\s+(?:the\s+)?[\"“']?([A-Z][^\n.!]{2,90}?)[\"”']?\s+(?:position|role|opening|job)\b"),
    re.compile(r"\bapplication for\s+(?:the\s+)?[\"“']?([A-Z][^\n.!]{2,90}?)[\"”']?(?=\s*(?:\(|,|\.|!|\n| at | with | - |$))"),
    re.compile(r"\bapplying (?:for|to)\s+(?:the\s+)?[\"“']?([A-Z][^\n.!]{2,90}?)[\"”']?(?=\s*(?:\(|,|\.|!|\n| at | with | - | role|$))"),
    re.compile(r"\bthe position\s+([A-Z][^\n.!,]{2,90}?)(?=\s*(?:,|\.|!|\n| at | with |$))"),
)
_ROLE_WORD_RE = re.compile(
    r"\b(engineer|engineering|analyst|scientist|developer|architect|manager|specialist|associate|intern|consultant|"
    r"lead|programmer|administrator|researcher|technician|designer|coordinator|fellow|officer|director|"
    r"strategist|operations|analytics)\b", re.I)
_GENERIC_NAMES = {"us", "our team", "the team", "team", "the company", "the role", "this role", "this position",
                  "the position", "our company", "you", "it", "them", "the opportunity", "a career", "careers", "linkedin",
                  "indeed", "application", "applications", "your application", "job application",
                  "hiring team", "talent acquisition", "human resources", "hr", "recruiting", "keep track of",
                  "important information regarding"}
_TENANT_SKIP = {"jobs-noreply", "indeedapply", "notify", "noreply-jobs", "no", "noreply", "no-reply", "donotreply", "do-not-reply", "notifications", "notification", "system",
                "careers", "jobs", "talent", "recruiting", "email", "mail", "info", "candidates", "hr", "autoreply"}


@dataclass
class Classification:
    kind: str  # confirmation | rejection | interview | assessment | offer | reply | ignore
    confidence: str  # high | medium | low
    reason: str
    company: str | None = None
    role: str | None = None
    job_ref: str | None = None
    due: str | None = None
    via_board: str | None = None  # "linkedin" / "indeed" when the message is the board's own receipt
    hints: list[str] = field(default_factory=list)  # sender-address names to try when the company text is missing
    account_mail: bool = False  # codes and passwords: kept with no excerpt


def domain_matches(domain: str, suffixes) -> bool:
    domain = (domain or "").lower()
    return any(domain == s or domain.endswith("." + s) for s in suffixes)


def is_ats_sender(raw: RawEmail) -> bool:
    return domain_matches(raw.sender_domain, ATS_DOMAINS) or bool(_ORACLE_RE.search(raw.sender_domain + " " + raw.sender_email))


def is_noise_sender(raw: RawEmail) -> bool:
    return domain_matches(raw.sender_domain, NOISE_DOMAINS)


def is_person(raw: RawEmail) -> bool:
    local = raw.sender_email.split("@", 1)[0]
    return bool(local) and not _NOREPLY_RE.search(local) and not is_ats_sender(raw)


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def sender_hints(raw: RawEmail) -> list[str]:
    """Names the sender address carries: the tenant of a shared ATS ("micron@myworkday.com", "schwab+autoreply@
    talent.icims.com") or the company's own domain label ("chris@carvana.com" -> "carvana")."""
    local = raw.sender_email.split("@", 1)[0].lower()
    hints = []
    if is_ats_sender(raw):
        tenant = re.split(r"[+._]", local)[0]
        if tenant and tenant not in _TENANT_SKIP and len(tenant) >= 2 and not _ORACLE_RE.search(raw.sender_email):
            hints.append(tenant)
        return hints
    labels = [p for p in raw.sender_domain.split(".") if p]
    for label in labels[:-1]:  # drop the top-level domain
        if label not in {"mail", "email", "e", "em", "news", "noreply", "careers", "jobs", "recruiting", "talent", "hr",
                         "notification", "notifications", "talentacquisition", "workflow", "oraclecloud", "us", "info", "go", "t",
                         "co", "com", "org", "net", "gmail", "googlemail", "outlook", "hotmail", "live", "yahoo", "icloud",
                         "me", "aol", "proton", "protonmail", "example"} and len(label) >= 2:
            hints.append(label)
    return hints


def _find(text: str, phrases) -> str | None:
    return next((p for p in phrases if p in text), None)


def _actionable(text: str, phrases=(), pattern: re.Pattern | None = None) -> str | None:
    """First phrase that sits in a sentence asking for something now, skipping conditional 'if selected' lines."""
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if _CONDITIONAL_RE.search(sentence):
            continue
        hit = _find(sentence, phrases)
        if hit:
            return hit
        if pattern is not None:
            m = pattern.search(sentence)
            if m:
                return m.group(0).lower()
    return None


def _tidy_name(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"\s+", " ", value).strip(" \t\"'“”‘’.,!:;-|[]")
    value = re.sub(r"^(?:the\s+|team at\s+|joining\s+(?:the team at\s+)?)", "", value, flags=re.I)
    value = re.sub(r"\s+(?:application received|application|careers|jobs|recruiting|talent acquisition)$", "", value, flags=re.I)
    value = re.sub(r"(?:,?\s+(?:inc|llc|ltd|corp|corporation|co)\.?)$", "", value, flags=re.I)
    value = value.strip(" .,!-&")
    if not value or value.lower() in _GENERIC_NAMES or len(value) > 70 or len(value) < 2:
        return None
    return value


def _company_ok(value: str | None) -> str | None:
    value = _tidy_name((value or "").split(": ", 1)[0])  # "Torc Robotics: Important information regarding your"
    if value and set(re.findall(r"[a-z]+", value.lower())) <= _GENERIC_ONLY:
        return None  # "Workday Services", "Talent Acquisition": a sender label, not a company
    if value and (_ROLE_WORD_RE.search(value) or re.search(r"\d{4,}", value) or _STATUS_LEAD_RE.match(value)):
        return None  # a role or a requisition number, not a company
    return value


def extract_company(raw: RawEmail) -> str | None:
    name = (raw.sender_name or "").strip()
    subject = flat(raw.subject)
    if domain_matches(raw.sender_domain, ASSESSMENT_DOMAINS):
        text = flat(raw.text)[:600]
        for pattern in (_ASSESSMENT_COMPANY_RE, _INVITED_BY_RE, _YOUR_ASSESSMENT_RE):
            m = pattern.search(subject) or pattern.search(text)
            if m and _company_ok(m.group(1)):
                return _company_ok(m.group(1))
        return None
    if name and "@" not in name:
        m = _DISPLAY_TEAM_AT_RE.match(name) or _DISPLAY_TEAM_RE.match(name)
        if m and _company_ok(m.group(1)):
            return _company_ok(m.group(1))
        if is_ats_sender(raw) and not domain_matches(raw.sender_domain, ("linkedin.com", "indeed.com", "indeedemail.com")):
            cleaned = _company_ok(_DISPLAY_JUNK_RE.sub(" ", re.sub(r"(?:hr)?(?:my)?workday", " ", name, flags=re.I)))
            if cleaned and len(cleaned) >= 3:
                return cleaned
    for pattern in _SUBJECT_COMPANY_RES:
        m = pattern.search(subject)
        if m and _company_ok(m.group(1)):
            return _company_ok(m.group(1))
    text = flat(raw.text)[:1500]
    for pattern in _TEXT_COMPANY_RES:
        m = pattern.search(text)
        if m and _company_ok(m.group(1)):
            return _company_ok(m.group(1))
    return None


def _tidy_role(value: str | None) -> str | None:
    role = _tidy_name(value)
    if not role:
        return None
    role = re.sub(r"^(?:re:\s*)?(?:your application status for|regarding your (?:job )?application (?:for|to|at)|"
                  r"update (?:on|regarding) your application for|application status update for)\s+", "", role, flags=re.I)
    role = re.sub(r"^(?:[\w&.-]+'s\s+)", "", role)  # "Vestwell's Data Scientist role"
    role = role.replace("&nbsp", "").strip()
    role = re.sub(r"\s+(?:role|position)$", "", role, flags=re.I)
    role = re.sub(r"\s*[-–(]\s*(?:job\s*)?(?:id|req|number)\b.*$", "", role, flags=re.I)
    role = re.sub(r"^(?:JR|R|REQ)-?\d{4,}\s+", "", role)
    role = re.sub(r"\s*\((?:open|closed)\)$", "", role, flags=re.I).strip(" -")
    role = re.sub(r"\s+[-–—]\s+[A-Z]{0,4}[-_]?\d{5,}$", "", role).strip(" -")  # Workday "BI Developer – 31168177"
    return role if role and _ROLE_WORD_RE.search(role) and len(role.split()) <= 14 else None


def extract_role(raw: RawEmail) -> str | None:
    head = flat(raw.subject) + " \n " + flat(raw.text)[:1500]
    for pattern in _ROLE_RES:
        for m in pattern.finditer(head):
            role = _tidy_role(m.group(1))
            if role:
                return role
    subject = flat(raw.subject)
    for pattern in (r"(?:submission to|application for|applied for|applying for|application to|received for:?)\s+(.+?)(?:\s+(?:position|role)\b|$|\s+-\s+JR)",
                    r"^(.+?)\s+at\s+[A-Z]", r"\s[-–|]\s([^-–|]+)$", r"^[^-–|]+?\s[-–]\s(.+?)\s[-–]"):
        m = re.search(pattern, subject, re.I)
        if m and _tidy_role(m.group(1)):
            return _tidy_role(m.group(1))
    return None


def extract_job_ref(raw: RawEmail) -> str | None:
    head = flat(raw.subject) + " \n " + flat(raw.text)[:2000]
    # A LinkedIn receipt names the job only by its link ("View job: .../jobs/view/4492028223/"); that ID is in the
    # posting URL the CRM saved, so it settles which application it is.
    m = _LINKEDIN_VIEW_JOB_RE.search(flat(raw.text)[:4000])
    if m:
        return m.group(1)
    for pattern in (_PAREN_ID_RE, _JOB_ID_RE, _CODE_ID_RE):
        m = pattern.search(head)
        if m:
            return m.group(1).strip("-_")
    m = re.search(r"\b(\d{5,})\b", flat(raw.subject))  # IBM "job application - 134985 - ...", AA "Job ID: 88908"
    return m.group(1) if m else None


def extract_due(text: str) -> str | None:
    m = _DUE_RE.search(text)
    if not m:
        return None
    if m.group(1):
        return f"within {m.group(1)} {m.group(2)}"
    return m.group(4).strip()


def classify(raw: RawEmail, known_person: bool = False, tracked_company_sender: bool = False) -> Classification:
    """`known_person`: the sender is someone already contacted (OutreachMessage) or a saved recruiter.
    `tracked_company_sender`: the sender's domain belongs to a company with an application."""
    subject = flat(raw.subject).lower()
    body = flat(raw.text).lower()
    text = subject + " . " + body
    hints = sender_hints(raw)

    if is_noise_sender(raw) and not known_person:
        return Classification("ignore", "high", f"sender {raw.sender_domain} is on the noise list", hints=hints)
    if ACCOUNT_RE.search(subject):
        return Classification("ignore", "high", "career-site account email (code or password)", hints=hints,
                              account_mail=True)
    via_board = None
    if domain_matches(raw.sender_domain, ("linkedin.com",)):
        if "your application was sent to" in text:
            via_board = "linkedin"
        elif not known_person:
            return Classification("ignore", "high", "LinkedIn notification, not about one application", hints=hints)
    if domain_matches(raw.sender_domain, ("indeed.com", "indeedemail.com")):
        if "indeed application:" in subject:
            via_board = "indeed"
        elif not _find(text, REJECTION_PHRASES):
            return Classification("ignore", "high", "Indeed notification, not about one application", hints=hints)
    if not via_board and not known_person and _DIGEST_RE.search(subject):
        return Classification("ignore", "medium", "looks like a job alert, digest or survey", hints=hints)

    extras = dict(company=extract_company(raw), role=extract_role(raw), job_ref=extract_job_ref(raw), hints=hints)
    jobish = bool(is_ats_sender(raw) or tracked_company_sender or known_person or via_board)

    hit = _actionable(text, OFFER_PHRASES)
    if hit and (jobish or is_person(raw)):
        return Classification("offer", "high" if jobish else "medium", f"matched '{hit}'", **extras)

    strong = None
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if _IF_RE.search(sentence):
            continue
        strong = _find(sentence, REJECTION_PHRASES)
        if not strong:
            m = next((p.search(sentence) for p in _REJECTION_RES if p.search(sentence)), None)
            strong = m.group(0) if m else None
        if strong:
            break
    if strong:
        return Classification("rejection", "high" if jobish else "medium", f"matched '{strong}'", **extras)

    if domain_matches(raw.sender_domain, ASSESSMENT_DOMAINS):
        done = re.search(r"\b(being reviewed|have completed|you completed|thank you for (taking|completing))\b", text)
        return Classification("assessment", "high", f"assessment platform ({raw.sender_domain})"
                              + ("; assessment already done" if done else ""), due=extract_due(body), **extras)
    hit = _actionable(text, ASSESSMENT_PHRASES)
    if hit and jobish and re.search(r"\b(complete|take|invite|invited|link|next step|moving forward|move forward|start|deadline)\b", text):
        return Classification("assessment", "high" if is_ats_sender(raw) else "medium", f"matched '{hit}'",
                              due=extract_due(body), **extras)
    hit = _actionable(text, INTERVIEW_PHRASES, _INVITE_RE)
    if hit and (jobish or (is_person(raw) and "interview" in subject)):
        return Classification("interview", "high" if jobish else "medium", f"matched '{hit}'", due=extract_due(body), **extras)

    if _UNFINISHED_RE.search(text):
        # Amazon's "Keep track of your application": sent when an application is started, finished or not.
        return Classification("ignore", "medium", "reminder about an application that may not be finished", hints=hints)

    hit = _find(text, CONFIRMATION_PHRASES)
    if hit and (jobish or "applica" in subject or "applying" in subject or _find(subject, CONFIRMATION_PHRASES)):
        conf = "high" if (jobish and (extras["company"] or hints)) else "medium"
        return Classification("confirmation", conf, f"matched '{hit}'" + (f" ({via_board} receipt)" if via_board else ""),
                              via_board=via_board, **extras)

    weak = _find(text, WEAK_REJECTION_PHRASES)
    if weak and jobish:
        return Classification("rejection", "low", f"matched only '{weak}'", **extras)
    if (known_person or tracked_company_sender) and is_person(raw):
        return Classification("reply", "medium" if known_person else "low",
                              "someone you contacted wrote back" if known_person else "a person at a tracked company wrote",
                              **extras)
    return Classification("ignore", "medium", "no job-status phrase found", hints=hints)
