# Decision records

Short records of the design decisions that shape Career Pilot: the problem, the options that were weighed, the choice and its
consequences. Newest decisions are added at the end; a decision that is replaced is marked as superseded rather than deleted.

| # | Decision | Status |
|---|---|---|
| [0001](0001-human-confirmation-gate.md) | Nothing is submitted or sent without a human click | Accepted |
| [0002](0002-non-llm-fabrication-guardrail.md) | A non-LLM check against the master profile gates every tailored document | Accepted |
| [0003](0003-mechanical-scoring-before-paid-llm.md) | Free mechanical scoring runs before any paid LLM call, under a daily spend cap | Accepted |
| [0004](0004-provider-agnostic-llm-layer.md) | One LLM interface with interchangeable providers behind it | Accepted |
| [0005](0005-postgres-with-row-level-security.md) | Supabase Postgres in production with row-level security on every table | Accepted |
| [0006](0006-browser-extension-for-employer-forms.md) | A browser extension fills forms in the user's own browser | Accepted |
| [0007](0007-kanban-moves-use-guarded-transitions.md) | Board drags go through the same guarded status transitions as buttons | Accepted |
| [0008](0008-separate-public-demo-instance.md) | The public demo is a separate instance with its own data and no real keys | Accepted |
