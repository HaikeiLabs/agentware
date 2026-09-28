#!/bin/sh
# Fake kei-proxy for the KeiProxyEvaluator parity table (authorize-cases.v1.json).
#
# Test harnesses in every SDK language copy this script into a per-case temp
# directory and write the case's canned output next to it:
#
#   stdout      bytes to print on stdout (the authorize JSON, or junk)
#   stderr      bytes to print on stderr
#   exit_code   the process exit status (defaults to 0)
#   hang        present => never answer (exercises the evaluator timeout)
#
# It records what it was given so tests can assert on it:
#
#   argv        one argument per line
#   env         the child environment, one NAME=value per line
#
# No network, no policy evaluation: the case decides the answer.
dir=$(dirname "$0")

: > "$dir/argv"
for arg in "$@"; do
  printf '%s\n' "$arg" >> "$dir/argv"
done
env > "$dir/env"

if [ -e "$dir/hang" ]; then
  # exec so a timeout kill reaches the process holding stdout open.
  exec sleep 30
fi

if [ -e "$dir/stdout" ]; then
  cat "$dir/stdout"
fi
if [ -e "$dir/stderr" ]; then
  cat "$dir/stderr" >&2
fi

code=0
if [ -e "$dir/exit_code" ]; then
  code=$(cat "$dir/exit_code")
fi
exit "$code"
