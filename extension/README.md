# Career Pilot Autofill (companion browser extension)

Fills real job application forms using your own Career Pilot profile,
running in your own real browser instead of the server's. This is what
actually fixes the employer-wrapped/bot-blocked case (confirmed live on
Samsara: 5/5 real server-side attempts, 0 fields filled) -- the block was
tied to the automation VM's datacenter IP, not anything about the form
itself, and your own browser doesn't have that problem.

This is a personal tool for your own Career Pilot instance. It is not
published anywhere and isn't meant to be -- you load it as an unpacked
extension in developer mode, the same way you'd run any local dev tool.

## Install (one-time)

1. Open `chrome://extensions` in Chrome (or `edge://extensions` in Edge).
2. Turn on **Developer mode** (top-right toggle).
3. Click **Load unpacked** and select this `extension/` folder.
4. Click the new Career Pilot Autofill icon in your toolbar, enter your
   Career Pilot URL (e.g. `https://129-146-36-193.sslip.io`), and click
   **Save**.
5. Make sure you're logged into Career Pilot itself in this same browser
   -- the extension reads that existing login, it doesn't ask you to log
   in separately.

## Use

1. Approve an application in Career Pilot as usual.
2. Open the real application page for that job (the actual employer
   site, or the ATS page it links to).
3. Click the extension icon. If it recognizes the page as a real,
   Approved application, it'll show the job title and company and a
   **Fill This Page** button.
4. Click it. Fields it could answer confidently get filled and outlined
   in green; anything it left blank is outlined in orange dashes --
   those are yours to fill in by hand, on purpose (it never guesses).
5. Review everything yourself before submitting, exactly like every
   other part of this app -- nothing here ever clicks submit for you.

## What it can and can't answer

Reuses the exact same fixed-fact logic the server-side automation
already has (see `app/services/extension_service.py` and
`app/services/autofill/common_answers.py`): name, email, phone, EEO
questions, visa/work-authorization/relocation/salary preferences, "how
did you hear about this role," and your real tailored cover letter text
for a matching field. It does **not** attempt open-ended essay questions
("why do you want to work here?") -- those still need you.

## Why the permissions are broad

This extension asks for access to every site (`host_permissions:
["<all_urls>"]`) and to read cookies for your Career Pilot domain. Both
are real, deliberate requirements, not an oversight:

- It has to be able to reach into a form embedded on **any** employer's
  site, including cross-origin iframes -- there's no way to list every
  ATS/employer domain in advance, and Chrome's narrower `activeTab`
  permission does not extend into a cross-origin iframe at all (verified
  against Chrome's own documented limitation before building this), so a
  scoped permission model literally can't cover the Samsara-style case
  this exists for.
- It reads your Career Pilot login cookie (not any other site's) so you
  never have to generate or paste a separate token -- see the commit
  history / CLAUDE.md for the full reasoning on why that's a reasonable
  trade for a single-user, self-authored, never-published extension like
  this one specifically.

If you fork Career Pilot and reuse this extension against your own
self-hosted instance, the same reasoning holds for you too: each copy
only ever reads its own single instance's own cookie -- there's no
shared backend or multi-tenant risk here to worry about.

## Nothing here auto-submits

Matches the rest of this app's hard rule: this extension fills fields,
it never clicks a submit/apply button. You always review and submit
yourself.
