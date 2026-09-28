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
#   hang_pid    (hang only) pid of a backgrounded grandchild that holds stdout
#               open; it must die with the process group on timeout
#
# Clients give the child no PATH, so every command is an absolute path and
# the script's directory comes from parameter expansion, not dirname.
# No network, no policy evaluation: the case decides the answer.
dir=${0%/*}

: > "$dir/argv"
for arg in "$@"; do
  printf '%s\n' "$arg" >> "$dir/argv"
done
/usr/bin/env > "$dir/env"

if [ -e "$dir/hang" ]; then
  # The grandchild shares the process group and the stdout pipe: only a
  # process-group kill ends it. exec so the direct child is a sleep too.
  /bin/sleep 30 &
  printf '%s\n' "$!" > "$dir/hang_pid"
  exec /bin/sleep 30
fi

if [ -e "$dir/stdout" ]; then
  /bin/cat "$dir/stdout"
fi
if [ -e "$dir/stderr" ]; then
  /bin/cat "$dir/stderr" >&2
fi

code=0
if [ -e "$dir/exit_code" ]; then
  code=$(/bin/cat "$dir/exit_code")
fi
exit "$code"
