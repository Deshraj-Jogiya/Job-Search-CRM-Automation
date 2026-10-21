# 0001: Nothing is submitted or sent without a human click

**Status:** Accepted

## Context
The pipeline finds postings, tailors documents and pre-fills application forms. Each of those steps can be automated end to end,
and many job-search tools do submit applications automatically. A wrong submission (wrong role, a document with an error, a duplicate
application) cannot be taken back, and an outreach email sent to the wrong person is public.

## Options
1. Fully automatic submission and sending.
2. Automatic with an undo window.
3. Automation prepares everything; a person reviews and clicks the final submit or send.

## Decision
Option 3. Every irreversible step (submitting an application, sending an email or message) requires an explicit human action.
Automation stops at "ready to submit".

## Consequences
- Throughput is limited by review time, which is accepted: the goal is fewer, correct applications.
- Status changes that look automatic (for example a clean tailoring run moving to Approved) never submit anything.
- Tests and manual QA check that no code path can submit or send on its own.
