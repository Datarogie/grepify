# Session protocol

Standing protocol for **every** agent session. Companion to `docs/playbook.md`
(session order + model routing) and `CLAUDE.md` (binding rules). The playbook
says *what* each session builds; this says *how* every session runs, gates, and
hands off.

## SESSION PROTOCOL (all sessions)

### a. Work the scope

Work through the session's scope end to end. Do not stop between issues — keep
going while context permits. If context usage approaches **~40%**, stop at a
clean boundary, commit, and output a **CONTINUATION PROMPT**: the exact text to
paste into a fresh session, stating what is done, what remains, and which files
matter.

### b. Completion gate

When scope is complete, run `make check` **and** the full test suite. Fix every
failure before proceeding — no red gate advances.

### c. Self-review gate (fresh-context subagent)

Spawn a subagent with **fresh context**. Give it ONLY:

1. the list of issue IDs in scope and their acceptance criteria **copied from
   `docs/prd.md`**, and
2. the diff.

It returns a review: AC met/unmet per issue, bugs, style violations
(`CLAUDE.md`), and missing tests. Address **every** finding or justify the
dismissal in writing. Repeat until the subagent passes the session.

### d. Open a PR

- **Title** = the issue IDs in scope.
- **Body** =
  - per-issue AC checklist (checked),
  - test evidence,
  - subagent review summary,
  - a **Phone-testable** section (see below).

#### Phone-testable (MANDATORY from S2 onward)

From S2 on, every PR body carries this section, in exactly this shape:

- **WHAT CHANGED** — 2–3 bullets, plain language (no jargon).
- **HOW TO TEST FROM PHONE** — numbered steps, each with a **direct tappable
  link** (the Actions run URL, the Pages URL, a specific page/anchor). **No step
  may require a terminal.**

If nothing is phone-testable this session, **say so explicitly** and state what
the self-review subagent verified instead (step c). S1 is the only session
exempt from producing tappable links (no CI/site exists yet).

### e. NEXT SESSION block

End the final message with a **NEXT SESSION** block:

- session number,
- model (per `docs/playbook.md` routing),
- the complete kickoff prompt, ready to paste, including any session-specific
  injections from the playbook appendix.

The kickoff prompt must **always end with** this block:

```
HUMAN FEEDBACK (optional - replace or delete this block):
- Phone test results: <pass / what looked wrong>
- Changes or requests to fold into this session: <...>
- Anything to add to CLAUDE.md or docs/process.md: <...>
```

A filled-in HUMAN FEEDBACK block is **scope-adjusting input**. The receiving
session must:

- Address feedback **FIRST** — fixes from the last session's phone test are
  **priority 0**, ahead of new scope.
- Propose PRD / process-doc diffs where feedback implies a **standing** change
  (never edit `docs/prd.md` silently — propose the diff in the PR, per
  `CLAUDE.md`).

### f. Stacking on an unmerged previous PR

If the previous session's PR is **unmerged** when a new session starts, the new
session begins by checking whether its scope **depends on that branch**:

- **If yes** — branch from the previous session's branch (not `main`) and note
  the stacking in the PR body (which branch it stacks on, and that it should
  merge after).
- **If no** — branch from `main` as usual.
