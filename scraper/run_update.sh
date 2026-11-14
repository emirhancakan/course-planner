#!/usr/bin/env bash
# Refreshes the course data and commits each semester that changed.
#
# With $SEMESTER set, refreshes just that semester. Otherwise refreshes what
# `scrape_boun.py --targets` names: the registrar's newest semester and the
# newest one already published, so the current term keeps updating while the
# next one is listed without a timetable.
#
# A semester with no timetable yet is skipped. A failed scrape or integrity
# check publishes nothing for that semester, but the others still go out; the
# script then exits non-zero so the run shows as failed.
#
# Outputs (when run in GitHub Actions): committed=<space-separated semesters>.

set -uo pipefail
cd "$(dirname "$0")/.."

summary="${GITHUB_STEP_SUMMARY:-/dev/null}"
msg="$(mktemp)"
committed=""
failed=""

if [ -n "${SEMESTER:-}" ]; then
  targets="$SEMESTER"
else
  targets="$(python scraper/scrape_boun.py --targets)" || exit 1
fi

{ echo "### Course data update"; echo; } >> "$summary"

for sem in $targets; do
  echo "::group::$sem"
  python scraper/scrape_boun.py "$sem"
  rc=$?
  if [ "$rc" -eq 3 ]; then
    echo "::notice::$sem is listed without a timetable yet - skipped."
    echo "- \`$sem\`: listed without a timetable yet - skipped." >> "$summary"
  elif [ "$rc" -ne 0 ]; then
    failed="$failed $sem"
    echo "- \`$sem\`: scrape failed - see the log." >> "$summary"
  else
    rm -f "$msg"
    # GITHUB_OUTPUT is blanked so detect_changes doesn't write per-semester
    # outputs; the commit message file is the signal that something changed.
    if GITHUB_OUTPUT="" COMMIT_MESSAGE_FILE="$msg" python scraper/detect_changes.py "$sem"; then
      if [ -s "$msg" ]; then
        git add data/ && git commit -q -F "$msg" && committed="$committed $sem"
        { echo "- \`$sem\`: committed."; echo; echo '```'; cat "$msg"; echo '```'; } >> "$summary"
      else
        echo "- \`$sem\`: no course changes." >> "$summary"
      fi
    else
      failed="$failed $sem"
      echo "- \`$sem\`: integrity check failed - not published." >> "$summary"
    fi
  fi
  # Anything this semester left uncommitted must not ride along in the next
  # semester's commit.
  git checkout -q -- data/
  git clean -fdq data/
  echo "::endgroup::"
done

if [ -n "$committed" ]; then
  git push || exit 1
fi
echo "committed=${committed# }" >> "${GITHUB_OUTPUT:-/dev/null}"

if [ -n "$failed" ]; then
  echo "::error::Not updated:$failed"
  exit 1
fi
