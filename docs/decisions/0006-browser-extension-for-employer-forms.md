# 0006: A browser extension fills forms in the user's own browser

**Status:** Accepted

## Context
Server-side form filling with Playwright worked on direct applicant-tracking pages but failed on employer-wrapped forms: the real form
sits in a cross-origin iframe, and requests from a datacenter IP were blocked by bot protection (5 of 5 attempts on one employer filled
0 fields).

## Options
1. Keep improving the server-side browser automation.
2. A Chrome extension that runs in the user's own browser and residential connection.

## Decision
Option 2. The content script does only generic field detection (labels, input types, radio and checkbox groups, file inputs,
react-select comboboxes, shadow DOM) and asks the app's API for answers, so all matching logic stays in one Python implementation.
Authentication reuses the existing session cookie, passed as an explicit header; the extension API is exempt from the CSRF token
because a third-party page cannot read that cookie or the response. Cross-origin iframes require broad host permissions, which the
extension README states plainly.

## Consequences
- Forms on 7 applicant-tracking systems were tested live (Greenhouse, Lever, Ashby, Recruitee, Personio, Workable, SmartRecruiters).
- Honeypot fields are excluded; consent questions are never answered automatically; the final submit stays with the user (0001).
