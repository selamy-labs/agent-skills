#!/usr/bin/env bash
# Compatibility entry point: bazel-sandbox.sh <branch> -- [startup options] <command> [args...] | --reap | --gc
# The lifecycle wrapper is bazel_sandbox.py; exec keeps the PID for signal delivery.
set -euo pipefail
here=$(cd -- "$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")" && pwd)
exec python3 -I -B "$here/bazel_sandbox.py" "$@"
