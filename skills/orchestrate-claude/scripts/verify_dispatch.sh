#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 SESSION DISPATCH_ID PRE_CAPTURE [POST_CAPTURE]" >&2
  exit 2
}

(($# == 3 || $# == 4)) || usage
session=$1
dispatch_id=$2
pre_capture=$3
post_capture=${4:-}
start_marker="[dispatch:$dispatch_id]"
end_marker="[dispatch-end:$dispatch_id]"
accepted_marker="[dispatch-accepted:$dispatch_id]"
activity_regex=${ORCHESTRATION_ACTIVITY_REGEX:-'^[[:space:]]*([*]|•|●|⏺|◉)[[:space:]]+(Working|Thinking|Running|Tool|Bash|Read|Edit|Write|Search|Fetch|Exec)|^[[:space:]]*(Tool|Bash|Read|Edit|Write|Search|Fetch|Exec)[(:[:space:]]'}
composer_regex=${ORCHESTRATION_COMPOSER_REGEX:-'^[[:space:]]*(›|❯|>)[[:space:]]*$'}

[[ -r $pre_capture ]] || { echo "pre-capture is not readable: $pre_capture" >&2; exit 2; }
for marker in "$start_marker" "$end_marker" "$accepted_marker"; do
  grep -Fq "$marker" "$pre_capture" && { echo "dispatch marker already existed before submission" >&2; exit 1; }
done

last_marker_line() {
  local marker=$1 capture=$2
  awk -v marker="$marker" 'index($0, marker) { line = NR } END { if (line) print line }' "$capture"
}

last_marker_position() {
  local marker=$1 capture=$2
  awk -v marker="$marker" '
    {
      position = index($0, marker)
      if (position) last = offset + position
      offset += length($0) + 1
    }
    END { if (last) print last }
  ' "$capture"
}

verify_capture() {
  local capture=$1 start_position end_position accepted_position accepted_line activity_line composer_line last_line
  start_position=$(last_marker_position "$start_marker" "$capture")
  end_position=$(last_marker_position "$end_marker" "$capture")
  accepted_position=$(last_marker_position "$accepted_marker" "$capture")
  accepted_line=$(last_marker_line "$accepted_marker" "$capture")
  [[ -n $start_position && -n $end_position && -n $accepted_position && -n $accepted_line ]] || return 1
  ((start_position < end_position && end_position < accepted_position)) || return 1

  activity_line=$(awk -v line="$accepted_line" -v pattern="$activity_regex"     'NR > line && $0 ~ pattern { print NR; exit }' "$capture")
  [[ -n $activity_line ]] || return 1

  composer_line=$(awk -v line="$activity_line" -v pattern="$composer_regex"     'NR > line && $0 ~ pattern { found = NR } END { if (found) print found }' "$capture")
  [[ -n $composer_line ]] || return 1
  last_line=$(awk 'NF { line = NR } END { if (line) print line }' "$capture")
  [[ $composer_line == "$last_line" ]] || return 1
  return 0
}

if [[ -n $post_capture ]]; then
  [[ -r $post_capture ]] || { echo "post-capture is not readable: $post_capture" >&2; exit 2; }
  verify_capture "$post_capture" || {
    echo "dispatch lacks ordered start/end/acceptance/activity/clean-composer evidence" >&2
    exit 1
  }
  printf 'verified_dispatch=%s\npost_capture=%s\n' "$dispatch_id" "$post_capture"
  exit 0
fi

attempts=${ORCHESTRATION_VERIFY_ATTEMPTS:-5}
delay=${ORCHESTRATION_VERIFY_DELAY_SECONDS:-1}
evidence_dir="${TMPDIR:-/tmp}/orchestrate-verify-$dispatch_id"
mkdir -p "$evidence_dir"
pane_id=$(tmux list-panes -t "=$session" -F '#{pane_id}' | head -n 1)
[[ -n $pane_id ]] || { echo "tmux session has no pane: $session" >&2; exit 2; }
for ((attempt = 1; attempt <= attempts; attempt++)); do
  post_capture="$evidence_dir/post-$attempt.txt"
  tmux capture-pane -p -J -t "$pane_id" -S - >"$post_capture"
  if verify_capture "$post_capture"; then
    printf 'verified_dispatch=%s\npost_capture=%s\n' "$dispatch_id" "$post_capture"
    exit 0
  fi
  sleep "$delay"
done
echo "dispatch lacks ordered start/end/acceptance/activity/clean-composer evidence; inspect $post_capture" >&2
exit 1
