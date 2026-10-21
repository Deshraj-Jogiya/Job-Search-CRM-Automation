# 0005: Supabase Postgres in production with row-level security on every table

**Status:** Accepted

## Context
A fresh install should work with no setup, but the production instance needs a durable managed database. Supabase exposes an
automatic public REST API for every table, which this app never uses.

## Decision
SQLite for a fresh local install; Postgres on Supabase in production through `DATABASE_URL` (pooler hostname, because the direct
hostname is IPv6-only and the VM has no IPv6 route). Alembic manages schema changes, and each migration is checked on both SQLite and
Postgres. Row-level security is enabled on every table, and a regression test fails if a new table is added without it.

## Consequences
- The public REST API path is closed even though the app talks to the database directly.
- SQLite-incompatible migration steps (adding a CHECK constraint, dropping a column on older SQLite) use batch mode.
