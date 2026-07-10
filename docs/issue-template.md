# Issue template - GRP-xx: <title>

Copy this for every issue. An agent must be able to execute from the epic brief
(`docs/epics/E<n>.md`) + this issue alone - no cross-epic context (PRD §12).

**Model:** Opus | Sonnet
**Epic:** E<n>
**Depends on:** GRP-xx (interfaces consumed)

## Scope

What this issue delivers. One coherent unit of work.

## Non-scope

What this issue explicitly does NOT touch (deferred to which issue). Guards
against scope creep - the Non-Goals (PRD §2) still apply.

## Files touched

- `path/to/file.py` - what changes

## Acceptance criteria (AC)

- [ ] Observable, checkable statements (e.g. "double-run adds zero new rows").
- [ ] Interfaces expose no backend-specific types (Postgres-swappable).
- [ ] Every new module documents its failure modes in its docstring.

## Test list

- Unit: …
- Fixture/contract: …
- Integration/snapshot: …

## Definition of done

Code + tests + fixtures + docstring failure modes + `make check` green. MR title
carries the issue ID; snapshot/eval deltas stated in the MR description.
