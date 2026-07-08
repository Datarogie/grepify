#!/usr/bin/env bash
# Coarse time-of-day gate for the pipeline's digest steps (GRP-06).
#
# The pipeline cron runs 3x/day; only one of those runs should attempt the
# daily digest, and only one run per week the weekly digest. This is a
# deliberately simple placeholder — GRP-45 (M4) replaces it with a tested,
# DST-aware pure function once the digest command itself is real (it is still
# a stub today). Wired here only so the workflow structure and loop-safety
# guards exist end to end now.
#
# Clock source is America/Edmonton per PRD §5. GREPIFY_FAKE_HOUR /
# GREPIFY_FAKE_WEEKDAY override the clock for tests; leave unset in CI.
#
# Output: two `key=value` lines (`daily=`, `weekly=`), valid to append
# directly to $GITHUB_OUTPUT.
set -euo pipefail

if [[ -n "${GREPIFY_FAKE_HOUR:-}" ]]; then
  hour="$GREPIFY_FAKE_HOUR"
else
  hour=$(TZ=America/Edmonton date +%-H)
fi

if [[ -n "${GREPIFY_FAKE_WEEKDAY:-}" ]]; then
  weekday="$GREPIFY_FAKE_WEEKDAY"
else
  weekday=$(TZ=America/Edmonton date +%-u) # 1=Monday .. 7=Sunday
fi

daily=false
weekly=false

# Morning slot only (one of the 3x/day cron runs lands ~05:00-08:00 local
# across both DST offsets); Monday morning doubles as the weekly slot.
if ((hour >= 5 && hour <= 8)); then
  daily=true
  if ((weekday == 1)); then
    weekly=true
  fi
fi

echo "daily=$daily"
echo "weekly=$weekly"
