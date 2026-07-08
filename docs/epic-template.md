# Epic brief template — E<n>: <epic name> (M<milestone>)

~1 page, written once per epic. Its job: let an agent implement any issue in the
epic without reading other epics' code — **restate the interfaces this epic
consumes** (PRD §12). See `docs/epics/E0.md` for a worked example.

## Goal

One paragraph: what this epic delivers and why it lands here in the milestone
order.

## Interfaces consumed (restated)

Copy the exact signatures of any `Repository` / `ConfigProvider` / fetcher /
LLM-provider methods this epic's issues call. The agent should not need to open
the producing epic's source.

## Interfaces produced

New contracts this epic defines that later epics depend on. Give signatures.

## Contracts & rules

- Data shapes, invariants, determinism requirements specific to this epic.
- Failure-mode expectations (what degrades vs what fails the run).

## Issues in this epic

| ID | Title | Model | Parallel-safe? |
|---|---|---|---|

## Definition of done

`make check` green; tests + fixtures for everything testable; MR per issue (or
grouped per session) with IDs in the title.
