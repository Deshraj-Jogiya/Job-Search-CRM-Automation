# Manual QA checklist

Covers the parts of this application that inherently need a real browser
against a real third-party site, or a real LLM call, and so aren't
covered by the automated suite in this directory. Run through this
after any change that touches the autofill modules, the confirmation
queue's auto-launch path, or the notification code.

## Before you start

- Use a disposable dummy PDF for resume/cover-letter uploads, not a real
  document, unless you specifically intend to review real content.
- If SMTP is configured with real credentials, be aware that any test
  which reaches `notification_service` will send a real email. Unset
  `SMTP_USER`/`SMTP_PASSWORD` in the shell running the test if you want
  to exercise the code path without a real send.
- Never let a real submit button get clicked during a QA pass unless
  you specifically intend to submit a real application.

## Per-ATS autofill

For each of Greenhouse, Lever, and Ashby, against a real live posting:

1. Trigger autofill (either the manual "Open Application" button on an
   Approved application, or the auto-launch path below).
2. Confirm the browser window that opens is visible, not headless.
3. Confirm contact fields (name, email, phone where applicable) are
   filled and hold their values -- re-check after the resume upload
   completes, since some fields have been found to reset asynchronously
   after an upload interaction.
4. Confirm the resume attaches successfully -- the ATS's own widget
   should show the real filename, with no JavaScript error banner on
   the page.
5. Confirm education fields (where the ATS has them) either fill
   correctly or are left visibly blank -- never a wrong value.
6. Confirm custom screening questions are answered, or left blank for
   ones the LLM couldn't honestly answer from the profile -- spot-check
   a few answers against the real profile for accuracy.
7. Confirm the on-page review banner is visible and legible.
8. Confirm nothing was submitted -- the form's submit button should
   still be sitting there, unclicked.

## Auto-launch flow (confirmation_service's clean-tailor path)

1. Set up a real application with a tailored, unflagged result against
   an autofill-supported source.
2. Trigger tailoring for real (not a seeded/synthetic status change).
3. Confirm the application's status becomes Approved without any manual
   click.
4. Confirm a real browser window opens automatically, pre-filled, per
   the per-ATS checklist above.
5. Confirm a notification email arrives (if SMTP is configured) with no
   approve/reject link -- just a pointer to go look at the open window.
6. Confirm a flagged (fabrication/scam) application does NOT take this
   path -- it should still land in Needs Review with no auto-launch.

## Submission detection (once built)

1. Complete a real application through the auto-launch flow.
2. Click the real submit button yourself.
3. Confirm the application's status transitions to Applied without a
   manual "Mark as Applied" click.
4. Confirm a false positive doesn't occur -- navigating away from an
   application page without submitting should not mark it Applied.
