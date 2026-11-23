"""The email inbox (email_reader_service, email_classifier, email_matcher, email_sync_service, /inbox).

Fixtures are shortened, anonymized versions of real job emails: the shapes the rules were tuned on.
"""

from datetime import datetime, timedelta

import pytest

from app.models import ActivityLog, EmailMessage, JobApplication, OutreachMessage, get_or_create_settings
from app.services import confirmation_service, email_classifier, email_matcher, email_reader_service, email_sync_service
from app.services.email_reader_service import EmailReaderError, RawEmail
from app.services.email_sync_service import EmailSyncError

from conftest import make_application, make_company, make_posting

T0 = datetime(2026, 11, 1, 15, 0)
_uid = iter(range(1, 10_000))


def mail(sender_email, subject, text="", name="", days=0, **kw) -> RawEmail:
    uid = next(_uid)
    return RawEmail(uid=uid, message_id=f"<t{uid}@example.com>", sender_name=name, sender_email=sender_email,
                    subject=subject, received_at=T0 + timedelta(days=days), text=text, **kw)


def kind(raw, **kw):
    return email_classifier.classify(raw, **kw).kind


# ---------------------------------------------------------------------------- classifier

class TestClassifier:
    def test_greenhouse_confirmation(self):
        raw = mail("no-reply@us.greenhouse-mail.io", "Thank you for applying to Whatnot",
                   "Hi D, Thank you for applying for the Data Engineer role at Whatnot. We have received your application.",
                   name="Whatnot Hiring Team")
        cls = email_classifier.classify(raw)
        assert cls.kind == "confirmation"
        assert cls.company == "Whatnot"

    def test_rejection_from_a_person(self):
        raw = mail("chris@carvana.com", "Re: Data Engineer",
                   "Thanks for your time with the team. We have decided we prefer to move forward with another candidate.",
                   name="Chris H")
        assert kind(raw, tracked_company_sender=True) == "rejection"

    def test_rejection_phrase_split_by_a_line_wrap(self):
        raw = mail("noreply@smartrecruiters.com", "Your application",
                   "After careful review we have decided to move forward\nwith other candidates for this position.")
        assert kind(raw) == "rejection"

    def test_if_sentence_is_not_a_rejection(self):
        raw = mail("no-reply@coderbyte.com", "Assessment from Wolverine Trading",
                   "Please complete the assessment within 7 days. Submissions that don't pass the tests will not move "
                   "forward in the process.")
        assert kind(raw) == "assessment"

    def test_conditional_interview_line_is_not_an_invite(self):
        raw = mail("no-reply@us.greenhouse-mail.io", "Thank you for applying to Acme",
                   "We received your application. If selected, a recruiter will reach out to schedule an interview.",
                   name="Acme Hiring Team")
        assert kind(raw) == "confirmation"

    def test_interview_request(self):
        raw = mail("sam@acme.com", "Next steps",
                   "I would love to talk with you about the role. Would you be available for a 30-minute call next week?",
                   name="Sam R")
        assert kind(raw, tracked_company_sender=True) == "interview"

    def test_offer(self):
        raw = mail("sam@acme.com", "Offer letter",
                   "We are pleased to offer you the position of Data Engineer. Your offer letter is attached.", name="Sam R")
        assert kind(raw, tracked_company_sender=True) == "offer"

    def test_password_and_code_mail_is_ignored_and_flagged(self):
        raw = mail("noreply@myworkday.com", "Your verification code",
                   "Your verification code is 123456. We are pleased to offer you secure sign-in.")
        cls = email_classifier.classify(raw)
        assert cls.kind == "ignore" and cls.account_mail

    def test_bank_alert_is_noise(self):
        raw = mail("alerts@capitalone.com", "We are pleased to inform you", "Your payment was received.")
        assert kind(raw) == "ignore"

    def test_assessment_company_from_invitation_line(self):
        raw = mail("no-reply@coderbyte.com", "You have a new assessment",
                   "Hi, Quarry Labs has invited you to complete a coding assessment for the Data Engineer role.")
        cls = email_classifier.classify(raw)
        assert cls.kind == "assessment" and cls.company == "Quarry Labs"

    def test_workday_tenant_hint(self):
        raw = mail("micron@myworkday.com", "Update on your application", "")
        assert "micron" in email_classifier.sender_hints(raw)

    def test_personal_mail_domains_give_no_hint(self):
        assert email_classifier.sender_hints(mail("someone@gmail.com", "hi")) == []

    def test_unrelated_mail_from_a_stranger_is_ignored(self):
        assert kind(mail("friend@example.org", "Dinner Friday?", "Are you free?")) == "ignore"


# ---------------------------------------------------------------------------- matcher

def _app(db, company, title, status, **kw):
    posting = make_posting(db, company, job_title=title, job_url=kw.pop("job_url", f"https://x/{title}"),
                           external_id=kw.pop("external_id", None))
    return make_application(db, posting, status=status, **kw)


def _match(db, raw):
    cls = email_classifier.classify(raw, known_person=raw.sender_email in email_matcher.known_people(db))
    return email_matcher.match(email_matcher.CompanyIndex(db), email_matcher.known_people(db), raw, cls)


class TestMatcher:
    def test_single_application_matches(self, db):
        app = _app(db, make_company(db, "Whatnot"), "Data Engineer", "Approved")
        raw = mail("no-reply@us.greenhouse-mail.io", "Thank you for applying to Whatnot",
                   "Thank you for applying for the Data Engineer role.", name="Whatnot Hiring Team")
        result = _match(db, raw)
        assert (result.state, result.application_id) == ("matched", app.id)

    def test_job_id_decides_between_two(self, db):
        c = make_company(db, "Amazon")
        _app(db, c, "Data Engineer I", "Applied", external_id="1111111")
        target = _app(db, c, "Data Engineer I", "Applied", external_id="2222222")
        raw = mail("no-reply@amazon.jobs", "Your application for Data Engineer I (2222222)",
                   "Thank you for applying to Amazon.", name="Amazon")
        assert _match(db, raw).application_id == target.id

    def test_sent_copy_wins_over_an_unsent_duplicate(self, db):
        c = make_company(db, "Lyft")
        _app(db, c, "Data Engineer", "Ingested")
        sent = _app(db, c, "Data Engineer", "Applied")
        raw = mail("no-reply@us.greenhouse-mail.io", "Your application to Lyft",
                   "Unfortunately we have decided to move forward with other candidates for the Data Engineer role.",
                   name="Lyft Hiring Team")
        result = _match(db, raw)
        assert (result.state, result.application_id) == ("matched", sent.id)

    def test_unknown_role_asks(self, db):
        c = make_company(db, "Torc Robotics")
        _app(db, c, "ML Engineer II - 3D Perception", "Applied")
        raw = mail("no-reply@us.greenhouse-mail.io", "Thank you for applying to Torc Robotics",
                   "Thank you for applying for the Data Analyst role at Torc Robotics.", name="Torc Robotics Hiring Team")
        assert _match(db, raw).state == "choose"

    def test_short_name_matches_the_full_one(self, db):
        app = _app(db, make_company(db, "Wolverine Trading"), "Data Engineer", "Applied")
        raw = mail("no-reply@us.greenhouse-mail.io", "Your application", "Unfortunately we will not be moving forward.",
                   name="Wolverine Hiring Team")
        assert _match(db, raw).application_id == app.id

    def test_generic_word_alone_never_matches(self, db):
        _app(db, make_company(db, "Banner Health"), "Data Engineer", "Applied")
        raw = mail("no-reply@us.greenhouse-mail.io", "Thank you for applying",
                   "Thank you for applying. We received your application.", name="Health Hiring Team")
        assert _match(db, raw).state == "unmatched"

    def test_known_person_points_to_their_application(self, db):
        c = make_company(db, "Carvana")
        _app(db, c, "Data Engineer", "Applied")
        target = _app(db, c, "Analytics Engineer", "Interviewing", recruiter_email="chris@carvana.com")
        raw = mail("chris@carvana.com", "Re: chat", "We prefer to move forward with another candidate.", name="Chris")
        assert _match(db, raw).application_id == target.id


# ---------------------------------------------------------------------------- sync, suggestions and actions

def _ingest(db, *emails):
    return email_sync_service.ingest(db, list(emails))


def _row(db, raw):
    return db.query(EmailMessage).filter(EmailMessage.message_id == raw.message_id).one()


def _ingest_waiting(db, *emails):
    """Ingest without the automatic step, so a rejection or confirmation stays on /inbox for the test to act on."""
    from unittest import mock
    with mock.patch.object(email_sync_service, "auto_accept_confirmations", return_value=0):
        return _ingest(db, *emails)


class TestIngestAndAccept:
    def test_unrelated_mail_is_not_stored(self, db):
        counts = _ingest(db, mail("friend@example.org", "Dinner Friday?", "Are you free?"))
        assert counts["stored"] == 0 and db.query(EmailMessage).count() == 0

    def test_account_mail_is_stored_without_its_text(self, db):
        raw = mail("noreply@myworkday.com", "Your verification code", "Your code is 123456.")
        _ingest(db, raw)
        row = _row(db, raw)
        assert row.state == "ignored" and row.excerpt is None

    def test_same_message_twice_is_stored_once(self, db):
        _app(db, make_company(db, "Whatnot"), "Data Engineer", "Approved")
        raw = mail("no-reply@us.greenhouse-mail.io", "Thank you for applying to Whatnot",
                   "Thank you for applying for the Data Engineer role.", name="Whatnot Hiring Team")
        _ingest(db, raw)
        _ingest(db, raw)
        assert db.query(EmailMessage).count() == 1

    def test_confirmation_marks_applied_on_the_email_date(self, db):
        app = _app(db, make_company(db, "Whatnot"), "Data Engineer", "Ingested")
        raw = mail("no-reply@us.greenhouse-mail.io", "Thank you for applying to Whatnot",
                   "Thank you for applying for the Data Engineer role.", name="Whatnot Hiring Team", days=-3)
        _ingest(db, raw)  # a clear employer confirmation is applied without a click
        db.refresh(app)
        assert app.status == "Applied" and app.applied_at == raw.received_at
        row = _row(db, raw)
        assert row.state == "done" and row.action_taken == email_sync_service.AUTO_LABEL

    def test_confirmation_that_could_be_several_applications_still_waits(self, db):
        c = make_company(db, "OpenAI")
        a1 = _app(db, c, "Data Engineer", "Approved")
        a2 = _app(db, c, "ML Engineer", "Approved")
        raw = mail("no-reply@openai.com", "Thank you for applying to OpenAI",
                   "Thank you for applying. We received your application.", name="OpenAI Hiring Team")
        _ingest(db, raw)
        db.refresh(a1), db.refresh(a2)
        assert _row(db, raw).state == "pending" and {a1.status, a2.status} == {"Approved"}

    def test_waiting_confirmations_from_before_are_applied_too(self, db):
        app = _app(db, make_company(db, "Whatnot"), "Data Engineer", "Approved")
        raw = mail("no-reply@us.greenhouse-mail.io", "Thank you for applying to Whatnot",
                   "Thank you for applying for the Data Engineer role.", name="Whatnot Hiring Team", days=-2)
        from unittest import mock
        with mock.patch.object(email_sync_service, "auto_accept_confirmations", return_value=0):
            _ingest(db, raw)
        assert _row(db, raw).state == "pending"
        assert email_sync_service.auto_accept_confirmations(db) == 1
        db.refresh(app)
        assert app.status == "Applied" and app.applied_at == raw.received_at

    def test_rejection_marks_not_selected_on_its_own(self, db):
        app = _app(db, make_company(db, "Lyft"), "Data Engineer", "Applied")
        raw = mail("no-reply@us.greenhouse-mail.io", "Your application to Lyft",
                   "Unfortunately we have decided to move forward with other candidates.", name="Lyft Hiring Team")
        _ingest(db, raw)
        db.refresh(app)
        assert app.status == "Not Selected" and app.not_selected_at == raw.received_at
        row = _row(db, raw)
        assert row.state == "done" and row.action_taken.startswith(email_sync_service.AUTO_REJECT_LABEL)

    def test_auto_rejection_names_the_people_contacted_there(self, db):
        app = _app(db, make_company(db, "Lyft"), "Data Engineer", "Applied")
        db.add(OutreachMessage(application_id=app.id, recipient_name="Pat Lee",
                               subject="Hi", body="Hello", status="Sent"))
        db.commit()
        raw = mail("no-reply@us.greenhouse-mail.io", "Your application to Lyft",
                   "Unfortunately we have decided to move forward with other candidates.", name="Lyft Hiring Team")
        _ingest(db, raw)
        assert "Pat Lee" in _row(db, raw).action_taken
        assert db.query(ActivityLog).filter(ActivityLog.message.contains("Pat Lee")).count() == 1

    def test_rejection_never_touches_an_offer(self, db):
        app = _app(db, make_company(db, "Lyft"), "Data Engineer", "Offer")
        raw = mail("no-reply@us.greenhouse-mail.io", "Your application to Lyft",
                   "Unfortunately we have decided to move forward with other candidates.", name="Lyft Hiring Team")
        _ingest(db, raw)
        db.refresh(app)
        assert app.status == "Offer"

    def test_rejection_that_could_be_several_applications_waits(self, db):
        c = make_company(db, "Torc Robotics")
        a = _app(db, c, "ML Engineer II - 3D Perception", "Applied")
        raw = mail("no-reply@us.greenhouse-mail.io", "Your application to Torc Robotics",
                   "Unfortunately we have decided to move forward with other candidates for the Data Analyst role.",
                   name="Torc Robotics Hiring Team")
        _ingest(db, raw)
        db.refresh(a)
        assert a.status == "Applied" and _row(db, raw).state == "pending"

    def test_interview_waits_for_a_click(self, db):
        app = _app(db, make_company(db, "Acme"), "Data Engineer", "Applied", recruiter_email="sam@acme.com")
        raw = mail("sam@acme.com", "Next steps", "I would love to talk. Would you be available for a call next week?",
                   name="Sam R")
        _ingest(db, raw)
        db.refresh(app)
        assert app.status == "Applied" and _row(db, raw).state == "pending"

    def test_rejection_is_not_selected_never_rejected(self, db):
        app = _app(db, make_company(db, "Lyft"), "Data Engineer", "Applied")
        raw = mail("no-reply@us.greenhouse-mail.io", "Your application to Lyft",
                   "Unfortunately we have decided to move forward with other candidates.", name="Lyft Hiring Team")
        _ingest_waiting(db, raw)
        email_sync_service.accept(db, _row(db, raw).id)
        db.refresh(app)
        assert app.status == "Not Selected" and app.not_selected_at == raw.received_at

    def test_rejection_for_an_unsent_application_records_applied_first(self, db):
        app = _app(db, make_company(db, "Lyft"), "Data Engineer", "Tailored")
        confirm = mail("no-reply@us.greenhouse-mail.io", "Thank you for applying to Lyft",
                       "Thank you for applying for the Data Engineer role.", name="Lyft Hiring Team", days=-10)
        reject = mail("no-reply@us.greenhouse-mail.io", "Your application to Lyft",
                      "Unfortunately we have decided to move forward with other candidates.", name="Lyft Hiring Team")
        _ingest(db, confirm, reject)  # the confirmation and the rejection both apply on their own
        db.refresh(app)
        assert app.status == "Not Selected"
        assert app.applied_at == confirm.received_at  # the earliest confirmation, not the rejection's date
        assert _row(db, confirm).state == "done" and _row(db, reject).state == "done"

    def test_confirmation_for_an_applied_application_needs_nothing(self, db):
        _app(db, make_company(db, "Whatnot"), "Data Engineer", "Applied")
        raw = mail("no-reply@us.greenhouse-mail.io", "Thank you for applying to Whatnot",
                   "Thank you for applying for the Data Engineer role.", name="Whatnot Hiring Team")
        _ingest(db, raw)
        assert _row(db, raw).state == "noted"
        assert email_sync_service.pending_count(db) == 0

    def test_interview_moves_applied_to_interviewing(self, db):
        app = _app(db, make_company(db, "Acme"), "Data Engineer", "Applied", recruiter_email="sam@acme.com")
        raw = mail("sam@acme.com", "Next steps", "I would love to talk. Would you be available for a call next week?",
                   name="Sam R")
        _ingest(db, raw)
        email_sync_service.accept(db, _row(db, raw).id)
        db.refresh(app)
        assert app.status == "Interviewing" and app.interviewing_at == raw.received_at

    def test_choose_needs_an_application_picked(self, db):
        c = make_company(db, "Torc Robotics")
        target = _app(db, c, "ML Engineer II - 3D Perception", "Applied")
        raw = mail("no-reply@us.greenhouse-mail.io", "Your application to Torc Robotics",
                   "Unfortunately we have decided to move forward with other candidates for the Data Analyst role.",
                   name="Torc Robotics Hiring Team")
        _ingest(db, raw)
        row = _row(db, raw)
        assert row.match_state == "choose"
        with pytest.raises(EmailSyncError):
            email_sync_service.accept(db, row.id)
        email_sync_service.accept(db, row.id, target.id)
        db.refresh(target)
        assert target.status == "Not Selected"

    def test_add_to_crm_creates_the_application(self, db):
        raw = mail("no-reply@us.greenhouse-mail.io", "Thank you for applying to ClassLink",
                   "Thank you for applying for the Data Analyst role at ClassLink.", name="ClassLink Hiring Team",
                   days=-2)
        _ingest(db, raw)
        row = _row(db, raw)
        assert row.match_state == "unmatched"
        app = email_sync_service.add_to_crm(db, row.id, "ClassLink", "Data Analyst")
        assert app.status == "Applied" and app.posting.source == "email"
        assert app.applied_at == raw.received_at

    def test_dismiss_and_restore(self, db):
        app = _app(db, make_company(db, "Lyft"), "Data Engineer", "Applied")
        raw = mail("no-reply@us.greenhouse-mail.io", "Your application to Lyft",
                   "Unfortunately we have decided to move forward with other candidates.", name="Lyft Hiring Team")
        _ingest_waiting(db, raw)
        row = _row(db, raw)
        email_sync_service.dismiss(db, row.id)
        db.refresh(app)
        assert app.status == "Applied" and _row(db, raw).state == "dismissed"
        email_sync_service.restore(db, row.id)
        assert _row(db, raw).state == "pending"

    def test_set_kind_changes_the_suggestion(self, db):
        app = _app(db, make_company(db, "Lyft"), "Data Engineer", "Applied")
        raw = mail("no-reply@us.greenhouse-mail.io", "Your application to Lyft",
                   "Unfortunately we have decided to move forward with other candidates.", name="Lyft Hiring Team")
        _ingest_waiting(db, raw)
        row = _row(db, raw)
        email_sync_service.set_kind(db, row.id, "interview")
        email_sync_service.accept(db, row.id)
        db.refresh(app)
        assert app.status == "Interviewing"

    def test_accept_twice_is_refused(self, db):
        _app(db, make_company(db, "Lyft"), "Data Engineer", "Applied")
        raw = mail("no-reply@us.greenhouse-mail.io", "Your application to Lyft",
                   "Unfortunately we have decided to move forward with other candidates.", name="Lyft Hiring Team")
        _ingest_waiting(db, raw)
        row_id = _row(db, raw).id
        email_sync_service.accept(db, row_id)
        with pytest.raises(EmailSyncError):
            email_sync_service.accept(db, row_id)

    def test_deleted_application_does_not_break_the_inbox(self, db):
        app = _app(db, make_company(db, "Lyft"), "Data Engineer", "Approved")
        raw = mail("no-reply@us.greenhouse-mail.io", "Thank you for applying to Lyft",
                   "Thank you for applying for the Data Engineer role.", name="Lyft Hiring Team")
        _ingest(db, raw)
        db.delete(app)
        db.commit()
        groups = email_sync_service.inbox(db)  # must not raise
        assert isinstance(groups, dict)

    def test_application_page_lists_its_emails(self, db):
        app = _app(db, make_company(db, "Whatnot"), "Data Engineer", "Approved")
        raw = mail("no-reply@us.greenhouse-mail.io", "Thank you for applying to Whatnot",
                   "Thank you for applying for the Data Engineer role.", name="Whatnot Hiring Team")
        _ingest(db, raw)
        assert len(email_sync_service.emails_for_application(db, app.id)) == 1


class TestMarkAppliedFromEmail:
    def test_accepts_any_pre_applied_status_with_the_given_date(self, db):
        c = make_company(db)
        when = datetime(2026, 10, 3, 9, 0)
        for status in ("Ingested", "Tailored", "Needs Review", "Pending Confirmation", "Approved", "Rejected"):
            app = _app(db, c, f"Data Engineer {status}", status)
            confirmation_service.mark_applied_from_email(db, app.id, at=when)
            db.refresh(app)
            assert app.status == "Applied" and app.applied_at == when and app.confirmation_deadline is None

    def test_refuses_an_application_already_sent(self, db):
        app = _app(db, make_company(db), "Data Engineer", "Interviewing")
        with pytest.raises(confirmation_service.ConfirmationServiceError):
            confirmation_service.mark_applied_from_email(db, app.id)

    def test_guarded_functions_take_a_date(self, db):
        app = _app(db, make_company(db), "Data Engineer", "Applied")
        when = datetime(2026, 10, 9, 12, 0)
        confirmation_service.mark_not_selected(db, app.id, at=when)
        db.refresh(app)
        assert app.not_selected_at == when


# ---------------------------------------------------------------------------- reading the mailbox

class FakeIMAP:
    """A Gmail-like server holding a few messages. Records every command; anything that could change mail fails."""

    def __init__(self, messages: dict[int, bytes], validity: int = 11):
        self.messages, self.validity, self.calls = messages, validity, []

    def login(self, user, password):
        self.calls.append(("LOGIN",))

    def select(self, mailbox, readonly=False):
        self.calls.append(("SELECT", mailbox, readonly))
        assert readonly, "the reader must only ever open a mailbox read-only (EXAMINE)"
        return "OK", [b"3"]

    def response(self, code):
        return code, [str(self.validity).encode()]

    def uid(self, command, *args):
        self.calls.append(("UID", command) + args)
        if command == "SEARCH":
            criterion = args[-1]
            uids = sorted(self.messages)
            if criterion.startswith("UID "):
                low = int(criterion.split()[1].split(":")[0])
                uids = [u for u in uids if u >= low]
            return "OK", [" ".join(map(str, uids)).encode()]
        if command == "FETCH":
            wanted = [int(u) for u in args[0].split(",")]
            rows = []
            for u in wanted:
                body = self.messages[u]
                if "HEADER.FIELDS" in args[1]:
                    head = body.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                    rows.append((f"{u} (UID {u} X-GM-THRID 1803000000000000{u} BODY[HEADER.FIELDS] {{1}}".encode(), head))
                else:
                    rows.append((f"{u} (UID {u} BODY[] {{1}}".encode(), body))
                rows.append(b")")
            return "OK", rows
        raise AssertionError(f"unexpected UID {command}")

    def store(self, *a):
        raise AssertionError("STORE would change flags")

    def logout(self):
        self.calls.append(("LOGOUT",))


def _rfc822(sender, subject, body, date="Sat, 01 Nov 2026 15:00:00 +0000", mid="<m@x>"):
    return (f"From: {sender}\r\nTo: me@example.com\r\nSubject: {subject}\r\nDate: {date}\r\nMessage-ID: {mid}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n").encode()


@pytest.fixture()
def imap_env(monkeypatch):
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USER", "me@example.com")
    monkeypatch.setenv("IMAP_PASSWORD", "test-app-password")


class TestReader:
    def _server(self):
        return FakeIMAP({
            5: _rfc822("Whatnot Hiring Team <no-reply@us.greenhouse-mail.io>", "Thank you for applying to Whatnot",
                       "Thank you for applying for the Data Engineer role.", mid="<a@x>"),
            6: _rfc822("Friend <friend@example.org>", "Dinner Friday?", "Are you free?", mid="<b@x>"),
            7: _rfc822("Lyft Hiring Team <no-reply@us.greenhouse-mail.io>", "Your application to Lyft",
                       "Unfortunately we have decided to move forward with other candidates.", mid="<c@x>"),
        })

    def test_reads_read_only_and_downloads_only_wanted_bodies(self, imap_env):
        server = self._server()
        emails, highest, validity = email_reader_service.fetch_new(
            None, datetime(2026, 10, 1).date(), None, lambda h: "greenhouse" in h.sender_email, connect=lambda: server)
        assert [e.uid for e in emails] == [5, 7] and highest == 7 and validity == 11
        assert "Thank you for applying" in emails[0].text
        assert emails[0].thread_id is not None
        body_fetches = [c for c in server.calls if c[:2] == ("UID", "FETCH") and "HEADER" not in c[3]]
        assert all("PEEK" in c[3] for c in server.calls if c[:2] == ("UID", "FETCH"))
        assert body_fetches and "6" not in body_fetches[0][2].split(",")

    def test_next_run_reads_only_newer_messages(self, imap_env):
        server = self._server()
        emails, _, _ = email_reader_service.fetch_new(7, datetime(2026, 10, 1).date(), 11, lambda h: True,
                                                      connect=lambda: server)
        assert emails == []
        search = next(c for c in server.calls if c[:2] == ("UID", "SEARCH"))
        assert search[-1] == "UID 8:*"

    def test_renumbered_mailbox_falls_back_to_the_date(self, imap_env):
        server = self._server()
        email_reader_service.fetch_new(7, datetime(2026, 10, 1).date(), 99, lambda h: True, connect=lambda: server)
        search = next(c for c in server.calls if c[:2] == ("UID", "SEARCH"))
        assert search[-1].startswith("SINCE")

    def test_wrapper_refuses_write_commands(self):
        wrapped = email_reader_service._ReadOnlyIMAP(FakeIMAP({}))
        with pytest.raises(EmailReaderError):
            wrapped.store("1", "+FLAGS", "\\Seen")
        with pytest.raises(EmailReaderError):
            wrapped.uid("STORE", "1", "+FLAGS", "\\Deleted")
        with pytest.raises(EmailReaderError):
            wrapped.uid("FETCH", "1", "(BODY[])")

    def test_not_configured_is_a_clear_error(self, monkeypatch):
        monkeypatch.delenv("IMAP_HOST", raising=False)
        with pytest.raises(EmailReaderError, match="isn't set up"):
            email_reader_service.fetch_new(None, datetime(2026, 10, 1).date(), None, lambda h: True,
                                           connect=lambda: FakeIMAP({}))

    def test_run_sync_end_to_end(self, db, imap_env):
        _app(db, make_company(db, "Lyft"), "Data Engineer", "Applied")
        server = self._server()
        counts = email_sync_service.run_sync(db, connect=lambda: server, force=True)
        settings = get_or_create_settings(db)
        assert settings.email_sync_last_uid == 7 and settings.email_sync_uidvalidity == 11
        assert settings.email_sync_last_error is None
        assert counts["suggested"] == 1  # the Whatnot confirmation offered to add; the Lyft rejection applied itself
        rows = {r.subject: r for r in db.query(EmailMessage).all()}
        assert rows["Your application to Lyft"].match_state == "matched" and rows["Your application to Lyft"].state == "done"
        assert rows["Thank you for applying to Whatnot"].match_state == "unmatched"
        assert db.query(EmailMessage).filter(EmailMessage.sender_email == "friend@example.org").count() == 0

    def test_run_sync_is_off_by_default(self, db, imap_env):
        assert get_or_create_settings(db).email_sync_enabled is False
        assert "skipped" in email_sync_service.run_sync(db, connect=lambda: self._server())

    def test_scheduler_entry_records_a_sign_in_failure(self, db, imap_env, monkeypatch):
        settings = get_or_create_settings(db)
        settings.email_sync_enabled = True
        db.commit()

        monkeypatch.setattr(email_reader_service, "_connect",
                            lambda connect=None: (_ for _ in ()).throw(EmailReaderError("Couldn't sign in")))
        email_sync_service.run_if_enabled(db)
        assert "Couldn't sign in" in (get_or_create_settings(db).email_sync_last_error or "")
        assert db.query(ActivityLog).filter(ActivityLog.message.like("%Email check failed%")).count() == 1


# ---------------------------------------------------------------------------- demo and pages

class TestShowcase:
    def test_run_sync_refused_in_showcase(self, db, monkeypatch):
        monkeypatch.setenv("APP_MODE", "showcase")
        with pytest.raises(EmailSyncError):
            email_sync_service.run_sync(db, force=True)

    def test_demo_seed_fills_a_sample_inbox(self, db, monkeypatch):
        monkeypatch.setenv("APP_MODE", "showcase")
        from app.services import demo_seed_service

        demo_seed_service.seed_demo_data(db)
        groups = email_sync_service.inbox(db)
        assert len(groups["suggested"]) >= 2 and len(groups["unmatched"]) >= 1
        assert db.query(EmailMessage).filter(EmailMessage.action_taken.like("Marked as%automatically%")).count() >= 1
        assert all(r.excerpt is None for r in db.query(EmailMessage).filter(EmailMessage.state == "ignored"))
        demo_seed_service.reset_demo_data(db)
        assert db.query(EmailMessage).count() == 6


def test_inbox_page_and_actions(db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.csrf import CSRF_COOKIE_NAME
    from app.main import app as web_app

    app = _app(db, make_company(db, "Lyft"), "Data Engineer", "Applied")
    raw = mail("no-reply@us.greenhouse-mail.io", "Your application to Lyft",
               "Unfortunately we have decided to move forward with other candidates.", name="Lyft Hiring Team")
    _ingest_waiting(db, raw)
    row_id = _row(db, raw).id
    client = TestClient(web_app)
    page = client.get("/inbox")
    assert page.status_code == 200 and "Lyft" in page.text and "Mark as Not Selected" in page.text
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert "Emails" in client.get(f"/jobs/{app.id}").text

    # the demo refuses every change
    monkeypatch.setenv("APP_MODE", "showcase")
    response = client.post(f"/inbox/{row_id}/accept", data={"csrf_token": token}, follow_redirects=False)
    assert "error=" in response.headers["location"]
    db.refresh(app)
    assert app.status == "Applied"

    monkeypatch.delenv("APP_MODE")
    response = client.post(f"/inbox/{row_id}/accept", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303 and "message=" in response.headers["location"]
    db.refresh(app)
    assert app.status == "Not Selected"
    response = client.post("/inbox/settings", data={"csrf_token": token, "email_sync_enabled": "true",
                                                    "email_sync_lookback_days": "30"}, follow_redirects=False)
    db.expire_all()
    settings = get_or_create_settings(db)
    assert settings.email_sync_enabled is True and settings.email_sync_lookback_days == 30


def test_migration_runs_on_sqlite_whether_or_not_the_table_already_exists(tmp_path):
    """create_all at startup may make email_messages before Alembic runs (the demo); the migration skips what exists,
    and a downgrade then upgrade builds it from nothing."""
    import os
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path

    from sqlalchemy import create_engine

    from app.database import Base

    repo = Path(__file__).resolve().parent.parent
    db_file = tmp_path / "m.db"
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    Base.metadata.create_all(engine)
    engine.dispose()
    con = sqlite3.connect(db_file)
    con.execute("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
    con.execute("DELETE FROM alembic_version")
    con.execute("INSERT INTO alembic_version VALUES ('a734a7bcb026')")
    con.commit()
    con.close()
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_file.as_posix()}"}
    for args in (["upgrade", "b5e3d8a1c7f4"], ["downgrade", "a734a7bcb026"], ["upgrade", "b5e3d8a1c7f4"]):
        run = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=repo, env=env, capture_output=True, text=True)
        assert run.returncode == 0, run.stderr[-2000:]
    con = sqlite3.connect(db_file)
    cols = {row[1] for row in con.execute("PRAGMA table_info(global_settings)")}
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "email_messages" in tables and {"email_sync_enabled", "email_sync_last_uid"} <= cols


class TestDryRunFixes:
    """Cases found running the inbox over two months of real mail."""

    def test_company_stops_at_a_colon(self):
        raw = mail("no-reply@torc.ai", "Torc Robotics: Important information regarding your application",
                   "Thank you for applying. We received your application.")
        assert email_classifier.classify(raw).company == "Torc Robotics"

    def test_sender_label_is_not_a_company(self):
        raw = mail("miamioh@myworkday.com", "JR104070 BI ETL Data Developer Application",
                   "Thank you for applying. We received your application.", name="Workday Services")
        assert email_classifier.classify(raw).company != "Services"

    def test_your_company_assessment(self):
        raw = mail("do-not-reply@criteriacorp.com", "Action needed: Complete your Eight Sleep assessment within 2 days",
                   "Please complete the assessment.", name="Hiring Team")
        assert email_classifier.classify(raw).company == "Eight Sleep"

    def test_old_interview_email_for_a_closed_application_is_history(self, db):
        app = _app(db, make_company(db, "Carvana"), "Data Product Engineer", "Not Selected",
                   recruiter_email="chris@carvana.com", not_selected_at=T0)
        raw = mail("chris@carvana.com", "Interview", "Would you be available for a call next week?", name="Chris",
                   days=-7)
        _ingest(db, raw)
        assert _row(db, raw).state == "noted"
        later = mail("chris@carvana.com", "Another role", "Would you be available for a call next week?", name="Chris",
                     days=3)
        _ingest(db, later)
        assert _row(db, later).state == "pending"

    def test_choose_where_nothing_would_change_is_noted(self, db):
        c = make_company(db, "OpenAI")
        _app(db, c, "Data Engineer", "Applied")
        _app(db, c, "ML Engineer", "Applied")
        raw = mail("no-reply@openai.com", "Thank you for applying to OpenAI",
                   "Thank you for applying. We received your application.", name="OpenAI Hiring Team")
        _ingest(db, raw)
        assert _row(db, raw).state == "noted"

    def test_bulk_accept_skips_a_duplicate_settled_by_the_first(self, db):
        app = _app(db, make_company(db, "Lyft"), "Data Engineer", "Applied")
        a = mail("no-reply@us.greenhouse-mail.io", "Your application to Lyft",
                 "Unfortunately we have decided to move forward with other candidates.", name="Lyft Hiring Team", days=-2)
        b = mail("no-reply@us.greenhouse-mail.io", "Your application to Lyft",
                 "Unfortunately we have decided to move forward with other candidates.", name="Lyft Hiring Team", days=-1)
        _ingest_waiting(db, a, b)
        done, errors = email_sync_service.accept_many(db, [_row(db, a).id, _row(db, b).id])
        db.refresh(app)
        assert (done, errors, app.status) == (1, [], "Not Selected") and app.not_selected_at == a.received_at


class TestLiveGapsNov21:
    """Real emails from 2026-11-21 that were misread or not tied to their application."""

    def test_selection_process_boilerplate_is_a_confirmation(self):
        raw = mail("noreply@icims.com", "Thank you for your application",
                   "Thank you for applying for the Data Engineer position at CommonSpirit. We have received your "
                   "application. Our selection process may include phone screens and interviews with the hiring team.",
                   name="CommonSpirit Talent Acquisition")
        assert kind(raw) == "confirmation"

    def test_linkedin_view_job_link_is_the_job_ref(self):
        raw = mail("jobs-noreply@linkedin.com", "Your application was sent to TikTok",
                   "Your application was sent to TikTok. Data Engineer View job: "
                   "https://www.linkedin.com/comm/jobs/view/4492028223/?trackingId=abc")
        assert email_classifier.extract_job_ref(raw) == "4492028223"

    def test_workday_number_after_the_title_is_not_part_of_the_role(self):
        assert email_classifier._tidy_role("BI Developer – 31168177") == "BI Developer"
        assert email_classifier._tidy_role("Data Engineer - JR-104070") == "Data Engineer"

    def test_role_named_in_one_title_decides(self, db):
        c = make_company(db, "Abbott")
        _app(db, c, "Sr. Data Engineer", "Approved")
        target = _app(db, c, "BI Developer (SQL)", "Approved")
        raw = mail("no-reply@us.greenhouse-mail.io", "Thank you for applying to Abbott",
                   "Thank you for applying for the BI Developer role at Abbott. We received your application.",
                   name="Abbott Hiring Team")
        assert _match(db, raw).application_id == target.id

    def test_short_role_contained_in_a_long_title(self, db):
        c = make_company(db, "TikTok")
        _app(db, c, "Machine Learning Engineer, Ads Ranking", "Approved")
        target = _app(db, c, "Data Engineer, Recommendation Architecture - USDS", "Approved")
        raw = mail("no-reply@us.greenhouse-mail.io", "Thank you for applying to TikTok",
                   "Thank you for applying for the Data Engineer role at TikTok. We received your application.",
                   name="TikTok Hiring Team")
        assert _match(db, raw).application_id == target.id

    def test_rejection_from_the_employers_own_address_applies(self, db):
        app = _app(db, make_company(db, "Vestwell"), "Data Scientist", "Applied")
        raw = mail("no-reply@vestwell.com", "Vestwell Application Update",
                   "Thank you for your interest in joining Vestwell. Our hiring team decided to move forward with other "
                   "candidates at this time for the Data Scientist role.")
        _ingest(db, raw)
        db.refresh(app)
        assert _row(db, raw).confidence == "medium" and app.status == "Not Selected"

    def test_exact_title_decides_and_the_sent_copy_wins(self, db):
        c = make_company(db, "First Citizens Bank")
        _app(db, c, "Data Engineer II - ETL/ELT", "Tailored")
        sent = _app(db, c, "Data Engineer II - ETL/ELT", "Applied")
        raw = mail("firstcitizens+autoreply@talent.icims.com", "Application Status from First Citizens Bank",
                   "Thank you for your recent application to the Data Engineer II - ETL/ELT position at First Citizens "
                   "Bank. We are sorry to inform you that you do not meet the minimum qualifications.")
        assert _match(db, raw).application_id == sent.id

    def test_full_title_written_in_the_email_decides(self, db):
        c = make_company(db, "IBM")
        target = _app(db, c, "Data Engineer-Data Platforms-Google - 5", "Applied")
        _app(db, c, "Data Engineer, IBM Quantum", "Applied")
        _app(db, c, "Data Engineer-Data Platforms-Google - 3", "Ingested")
        raw = mail("talent@ibm.com", "Your IBM Application Status",
                   "Ref: 134985 - Data Engineer-Data Platforms-Google - 5 Dear D, we have decided to move forward with "
                   "other candidates.", name="IBM Talent Acquisition")
        assert _match(db, raw).application_id == target.id

    def test_rejection_naming_no_role_goes_to_the_only_sent_application(self, db):
        c = make_company(db, "Charles Schwab")
        sent = _app(db, c, "AI Engineer, AI.x", "Applied")
        _app(db, c, "Machine Learning Ops Data Engineer", "Ingested")
        raw = mail("schwab+email@talent.icims.com", "Thank You",
                   "Thank you again for considering Charles Schwab. We have decided to pursue other candidates for this "
                   "position.", name="Charles Schwab Talent Acquisition")
        assert _match(db, raw).application_id == sent.id

    def test_rejection_naming_no_role_with_two_sent_still_asks(self, db):
        c = make_company(db, "IBM")
        _app(db, c, "Data Engineer-Data Platforms-Google - 5", "Applied")
        _app(db, c, "Data Engineer, IBM Quantum", "Applied")
        raw = mail("talent@ibm.com", "Your IBM Application Status",
                   "Dear D, we have decided to move forward with other candidates.", name="IBM Talent Acquisition")
        assert _match(db, raw).state == "choose"


class TestFirstRunChoiceAndRuns:
    def _signup(self, choice):
        from fastapi.testclient import TestClient

        from app.csrf import CSRF_COOKIE_NAME
        from app.main import app as web_app

        client = TestClient(web_app)
        page = client.get("/signup")
        assert "Read job emails?" in page.text
        token = client.cookies.get(CSRF_COOKIE_NAME)
        form = {"csrf_token": token, "username": "me", "password": "longpassword1", "confirm_password": "longpassword1"}
        if choice is not None:
            form["email_sync_enabled"] = choice
        return client.post("/signup", data=form, follow_redirects=False)

    def test_signup_yes_turns_email_reading_on(self, db):
        assert self._signup("true").status_code == 303
        db.expire_all()
        assert get_or_create_settings(db).email_sync_enabled is True

    def test_signup_not_now_leaves_it_off(self, db):
        self._signup("false")
        db.expire_all()
        assert get_or_create_settings(db).email_sync_enabled is False

    def test_a_second_run_while_one_is_running_is_skipped(self, db, imap_env):
        email_sync_service._RUN_LOCK.acquire()
        try:
            assert email_sync_service.run_sync(db, connect=lambda: FakeIMAP({}), force=True) == \
                {"skipped": "a check is already running"}
            assert email_sync_service.run_in_background() is False
        finally:
            email_sync_service._RUN_LOCK.release()

    def test_check_now_without_a_mailbox_says_so(self, db):
        from fastapi.testclient import TestClient

        from app.csrf import CSRF_COOKIE_NAME
        from app.main import app as web_app

        client = TestClient(web_app)
        client.get("/inbox")
        response = client.post("/inbox/check", data={"csrf_token": client.cookies.get(CSRF_COOKIE_NAME)},
                               follow_redirects=False)
        assert "error=" in response.headers["location"]


def test_a_waiting_rejection_resolves_once_another_settles(db):
    """IBM, 2026-11-21: two sent applications, two rejections; one names its title, the other names none. Once the
    first is applied, the second has one sent application left and applies in the same check."""
    c = make_company(db, "IBM")
    named = _app(db, c, "Data Engineer-Data Platforms-Google - 5", "Applied")
    other = _app(db, c, "Data Engineer, IBM Quantum", "Applied")
    vague = mail("talent@ibm.com", "Your IBM Application Status",
                 "Dear D, we have decided to move forward with other candidates.", name="IBM Talent Acquisition", days=-3)
    _ingest(db, vague)
    assert _row(db, vague).state == "pending"
    clear = mail("talent@ibm.com", "Your IBM Application Status",
                 "Ref: 134985 - Data Engineer-Data Platforms-Google - 5. We have decided to move forward with other "
                 "candidates.", name="IBM Talent Acquisition")
    _ingest(db, clear)
    db.refresh(named), db.refresh(other)
    assert (named.status, other.status) == ("Not Selected", "Not Selected")


def test_company_from_time_to_apply_to():
    raw = mail("no-reply@us.greenhouse-mail.io", "Thanks for your interest, D",
               "Thanks again for taking the time to apply to SeatGeek. We have decided not to move forward with your "
               "application for the Senior Machine Learning Engineer position.")
    assert email_classifier.classify(raw).company == "SeatGeek"


def test_confirmation_from_the_employers_own_address_applies(db):
    """New York Life, 2026-11-22: its own autoresponder, so medium confidence, but the company matched exactly."""
    app = _app(db, make_company(db, "Seattle Children's"), "Data Engineer", "Tailored")
    raw = mail("recruitment@jobalerts.seattlechildrens.org", "Thank you for your interest in Seattle Children's",
               "Dear D, Thank you for taking time to apply for the Data Engineer role. We're currently reviewing "
               "applications for this position.")
    _ingest(db, raw)
    db.refresh(app)
    assert _row(db, raw).confidence == "medium" and app.status == "Applied"


def test_reminder_for_a_started_application_is_not_a_confirmation():
    """Amazon "Keep track of your application", 2026-11-22: sent whether or not the application was finished."""
    raw = mail("noreply@mail.amazon.jobs", "Keep track of your application",
               "Thank you for your interest in Business Intelligence Engineer, Amazon Autos (ID: 10488670). If you have "
               "completed the application: Great! If you are still working on the application: Don't worry! You can "
               "complete it here when you're ready.")
    assert kind(raw) == "ignore"


def test_general_title_inside_a_longer_named_role_is_not_evidence(db):
    c = make_company(db, "Amazon")
    _app(db, c, "Applied Scientist", "Ingested")
    _app(db, c, "Data Engineer, Amazon Prime", "Applied")
    raw = mail("noreply@mail.amazon.jobs", "Thank you for Applying to Amazon!",
               "We've received your application for the Applied Scientist, AWS Payments and Fraud Prevention (ID: "
               "10557803) position.")
    assert _match(db, raw).state != "matched"


def test_close_company_name_with_the_exact_title_applies(db):
    """Otter.ai, 2026-11-23: talent@otter.ai names "Otter" (a close match) but also the exact role, so it applies."""
    c = make_company(db, "Otter.ai")
    target = _app(db, c, "Software Engineer, Data Infrastructure", "Tailored")
    other = _app(db, c, "Software Engineer, AI Agent & LLM", "Tailored")
    raw = mail("talent@otter.ai", "Thank you for applying to Otter.ai, DESHRAJ!",
               "Dear DESHRAJ, Thank you for taking the time to submit your application for Software Engineer, Data "
               "Infrastructure and considering Otter.ai for your next role!")
    _ingest(db, raw)
    db.refresh(target); db.refresh(other)
    assert (target.status, other.status) == ("Applied", "Tailored")


def test_confirmation_for_two_unsent_copies_goes_to_the_newest(db):
    """Pinterest, 2026-11-23: the same posting saved twice, neither sent; the confirmation marks the newer copy."""
    c = make_company(db, "Pinterest")
    old = _app(db, c, "Software Engineer II, Data Analytics & Engineering", "Tailored", job_url="https://x/old")
    new = _app(db, c, "Software Engineer II, Data Analytics & Engineering", "Tailored", job_url="https://x/new")
    _app(db, c, "Machine Learning Engineer", "Tailored")
    raw = mail("no-reply@us.greenhouse-mail.io", "Your application for Software Engineer II, Data Analytics & Engineering",
               "Hi Deshraj, Thank you for your interest in joining the Pinterest team! We appreciate the time you took to "
               "apply for a role with us.", name="Pinterest")
    _ingest(db, raw)
    db.refresh(old); db.refresh(new)
    assert (new.status, old.status) == ("Applied", "Tailored")


def test_rejection_for_two_unsent_copies_still_waits(db):
    c = make_company(db, "Pinterest")
    _app(db, c, "Data Engineer", "Tailored", job_url="https://x/a")
    _app(db, c, "Data Engineer", "Tailored", job_url="https://x/b")
    raw = mail("no-reply@us.greenhouse-mail.io", "Your application for Data Engineer",
               "Thank you for applying to Pinterest. We have decided to move forward with other candidates for the "
               "Data Engineer role.", name="Pinterest")
    assert _match(db, raw).state != "matched"
