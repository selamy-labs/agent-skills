#!/usr/bin/env bash
set -euo pipefail
umask 077

usage() {
  echo "usage: $0 SESSION FOLLOWUP_FILE" >&2
  exit 2
}

(($# == 2)) || usage
session=$1
followup_file=$2
if ! [[ $session =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ && $session != *..* ]]; then
  echo "invalid tmux session name" >&2
  exit 2
fi
[[ $followup_file == /* ]] || { echo "follow-up path must be absolute: $followup_file" >&2; exit 2; }
[[ -r $followup_file ]] || { echo "follow-up is not readable: $followup_file" >&2; exit 2; }
tmux has-session -t "=$session" 2>/dev/null || { echo "tmux session does not exist: $session" >&2; exit 2; }
pane_id=$(tmux list-panes -t "=$session" -F '#{pane_id}' 2>/dev/null | head -n 1 || true)
[[ -n $pane_id ]] || { echo "tmux session has no pane: $session" >&2; exit 2; }

script_path=$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$0")
script_dir=$(cd -- "$(dirname -- "$script_path")" && pwd -P)
repo_root=$(cd -- "$script_dir/../../.." && pwd -P)
helper="$repo_root/tools/orchestrate_dispatch.py"
[[ -f $helper ]] || helper="$script_dir/orchestrate_dispatch.py"
verify="$script_dir/verify_dispatch.sh"
[[ -f $helper ]] || { echo "missing dispatch helper: $helper" >&2; exit 2; }

kv_get() {
  local key=$1 payload=$2
  awk -F= -v k="$key" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' <<<"$payload"
}

tool_name=codex
dispatch_id="${ORCHESTRATION_DISPATCH_ID:-$tool_name-$(date -u +%Y%m%dT%H%M%SZ)-$$-$RANDOM}"
if ! [[ $dispatch_id =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ && $dispatch_id != *..* ]]; then
  echo "invalid dispatch id: $dispatch_id" >&2
  exit 2
fi
accepted_marker="[dispatch-accepted:$dispatch_id]"
start_marker="[dispatch:$dispatch_id]"
end_marker="[dispatch-end:$dispatch_id]"
buffer="orchestrate-$dispatch_id"
directive=""
buffer_loaded=0

cleanup() {
  if [[ -n $directive ]]; then
    rm -f -- "$directive"
    directive=""
  fi
  if ((buffer_loaded)); then
    tmux delete-buffer -b "$buffer" 2>/dev/null || true
    buffer_loaded=0
  fi
}

on_signal() {
  local code=$1
  trap - EXIT HUP INT TERM
  cleanup
  exit "$code"
}
trap cleanup EXIT
trap 'on_signal 129' HUP
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

evidence_dir="${TMPDIR:-/tmp}/orchestrate-$session-$dispatch_id"
mkdir -p -- "$evidence_dir"
chmod 700 -- "$evidence_dir"
pre_capture="$evidence_dir/pre.txt"
tmux capture-pane -p -J -t "$pane_id" -S - >"$pre_capture"

pre_out=""
pre_rc=0
pre_out=$(python3 -B "$helper" pre-check --dispatch-id="$dispatch_id" --pre="$pre_capture") || pre_rc=$?
if ((pre_rc != 0)); then
  pre_reason=$(kv_get reason "$pre_out")
  printf '%s; no paste sent; inspect %s\n' "${pre_reason:-pre-check failed}" "$evidence_dir" >&2
  exit "$pre_rc"
fi

followup=$(<"$followup_file")
[[ -n ${followup//[[:space:]]/} ]] || { echo "follow-up is empty: $followup_file" >&2; exit 2; }

directive="$evidence_dir/directive.txt"
{
  printf '%s Acknowledge this exact turn first by emitting %s. ' "$start_marker" "$accepted_marker"
  printf '%s' "$followup"
  printf ' %s' "$end_marker"
} >"$directive"
chmod 600 -- "$directive"
buffer_loaded=1
tmux load-buffer -b "$buffer" -- "$directive"
tmux paste-buffer -p -b "$buffer" -t "$pane_id" -d
buffer_loaded=0
rm -f -- "$directive"
directive=""

prep_attempts=${ORCHESTRATION_PREPARE_ATTEMPTS:-40}
prep_delay=${ORCHESTRATION_PREPARE_DELAY_SECONDS:-0.025}
prep_capture="$evidence_dir/prepared.txt"
prep_state="ambiguous"
prep_reason=""
for ((attempt = 1; attempt <= prep_attempts; attempt++)); do
  tmux capture-pane -p -J -t "$pane_id" -S - >"$prep_capture"
  prep_out=$(python3 -B "$helper" classify --dispatch-id="$dispatch_id" --pre="$pre_capture" --capture="$prep_capture")
  prep_state=$(kv_get delivery_state "$prep_out")
  prep_reason=$(kv_get reason "$prep_out")
  if [[ $prep_state != "ambiguous" && ! ($prep_state == "partial_paste" && $attempt -lt 3) ]]; then
    break
  fi
  if ((attempt < prep_attempts)); then
    sleep "$prep_delay"
  fi
done

case $prep_state in
  unsubmitted)
    ;;
  submitted)
    printf 'dispatch_id=%s\ndelivery_state=submitted\nenter_presses=0\nevidence_dir=%s\n' "$dispatch_id" "$evidence_dir"
    exit 0
    ;;
  queued)
    if [[ ${ORCHESTRATION_ALLOW_QUEUED:-0} == "1" ]]; then
      printf 'dispatch_id=%s\ndelivery_state=queued\nenter_presses=0\nevidence_dir=%s\n' "$dispatch_id" "$evidence_dir"
      exit 0
    fi
    printf 'dispatch is queued by client (delivery_state=queued); no Enter sent; inspect %s\n' "$evidence_dir" >&2
    exit 1
    ;;
  blocked_modal)
    printf '%s (delivery_state=blocked_modal); no Enter sent; inspect %s\n' "${prep_reason:-blocked by modal}" "$evidence_dir" >&2
    exit 75
    ;;
  partial_paste)
    printf 'partial paste detected before Enter (delivery_state=partial_paste); no Enter sent; inspect %s\n' "$evidence_dir" >&2
    exit 1
    ;;
  *)
    printf 'paste not verified in active composer (delivery_state=%s); no Enter sent; inspect %s\n' "$prep_state" "$evidence_dir" >&2
    exit 1
    ;;
esac

tmux send-keys -t "$pane_id" Enter
verify_first_rc=0
ORCHESTRATION_VERIFY_DIR="$evidence_dir/verify-first" "$verify" "$session" "$dispatch_id" "$pre_capture" >"$evidence_dir/verify-first.out" 2>"$evidence_dir/verify-first.err" || verify_first_rc=$?
if ((verify_first_rc == 0)); then
  first_verify_state=$(kv_get delivery_state "$(<"$evidence_dir/verify-first.out")")
  printf 'dispatch_id=%s\ndelivery_state=%s\nenter_presses=1\nevidence_dir=%s\n' "$dispatch_id" "${first_verify_state:-submitted}" "$evidence_dir"
  exit 0
fi
if ((verify_first_rc != 1)); then
  cat -- "$evidence_dir/verify-first.err" >&2
  exit "$verify_first_rc"
fi

after_first="$evidence_dir/after-first-enter.txt"
tmux capture-pane -p -J -t "$pane_id" -S - >"$after_first"
after_out=$(python3 -B "$helper" classify --dispatch-id="$dispatch_id" --pre="$pre_capture" --capture="$after_first")
after_state=$(kv_get delivery_state "$after_out")
after_reason=$(kv_get reason "$after_out")
tab_to_queue=$(kv_get tab_to_queue "$after_out")

case $after_state in
  submitted)
    printf 'dispatch_id=%s\ndelivery_state=submitted\nenter_presses=1\nevidence_dir=%s\n' "$dispatch_id" "$evidence_dir"
    exit 0
    ;;
  accepted_active)
    echo "dispatch was accepted but the clean composer has not returned; no second Enter sent; inspect $evidence_dir" >&2
    exit 1
    ;;
  queued)
    if [[ ${ORCHESTRATION_ALLOW_QUEUED:-0} == "1" ]]; then
      printf 'dispatch_id=%s\ndelivery_state=queued\nenter_presses=1\nevidence_dir=%s\n' "$dispatch_id" "$evidence_dir"
      exit 0
    fi
    printf 'dispatch is queued by client (delivery_state=queued); no second Enter sent; inspect %s\n' "$evidence_dir" >&2
    exit 1
    ;;
  unsubmitted)
    if [[ $tab_to_queue == "1" ]]; then
      printf 'worker turn is active (tab to queue); no second Enter sent; inspect %s\n' "$evidence_dir" >&2
      exit 1
    fi
    tmux send-keys -t "$pane_id" Enter
    verify_second_rc=0
    ORCHESTRATION_VERIFY_DIR="$evidence_dir/verify-second" "$verify" "$session" "$dispatch_id" "$pre_capture" >"$evidence_dir/verify-second.out" 2>"$evidence_dir/verify-second.err" || verify_second_rc=$?
    if ((verify_second_rc == 0)); then
      second_verify_state=$(kv_get delivery_state "$(<"$evidence_dir/verify-second.out")")
      printf 'dispatch_id=%s\ndelivery_state=%s\nenter_presses=2\nevidence_dir=%s\n' "$dispatch_id" "${second_verify_state:-submitted}" "$evidence_dir"
      exit 0
    fi
    if ((verify_second_rc != 1)); then
      cat -- "$evidence_dir/verify-second.err" >&2
      exit "$verify_second_rc"
    fi
    after_second="$evidence_dir/after-second-enter.txt"
    tmux capture-pane -p -J -t "$pane_id" -S - >"$after_second"
    second_out=$(python3 -B "$helper" classify --dispatch-id="$dispatch_id" --pre="$pre_capture" --capture="$after_second")
    second_state=$(kv_get delivery_state "$second_out")
    second_reason=$(kv_get reason "$second_out")
    case $second_state in
      submitted)
        printf 'dispatch_id=%s\ndelivery_state=submitted\nenter_presses=2\nevidence_dir=%s\n' "$dispatch_id" "$evidence_dir"
        exit 0
        ;;
      queued)
        if [[ ${ORCHESTRATION_ALLOW_QUEUED:-0} == "1" ]]; then
          printf 'dispatch_id=%s\ndelivery_state=queued\nenter_presses=2\nevidence_dir=%s\n' "$dispatch_id" "$evidence_dir"
          exit 0
        fi
        printf 'dispatch is queued by client after two Enter presses (delivery_state=queued); inspect %s\n' "$evidence_dir" >&2
        exit 1
        ;;
      blocked_modal)
        printf '%s (delivery_state=blocked_modal); inspect %s\n' "${second_reason:-blocked by modal}" "$evidence_dir" >&2
        exit 75
        ;;
      *)
        printf 'directive is not verified after two Enter presses (delivery_state=%s); inspect %s\n' "$second_state" "$evidence_dir" >&2
        exit 1
        ;;
    esac
    ;;
  blocked_modal)
    printf '%s (delivery_state=blocked_modal); no second Enter sent; inspect %s\n' "${after_reason:-blocked by modal}" "$evidence_dir" >&2
    exit 75
    ;;
  partial_paste)
    printf 'partial paste detected after first Enter (delivery_state=partial_paste); no second Enter sent; inspect %s\n' "$evidence_dir" >&2
    exit 1
    ;;
  *)
    printf '%s (delivery_state=%s); no second Enter sent; inspect %s\n' "${after_reason:-ambiguous pane state}" "$after_state" "$evidence_dir" >&2
    exit 1
    ;;
esac
