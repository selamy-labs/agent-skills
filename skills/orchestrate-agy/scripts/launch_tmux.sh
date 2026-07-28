#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 SESSION CWD PROMPT_FILE AGY [ARG ...]" >&2
  exit 2
}

if [[ ${1:-} == __run ]]; then
  shift
  (($# >= 3)) || usage
  prompt_file=$1
  cli=$2
  dispatch_id=$3
  shift 3
  prompt=$(<"$prompt_file")
  prompt="[dispatch:$dispatch_id]"$'\n'"Acknowledge this exact turn first by emitting [dispatch-accepted:$dispatch_id]."$'\n'"$prompt"$'\n'"[dispatch-end:$dispatch_id]"
  exec "$cli" "$@" --prompt-interactive "$prompt"
fi

(($# >= 4)) || usage
session=$1
cwd=$2
prompt_file=$3
cli=$4
shift 4

[[ $session =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "invalid tmux session name" >&2; exit 2; }
[[ $cwd == /* ]] || { echo "cwd must be absolute: $cwd" >&2; exit 2; }
[[ $prompt_file == /* ]] || { echo "prompt path must be absolute: $prompt_file" >&2; exit 2; }
[[ $cli == /* ]] || { echo "CLI path must be absolute: $cli" >&2; exit 2; }
[[ -d $cwd ]] || { echo "cwd is not a directory: $cwd" >&2; exit 2; }
[[ -r $prompt_file ]] || { echo "prompt is not readable: $prompt_file" >&2; exit 2; }
[[ -x $cli ]] || { echo "CLI is not executable: $cli" >&2; exit 2; }
tmux has-session -t "=$session" 2>/dev/null && { echo "tmux session already exists: $session" >&2; exit 2; }

script_dir=$(cd -- "$(dirname -- "$0")" && pwd -P)
runner="$script_dir/$(basename -- "$0")"
dispatch_id="agy-$(date -u +%Y%m%dT%H%M%SZ)-$$-$RANDOM"
evidence_dir="${TMPDIR:-/tmp}/orchestrate-$session-$dispatch_id"
mkdir -p "$evidence_dir"
: >"$evidence_dir/pre.txt"
command_string=
for arg in "$runner" __run "$prompt_file" "$cli" "$dispatch_id" "$@"; do
  printf -v quoted '%q' "$arg"
  command_string+="${command_string:+ }$quoted"
done

tmux new-session -d -s "$session" -c "$cwd" "$command_string"
printf 'dispatch_id=%s\npre_capture=%s\n' "$dispatch_id" "$evidence_dir/pre.txt"
tmux list-panes -t "=$session" -F '#{pane_pid} #{pane_current_path} #{pane_current_command} #{pane_dead}'
