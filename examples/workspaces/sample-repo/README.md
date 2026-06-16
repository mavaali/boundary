# Sample service

This tiny workspace exists so Boundary examples have a safe repository-like
target. It is intentionally small but includes enough surface area for research,
docs review, and risk-review examples.

## Behavior

- `src/access.py` grants read access to active users.
- Admin users can read every project.
- Non-admin users can read only projects listed in their `project_ids`.

## Known follow-up

The project notes mention a planned audit log, but the implementation does not
write audit records yet.

