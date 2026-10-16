# AgentCore Runtime + Bedrock Converse spike (HAI-716)

> **Throwaway spike. Not a supported API.** Nothing here is part of the
> `haikeilabs-agentware` package, the Go module, or the TypeScript SDK, and it
> is not covered by CI. It exists so the HAI-716 findings can be reproduced.
> Do not import from it. Ticket: https://linear.app/haikeilabs/issue/HAI-716

## What it proves

- **Python, inside a locally served `bedrock-agentcore` app.** `python/agent_app.py`
  runs `BedrockAgentCoreApp` on `127.0.0.1:8080` (the same `/invocations` + `/ping`
  contract AgentCore Runtime calls). Every model-requested tool call goes through
  agentware `MiddlewareImpl` -> `KeiProxyEvaluator` -> `InMemoryAuditor` before it
  can run:
  - **allow**: signed-in user asks for weather -> tool runs, audit `allow`.
  - **deny**: same user asks to delete a repo -> tool never runs, audit `deny`.
  - **no-JWT deny**: no `Authorization: Bearer` header -> no human subject ->
    denied even for the allowed tool (no fallback to the agent's identity).
- **Real Bedrock** on Amazon Nova 2 Lite (`us.amazon.nova-2-lite-v1:0`), with and
  without Nova `reasoningConfig`; 3/3 cases as expected.
- **Go**: `go/main.go` calls Bedrock Converse via aws-sdk-go-v2 (Nova 2 Lite) and
  routes the returned `toolUse` through the agentware Go middleware: allow/deny + audit.
- Offline: `python/thinking_roundtrip_check.py` shows the Converse backend must
  replay the signed `reasoningContent` block on the tool-result turn, and that
  agentware's `Message` has no slot for it (the spike uses a side map).
  `python/reasoning_probe.py` shows `ReasoningAdapter` needs Converse block arrays
  flattened first.

See `sample-output.txt` for trimmed output.

## What it does NOT verify

- **Claude Haiku 4.5 and extended thinking on Bedrock**: blocked; the AWS account
  has not submitted the Anthropic use-case form, so Converse returns
  `ResourceNotFoundException`. The thinking round trip is checked offline only.
- **No deployed AgentCore**: no Runtime deployment, Gateway, Policy, Identity,
  Memory or OTel/Observability. The app is served locally only, and the JWT is
  an unsigned test token (in AgentCore the inbound authorizer would validate it).
- **The Kei decision is fake.** `FakeKeiProxyClient` mimics `kei-proxy authorize`
  answers in-process; no real kei-proxy, policy bundle or Kei control plane is called.
  The Go probe uses a local agentware `Policy`, not Kei.

## Layout

```
python/   agent_app.py, bedrock_backend.py (Converse <-> agentware llm types),
          run_tests.sh, thinking_roundtrip_check.py, reasoning_probe.py, requirements.txt
go/       main.go with its own go.mod (replace -> ../../../../go), so the root go/
          module's dependencies are untouched
```

The Python scripts import agentware from this checkout's `python/src`
(`haikeilabs.agentware`) via `sys.path`; the spike deps live only in
`python/requirements.txt`.

## Run it

### Python, stub mode (no AWS)

```sh
cd examples/spikes/agentcore/python
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
BEDROCK_STUB=1 ./run_tests.sh stub          # cases A/B/C against a scripted Converse stub
.venv/bin/python thinking_roundtrip_check.py
```

`run_tests.sh` starts the app on `127.0.0.1:8080`, sends three `/invocations`
requests, prints the JSON responses and writes the server log to `out/` (gitignored).
Set `PYTHON=/path/to/python` to use a different interpreter.

### Python, real Bedrock

Uses AWS SSO with the profile `AdministratorAccess-726299224953` in `us-east-1`
(model access for Nova 2 Lite must be enabled on the account):

```sh
aws sso login --profile AdministratorAccess-726299224953
export AWS_PROFILE=AdministratorAccess-726299224953 AWS_REGION=us-east-1
MODEL_ID=us.amazon.nova-2-lite-v1:0 ./run_tests.sh live-nova
MODEL_ID=us.amazon.nova-2-lite-v1:0 \
  ADDITIONAL_FIELDS='{"reasoningConfig":{"type":"enabled","maxReasoningEffort":"low"}}' \
  ./run_tests.sh live-nova-reasoning
.venv/bin/python reasoning_probe.py          # DeepSeek-R1 reasoningContent shape
# Claude (blocked until the Anthropic use-case form is filed):
# MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0 THINKING_BUDGET=1024 ./run_tests.sh live-claude
```

### Go

```sh
cd examples/spikes/agentcore/go
go vet ./... && go build .
AWS_PROFILE=AdministratorAccess-726299224953 go run .
```
