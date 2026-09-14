# Career Pilot — Job Search CRM & Automation Command Center

A personal job-search command center: it pulls in postings from
multiple sources, scores and tailors your resume/cover letter against
each one with an AI pass that's mechanically checked for fabrication,
and runs a human-confirmation-gated auto-apply and outreach pipeline —
built to run at $0.

Every side effect that matters — submitting an application, sending an
email — stays behind an explicit human decision. Automation handles
the repetitive part (finding postings, drafting tailored materials,
pre-filling forms); a person still reviews and clicks submit.

**Full details:** see [`ARCHITECTURE.md`](./ARCHITECTURE.md) for how
the system is built, and [`CONTRIBUTING.md`](./CONTRIBUTING.md) for
the design principles worth knowing before extending it.

## Stack

- FastAPI + SQLAlchemy + SQLite (Postgres-compatible via `DATABASE_URL`)
- Provider-agnostic LLM layer (`app/services/llm/`) — Claude by default,
  swappable to OpenAI, Gemini's free tier, or a fully local Ollama model
  via one `.env` value, no code changes required
- CSRF protection, fail-closed admin auth, and encrypted credential
  storage are baseline, not bolted on

## Running cost

Every external service this project touches has a free tier that's
genuinely enough for one person's own job search — nothing here
requires paying for anything to be fully functional. The one real
tradeoff is the LLM provider, where "free" trades money for either
your own hardware or noticeably weaker tailoring quality. Everything
below is the real, current free-tier limit of each service, not a
guess.

| Component | Free option | What free gets you | Paid option | What paid adds |
|---|---|---|---|---|
| **LLM (scoring, tailoring, cover letters, interview prep)** | Google Gemini's free tier, or a fully local Ollama model (Llama 3.1 etc.) | Gemini's free tier: full functionality, rate-limited rather than capped — fine for one person's daily posting volume. Ollama: zero cost forever, but tailoring quality depends entirely on the local model you run and your own CPU/GPU. | Anthropic Claude or OpenAI, pay-per-token | Meaningfully better tailoring/fabrication-check judgment quality (this is a real, noticeable difference on the fabrication-safeguard checks specifically, which lean on the model actually understanding nuance) — real cost is small for one person's daily volume (a handful of postings/day), typically well under $5/month, but it is a real recurring cost, not a rounding error to ignore |
| **Job board search** (Adzuna) | Free tier: ~1,000 calls/month | Fully sufficient — a daily personal job search uses a small fraction of this | Paid tier exists | Not needed for personal use; relevant only at much higher call volume than one person's search generates |
| **Job aggregation** (JobsPipe) | Free tier: 1,000 jobs/month (billed per job returned, not per call) | Fully sufficient for personal use | Paid tier exists | Same as above — not needed at personal scale |
| **Contact/company research** (Tavily) | Free tier: 1,000 searches/month | Covers real outreach-research volume for one person's search comfortably | Paid tier exists | Only relevant if you're doing outreach at a volume well beyond a single job search |
| **Email finding** (Hunter.io) | Free tier: 25 searches/month | This is the tightest free-tier budget in the project — the app tracks and caps usage against it (`hunter_monthly_call_budget`) so you never silently overrun it | Paid tier exists | More outreach contacts discovered per month, if 25/month becomes a real bottleneck |
| **Database** | SQLite (default, zero setup) or Supabase's free tier (500MB Postgres) | Both are genuinely sufficient for one person's job-search data — this isn't a high-volume system | Supabase paid tiers | Only relevant if you outgrow 500MB, which a personal job tracker realistically never will |
| **Hosting** | Oracle Cloud's "Always Free" tier VM (`VM.Standard.A1.Flex`, 24GB RAM available) | This is what the real personal + demo instances both run on — genuinely free forever, not a trial | Any paid VM/cloud host | Only relevant if you want more compute than the free ARM VM offers, which this app doesn't need |
| **Email sending/reading** (SMTP/IMAP) | A free Gmail account + app password | Fully functional, no limits this app would ever hit | — | N/A — there's no paid tier to speak of here for this use case |

**Bottom line:** you can run this entire system, fully functional, for
$0/month using Ollama or Gemini's free tier plus the free tiers above.
The only place spending real money buys something real is LLM quality
— if you want the tailoring and fabrication checks to reason as well
as they possibly can, that's the one line item worth paying for, and
even then it's a small, predictable per-month cost for one person's
own search volume, not a meaningful line item for most budgets.

## Quick start

```bash
cp .env.example .env
# fill in SECRET_KEY, CREDENTIAL_ENCRYPTION_KEY, and your chosen LLM_PROVIDER's keys
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Visit `http://localhost:8000/` — you'll see the dashboard with a
global automation switch and every tunable setting (poll intervals,
confirmation windows, retention days, outreach caps) live-editable, no
code changes needed.

## What's here

Living profile management, multi-source job search (LinkedIn, Adzuna,
and direct Greenhouse/Lever/Ashby board polling), AI matching and
tailoring with a fabrication safeguard, a confirmation-gated auto-apply
queue, real application-form autofill, outreach automation with
contact discovery, interview prep generation, outcome analytics,
encrypted backup/restore, and a deployment setup for running this on a
free-tier cloud VM. See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for how
each piece works.

## Public Showcase Mode

**Live demo:** [demo.129-146-36-193.sslip.io](https://demo.129-146-36-193.sslip.io)
-- open to browse, no login. Seeded with a fictional profile, automation
off, and no LLM/job-board/outreach API keys configured on that instance
at all, so nothing there can spend real money, scrape on your behalf,
or touch your real data.

This is primarily a real, personal-use tool, but the same codebase can
run as a public demo (`APP_MODE=showcase` in `.env`) instead of the
default personal mode. Showcase mode:

- Auto-seeds a fictional demo profile ("Jordan Ellis," not a real
  person) on first startup, so there's something to explore
  immediately instead of an empty shell.
- Defaults the automation switch OFF for a brand-new deployment (still
  toggleable from the dashboard — a safe default, not a lock). This
  never affects a deployment that already has settings saved.

**Before turning automation on in a showcase deployment:**

- This is a demonstration of the architecture, not a scraping or spam
  service. Respect the terms of service of every source it touches
  (LinkedIn's guest search endpoint in particular is undocumented and
  sensitive — see [`CONTRIBUTING.md`](./CONTRIBUTING.md)'s Origin note
  on why this project treats it as one source among several, not a
  sole strategy).
- Don't point it at real job sites while impersonating someone else, or
  use the outreach feature to email real people on a fictional
  candidate's behalf — outreach still requires an explicit human click
  per message (no auto-send, by design), but the responsibility for
  what gets sent is yours once you enable it.
- LLM calls (scoring, tailoring, interview prep) use your own API key
  and have a real, small per-call cost — this isn't free to run, even
  though hosting is.
- If you're evaluating the project rather than actually job-searching
  with it, leave automation off and use the manual "Search Now" /
  score / tailor buttons to see the flow without anything running
  unattended.

## License

MIT — see [`LICENSE`](./LICENSE).
