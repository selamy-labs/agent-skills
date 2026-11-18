#!/usr/bin/env bash
set -euo pipefail
umask 077

usage() {
  echo "usage: $0 SESSION DISPATCH_ID PRE_CAPTURE [POST_CAPTURE]" >&2
  exit 2
}

(($# == 3 || $# == 4)) || usage
session=$1
dispatch_id=$2
pre_capture=$3
post_capture=${4:-}

if ! [[ ($session == "-" && -n $post_capture) || ($session =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ && $session != *..*) ]]; then
  echo "invalid tmux session name: $session" >&2
  exit 2
fi
if ! [[ $dispatch_id =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ && $dispatch_id != *..* ]]; then
  echo "invalid dispatch id: $dispatch_id" >&2
  exit 2
fi
[[ -r $pre_capture ]] || { echo "pre-capture is not readable: $pre_capture" >&2; exit 2; }

script_path=$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$0")
script_dir=$(cd -- "$(dirname -- "$script_path")" && pwd -P)
repo_root=$(cd -- "$script_dir/../../.." && pwd -P)
helper="$repo_root/tools/orchestrate_dispatch.py"
[[ -f $helper ]] || helper="$script_dir/orchestrate_dispatch.py"
[[ -f $helper ]] || { echo "missing dispatch helper: $helper" >&2; exit 2; }

classify_capture() {
  local capture_file=$1
  python3 -B "$helper" classify --dispatch-id="$dispatch_id" --pre="$pre_capture" --capture="$capture_file"
}

kv_get() {
  local key=$1 payload=$2
  awk -F= -v k="$key" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' <<<"$payload"
}

if [[ -n $post_capture ]]; then
  [[ -r $post_capture ]] || { echo "post-capture is not readable: $post_capture" >&2; exit 2; }
  verdict=$(classify_capture "$post_capture")
  state=$(kv_get delivery_state "$verdict")
  reason=$(kv_get reason "$verdict")
  if [[ $state == "submitted" || ($state == "queued" && ${ORCHESTRATION_ALLOW_QUEUED:-0} == "1") ]]; then
    printf 'verified_dispatch=%s\ndelivery_state=%s\npost_capture=%s\n' "$dispatch_id" "$state" "$post_capture"
    exit 0
  fi
  printf '%s (delivery_state=%s)\n' "$reason" "$state" >&2
  exit 1
fi

pre_verdict=$(classify_capture "$pre_capture")
pre_reason=$(kv_get reason "$pre_verdict")
if [[ $pre_reason == "dispatch marker already existed before submission" ]]; then
  echo "$pre_reason" >&2
  exit 1
fi

attempts=${ORCHESTRATION_VERIFY_ATTEMPTS:-5}
delay=${ORCHESTRATION_VERIFY_DELAY_SECONDS:-1}
tmux has-session -t "=$session" 2>/dev/null || { echo "tmux session not found: $session" >&2; exit 2; }
pane_id=$(tmux list-panes -t "=$session" -F '#{pane_id}' 2>/dev/null | head -n 1 || true)
[[ -n $pane_id ]] || { echo "tmux session has no pane: $session" >&2; exit 2; }
evidence_dir="${ORCHESTRATION_VERIFY_DIR:-${TMPDIR:-/tmp}/orchestrate-verify-$session-$dispatch_id}"
mkdir -p -- "$evidence_dir"
chmod 700 -- "$evidence_dir"

last_state="ambiguous"
last_reason="dispatch lacks ordered start/end/acceptance/activity/clean-composer evidence"
post_capture=""
for ((attempt = 1; attempt <= attempts; attempt++)); do
  post_capture="$evidence_dir/post-$attempt.txt"
  tmux capture-pane -p -J -t "$pane_id" -S - >"$post_capture"
  verdict=$(classify_capture "$post_capture")
  last_state=$(kv_get delivery_state "$verdict")
  last_reason=$(kv_get reason "$verdict")
  if [[ $last_state == "submitted" || ($last_state == "queued" && ${ORCHESTRATION_ALLOW_QUEUED:-0} == "1") ]]; then
    printf 'verified_dispatch=%s\ndelivery_state=%s\npost_capture=%s\n' "$dispatch_id" "$last_state" "$post_capture"
    exit 0
  fi
  if [[ $last_state == "partial_paste" || $last_state == "blocked_modal" ]]; then
    break
  fi
  if ((attempt < attempts)); then
    sleep "$delay"
  fi
done

printf '%s (delivery_state=%s); inspect %s\n' "$last_reason" "$last_state" "$post_capture" >&2
exit 1
