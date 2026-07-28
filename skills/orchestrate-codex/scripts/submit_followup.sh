#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 SESSION FOLLOWUP_FILE" >&2
  exit 2
}

(($# == 2)) || usage
session=$1
followup_file=$2
[[ $session =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "invalid tmux session name" >&2; exit 2; }
[[ $followup_file == /* ]] || { echo "follow-up path must be absolute: $followup_file" >&2; exit 2; }
[[ -r $followup_file ]] || { echo "follow-up is not readable: $followup_file" >&2; exit 2; }
tmux has-session -t "=$session" 2>/dev/null || { echo "tmux session does not exist: $session" >&2; exit 2; }
pane_id=$(tmux list-panes -t "=$session" -F '#{pane_id}' | head -n 1)
[[ -n $pane_id ]] || { echo "tmux session has no pane: $session" >&2; exit 2; }

tool_name=codex
dispatch_id="${ORCHESTRATION_DISPATCH_ID:-$tool_name-$(date -u +%Y%m%dT%H%M%SZ)-$$-$RANDOM}"
start_marker="[dispatch:$dispatch_id]"
accepted_marker="[dispatch-accepted:$dispatch_id]"
end_marker="[dispatch-end:$dispatch_id]"
buffer="orchestrate-$dispatch_id"
evidence_dir="${TMPDIR:-/tmp}/orchestrate-$session-$dispatch_id"
mkdir -p "$evidence_dir"
pre_capture="$evidence_dir/pre.txt"
tmux capture-pane -p -J -t "$pane_id" -S - >"$pre_capture"
grep -Fq "$end_marker" "$pre_capture" && { echo "dispatch marker collision" >&2; exit 1; }

tmux send-keys -t "$pane_id" -l "$start_marker "
followup=$(<"$followup_file")
followup="Acknowledge this exact turn first by emitting $accepted_marker. $followup"
tmux set-buffer -b "$buffer" -- "$followup"
tmux paste-buffer -b "$buffer" -t "$pane_id" -d
tmux send-keys -t "$pane_id" -l " $end_marker"
tmux send-keys -t "$pane_id" Enter

script_dir=$(cd -- "$(dirname -- "$0")" && pwd -P)
verify="$script_dir/verify_dispatch.sh"
if "$verify" "$session" "$dispatch_id" "$pre_capture" >"$evidence_dir/verify-first.out" 2>"$evidence_dir/verify-first.err"; then
  printf 'dispatch_id=%s\nenter_presses=1\nevidence_dir=%s\n' "$dispatch_id" "$evidence_dir"
  exit 0
fi

after_first="$evidence_dir/after-first-enter.txt"
tmux capture-pane -p -J -t "$pane_id" -S - >"$after_first"
tail_nonblank=$(awk 'NF { lines[++count] = $0 } END { start = count > 8 ? count - 7 : 1; for (i = start; i <= count; i++) print lines[i] }' "$after_first")
if ! grep -Fq '[Pasted Content' <<<"$tail_nonblank" && ! grep -Fq "$end_marker" <<<"$tail_nonblank"; then
  echo "first Enter changed the pane but fresh post-marker activity is not yet visible; inspect $evidence_dir" >&2
  exit 1
fi

tmux send-keys -t "$pane_id" Enter
if "$verify" "$session" "$dispatch_id" "$pre_capture" >"$evidence_dir/verify-second.out" 2>"$evidence_dir/verify-second.err"; then
  printf 'dispatch_id=%s\nenter_presses=2\nevidence_dir=%s\n' "$dispatch_id" "$evidence_dir"
  exit 0
fi

echo "directive is not verified after two Enter presses; inspect $evidence_dir" >&2
exit 1
