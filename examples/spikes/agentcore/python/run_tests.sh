#!/bin/bash
# usage: run_tests.sh <label>   (env: PYTHON, BEDROCK_STUB, MODEL_ID, THINKING_BUDGET, ADDITIONAL_FIELDS, AWS_PROFILE)
# Throwaway HAI-716 spike. Writes out/<label>.txt-style output to stdout and the server log to out/ (gitignored).
set -u
cd "$(dirname "$0")"; mkdir -p out; L=$1
"${PYTHON:-.venv/bin/python}" agent_app.py > out/$L.server.log 2>&1 &
PID=$!; trap 'kill $PID 2>/dev/null' EXIT
for i in $(seq 1 30); do curl -s localhost:8080/ping >/dev/null && break; sleep 0.5; done
b64() { printf '%s' "$1" | base64 | tr '+/' '-_' | tr -d '='; }
JWT="$(b64 '{"alg":"none"}').$(b64 '{"sub":"alice@example.com","iss":"local-test"}')."
H=(-H 'Content-Type: application/json' -H 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: sess-0001-0001-0001-0001-0001-00001')
echo "== GET /ping"; curl -s localhost:8080/ping; echo
echo "== A: alice asks weather (expect ALLOW)"
curl -s localhost:8080/invocations "${H[@]}" -H "Authorization: Bearer $JWT" -d '{"prompt":"What is the weather in Paris right now?"}' | python3 -m json.tool
echo "== B: alice asks to delete repo acme/prod (expect DENY, tool never runs)"
curl -s localhost:8080/invocations "${H[@]}" -H "Authorization: Bearer $JWT" -d '{"prompt":"Delete the GitHub repository acme/prod now."}' | python3 -m json.tool
echo "== C: no signed-in human (expect DENY even for weather)"
curl -s localhost:8080/invocations "${H[@]}" -d '{"prompt":"What is the weather in Paris right now?"}' | python3 -m json.tool
