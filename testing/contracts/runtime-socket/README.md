# Runtime socket client contract fixtures

Shared, language-neutral fixtures for the harness → `kei-proxy serve` Unix
socket client and the tool-manifest sync. Python
(`haikeilabs.agentware.kei.runtime_socket`, `tool_sync`), TypeScript
(`typescript/src/kei/socketAuthorizeClient.ts`, `toolSync.ts`) and Go
(`go/kei/runtimesocket`, `go/kei/toolsync`) each replay every case here and
must produce identical results. A change to any fixture is a contract change:
update all three languages in the same PR, or in linked PRs that land
together.

| File | Asserts |
| --- | --- |
| `constants.json` | The default socket path, environment variable names, header, schemas, routes, default timeouts, tool-sync path, media type and backoff numbers. |
| `client-cases.json` | Replay cases for the socket client: every request the client sends (method, path, `X-Kei-Session`, JSON body) and what each daemon answer turns into (a result, or a refusal with its reason class and daemon error code). |
| `tool-sync-cases.json` | `PUT /api/v1/runtime/tools`: request shape, the empty-manifest refusal, retry/backoff, token redaction, config from the environment, and egress/ambient proxy selection. |

## Socket path

Every SDK uses the same rule. The client's socket path is the explicit
argument, else `KEI_RUNTIME_SOCKET_PATH`, else `/run/kei-proxy/runtime.sock`
(the `kei-proxy serve` default). The authorization-client selectors
(`select_authorization_client` / `selectAuthorizationClient` /
`runtimesocket.SelectAuthorizationClient`) use the socket only when a path is
configured explicitly (argument or `KEI_RUNTIME_SOCKET_PATH`); otherwise they
return the `kei-proxy authorize` subprocess client. A configured socket that is
down is a refusal, never a silent fall back to the subprocess.

## Running `client-cases.json`

For each case:

1. Unless `"daemon": "absent"`, start a fake daemon on a fresh Unix socket
   (keep the path short: macOS caps it near 104 bytes). It answers incoming
   requests with `exchanges[i].response` in order and records each request as
   `{method, path, session, body}`: `session` is the `X-Kei-Session` header or
   `""`, `body` is the parsed JSON body or `null` when there is none. A
   response is `{status, body}` (JSON), `{status, raw, content_type}`
   (verbatim bytes), `{status}` (no body), or `{"stall": true}` (never
   answers).
2. Build a client on that socket with env `case.env` (default:
   `{"KEI_RUNTIME_TOKEN": <file token>}`) and timeout `case.timeout_ms`
   (default `constants.socket.default_timeout_ms`).
3. Run `steps` in order on that one client. Arguments are snake_case;
   `session` is `{source, external_id, session_id}`, `request` holds the
   authorize fields. Expectations:
   - `{"result": X}`: the call returns `X` (`open_session` → `{session_id}`,
     `close_session` / `forget_session` → `null`,
     `stream_chat_completion` → the list of yielded lines).
   - `{"error": {"reason_class", "code"}}`: the call raises the SDK's socket
     error with that reason class and daemon code (`""` when none). Its
     message must not contain the token.
   - `{"invalid_argument": true}`: a caller error (`ValueError` / `Error` /
     a plain `error`), raised before any request.
4. The recorded requests must equal `exchanges[*].request` exactly, and every
   recorded `Authorization` header must be `Bearer <file token>`.

## Running `tool-sync-cases.json`

- `sync_cases`: serve `responses` in order from a plain HTTP server on
  `127.0.0.1` (`{"network_error": true}` drops the connection without
  answering), with the control-plane URL pointed at it, the file token,
  `attempts` / `backoff_ms` (defaults from `constants.tool_sync`), and the
  sleep function replaced by a recorder. The manifest is `manifest` (an
  object) or `manifest_text` (JSON text, passed as text). Expect either
  `{sent, tool_count, counts}` or an error whose message contains
  `error_contains` (and `error_contains_also`, when set) and never the token.
  Exactly `expect_requests` requests arrive, each matching `expected_request`
  with the manifest as its body, and the recorded sleeps equal
  `expect_sleeps_ms`.
- `config_cases`: build the config from `env` alone (no process environment);
  expect `tools_url`, `egress_proxy`, `no_proxy`, or an error containing
  `error_contains`.
- `proxy_cases`: the proxy URL the sync uses for `url` with `env` as the whole
  environment, or `null` for a direct connection. `KEI_PROXY_EGRESS_PROXY`,
  when set, decides alone (`KEI_PROXY_NO_PROXY` bypasses it; the ambient
  variables are ignored). Without it, `HTTPS_PROXY` / `https_proxy` (or
  `HTTP_PROXY` / `http_proxy` for an `http` URL) apply, bypassed by
  `NO_PROXY` / `no_proxy`. A no-proxy entry matches the host exactly or as a
  domain suffix (a leading `.` is optional), case-insensitively; `*` matches
  every host. Python resolves only the `egress` layer itself and leaves the
  `ambient` layer to httpx, so its runner skips `ambient` cases.
