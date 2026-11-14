#!/usr/bin/env bash
# Run a first-agent example end to end with no network and no API key.
#
# Usage: scripts/ci/run-first-agent-offline.sh go|typescript
#
# Starts fixtures/first-agent/fake-openai-hn.py (a scripted OpenAI-compatible
# model plus Hacker News fixtures on one local port), points the example at it
# with OPENAI_BASE_URL and HN_BASE_URL, and checks that the run exits 0, prints
# the scripted summary and ends on the expected deny line.
# The TypeScript example needs the SDK built and `npm ci` run in its directory.
set -euo pipefail

lang="${1:?usage: $0 go|typescript}"
root="$(cd "$(dirname "$0")/../.." && pwd)"
work="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/first-agent.XXXXXX")"
port_file="$work/port"

python3 "$root/fixtures/first-agent/fake-openai-hn.py" --port-file "$port_file" 2>"$work/server.log" &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true; rm -rf "$work"' EXIT

for _ in $(seq 1 40); do
  [ -s "$port_file" ] && break
  sleep 0.25
done
if [ ! -s "$port_file" ]; then
  echo "fake model did not start" >&2
  cat "$work/server.log" >&2
  exit 1
fi
base="http://127.0.0.1:$(cat "$port_file")"

export OPENAI_API_KEY=offline-ci-key
export OPENAI_BASE_URL="$base/v1"
export HN_BASE_URL="$base/v0"
export MODEL=gpt-4o-mini
# Keep every request on the loopback stub.
export NO_PROXY='*' no_proxy='*'
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy

answer='Offline summary: Fixture story one leads today.'
case "$lang" in
  go)
    output="$(cd "$root/examples/go/first-agent" && go run . 2>"$work/run.log")" || {
      cat "$work/run.log" >&2
      exit 1
    }
    allow_line='audit tool=hn_top_stories decision=allow rule=local:allow success=true'
    final_prefix='audit tool=hn_post_comment decision=deny rule=local:deny-hn_post_comment success=false'
    ;;
  typescript)
    output="$(cd "$root/examples/typescript/first-agent" && npm start --silent 2>"$work/run.log")" || {
      cat "$work/run.log" >&2
      exit 1
    }
    allow_line='"tool_name":"hn_top_stories","args":{},"decision":{"action":"allow","rule":"read-stories"'
    final_prefix='DENIED hn_post_comment: denied by policy: Only reading stories is allowed'
    ;;
  *)
    echo "unknown example: $lang" >&2
    exit 2
    ;;
esac

printf '%s\n' "$output"
fail() {
  echo "first-agent ($lang) offline run: $1" >&2
  cat "$work/server.log" >&2
  exit 1
}
printf '%s\n' "$output" | grep -Fxq "$answer" || fail "missing the scripted summary line"
printf '%s\n' "$output" | grep -Fq "$allow_line" || fail "missing the hn_top_stories allow audit"
last="$(printf '%s\n' "$output" | tail -n 1)"
case "$last" in
  "$final_prefix"*) ;;
  *) fail "last line is not the deny line: $last" ;;
esac
echo "first-agent ($lang) offline run: ok"
