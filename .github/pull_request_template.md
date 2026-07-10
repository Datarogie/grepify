## What

<!-- One or two sentences: what this changes and why. Reference the issue id(s), e.g. GRP-45 / T3. -->

## How to test

<!--
Write the steps the reviewer will actually run. Default to PHONE steps (open a
page, tap a thing) - most testing happens on a phone. Only require a computer
when there is genuinely no phone path, and mark those steps "(computer)". For
every step, say exactly what the reviewer should SEE (the observable result),
not just what to do.

If this change has no user-visible surface (backend / CLI / data only), say that
plainly here and point to the Automated test evidence section instead.
-->

**On phone:**
1.
   - Expected:

**On a computer (only for steps that need one):**
-
   - Expected:

## Acceptance criteria

- [ ]

## Automated test evidence

- `make check` green (ruff + `mypy --strict` + pytest, N passed).
- New/changed tests:

## Notes / decisions

<!-- Assumptions made and proceeded on, PRD-diff proposals, documented follow-ups, residual risks. -->

<!--
READY-TO-MERGE SWEEP (do before opening or updating this PR):
- No em/en dashes anywhere in the diff (U+2014 / U+2013).
- No AI-authorship attribution - in commit messages, this body, or the commit
  AUTHOR identity: no "Co-Authored-By", "Claude-Session", "Generated with/by",
  or claude.ai/code. Every message is about the change itself.
-->
