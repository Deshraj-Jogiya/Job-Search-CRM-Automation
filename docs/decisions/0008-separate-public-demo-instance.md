# 0008: The public demo is a separate instance with its own data and no real keys

**Status:** Accepted

## Context
Visitors should be able to try the app without signing in, but the personal instance holds real profile data, real API keys and real
email credentials.

## Decision
The demo runs as a separate instance on the same VM: its own system user, directory, deploy key, SQLite database, secret keys and no
real API keys, in a separate working directory so `load_dotenv()` can never fall back to the personal `.env`. `APP_MODE=showcase`
refuses to start if the database URL points at Supabase, and every data source defaults to production-only unless explicitly allowed
for the demo.

## Consequences
- The demo cannot read or change personal data, spend on paid APIs or send email.
- The demo is held to the same quality bar as the personal instance; its health checks and migrations are maintained alongside it.
