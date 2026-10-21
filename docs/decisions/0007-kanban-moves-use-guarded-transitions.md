# 0007: Board drags go through the same guarded status transitions as buttons

**Status:** Accepted

## Context
Status changes have side effects beyond the status value: applied, interviewing, offer and not-selected timestamps, the company's
ghosted count, and autofill launch on approval. Analytics and the dashboard depend on those side effects.

## Decision
The Kanban board's single write endpoint (`POST /jobs/{id}/kanban-move`) dispatches through the existing guarded functions in
`confirmation_service` (`KANBAN_TRANSITIONS`) and never writes the status directly. Columns with no valid manual transition (stages
the pipeline sets itself) are not drop targets.

## Consequences
- A drag and a button press produce identical records.
- Invalid moves are blocked in the browser and again on the server.
