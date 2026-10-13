# kei-proxy runtime socket client: SDK parity

The `kei-proxy serve` daemon listens on a Unix socket. Each SDK ships the
same client for it, plus the tool-manifest sync a harness runs before it
serves. All three replay the shared cases in
[`testing/contracts/runtime-socket`](../testing/contracts/runtime-socket/README.md),
so the request and response shapes, error classes and fail-closed rules
cannot drift apart.

| | Python (`pedro_agentware.kei`) | TypeScript (`@haikeilabs/agentware`) | Go (`kei/runtimesocket`, `kei/toolsync`) |
| --- | --- | --- | --- |
| Client | `KeiProxySocketAuthorizeClient(socket_path=, timeout=, model_timeout=, env=)` | `new KeiProxySocketAuthorizeClient({ socketPath, timeoutMs, modelTimeoutMs, env })` | `&runtimesocket.Client{SocketPath, Timeout, ModelTimeout, Env}` |
| Identity / session | `KeiProxySocketIdentity(source, external_id)`, `KeiProxySocketSession` | `{ source, externalId }`, `KeiProxySocketSession` | `runtimesocket.Identity{Source, ExternalID}`, `runtimesocket.Session` |
| `PUT /v1/session` | `await open_session(identity)` | `await openSession(identity)` | `OpenSession(ctx, identity)` |
| `DELETE /v1/session` | `await close_session(session)` | `await closeSession(session)` | `CloseSession(ctx, session)` |
| Drop cached session | `forget_session(identity)` | `forgetSession(identity)` | `ForgetSession(identity)` |
| `POST /v1/authorize` | `await authorize(session, tool=, action=, ...)` | `await authorize(session, { tool, action, ... })` | `Authorize(ctx, session, evaluator.AuthorizeRequest{...})` |
| `GET /v1/models` | `await list_models()` | `await listModels()` | `ListModels(ctx)` |
| `POST /v1/chat/completions` | `await chat_completion(session, payload)` | `await chatCompletion(session, payload)` | `ChatCompletion(ctx, session, payload)` |
| Streamed chat (SSE `data:` lines) | `async for line in stream_chat_completion(session, payload)` | `for await (const line of streamChatCompletion(session, payload))` | `StreamChatCompletion(ctx, session, payload, func(line string) error)` |
| `GET /v1/policy-bundle/status` | `await policy_bundle_status()` | `await policyBundleStatus()` | `PolicyBundleStatus(ctx)` |
| Refusal error | `KeiProxySocketError` (`reason_class`, `code`) ⊂ `KeiProxyAuthorizeError` | `KeiProxySocketError` (`reasonClass`, `code`) ⊂ `KeiProxyAuthorizeError` | `*runtimesocket.Error` (`Class`, `Code`), unwraps to `*evaluator.AuthorizeError` |
| Evaluator adapter | `KeiProxySocketAuthorizationClient(source, client)` | `new KeiProxySocketAuthorizationClient(source, client)` | `&runtimesocket.AuthorizationClient{Source, Socket}` |
| Socket or CLI selector | `select_authorization_client(source, socket_path=, env=, ...)` | `selectAuthorizationClient(source, { socketPath, env, ... })` | `runtimesocket.SelectAuthorizationClient(source, runtimesocket.SelectOptions{...})` |
| CLI fallback | `KeiProxyAuthorizeClient` | `KeiProxyAuthorizeClient` | `evaluator.CLIClient` |
| Tool sync config | `ToolManifestSyncConfig.from_env(env)` | `ToolManifestSyncConfig.fromEnv(env)` | `toolsync.ConfigFromEnv(env)` |
| Tool sync | `sync_tool_manifest(manifest, config, expect_tools=)` | `await syncToolManifest(manifest, config, { expectTools })` | `toolsync.Sync(ctx, manifest, cfg, expectTools)` / `toolsync.SyncDocument(...)` |
| Tool sync error | `ToolManifestSyncError` | `ToolManifestSyncError` | wraps `toolsync.ErrSyncFailed` (+ `ErrEmptyManifest`, `ErrInvalidManifest`, `ErrInvalidConfig`) |

## The shared contract

- **Socket path.** The explicit path, else `KEI_RUNTIME_SOCKET_PATH`, else
  `/run/kei-proxy/runtime.sock` (the `kei-proxy serve` default). The
  selectors use the socket only when a path is configured explicitly (the
  argument or `KEI_RUNTIME_SOCKET_PATH`). Otherwise they return the
  `kei-proxy authorize` subprocess client. A configured socket that is down
  is a refusal, never a silent fall back.
- **Sessions.** `kei.session/v2`, one cached per `(source, external_id)`.
  Requests in a session carry `X-Kei-Session` and never name the user. A
  `session_not_found` answer (a 404 error code, or an authorize deny with
  that `reason_code`) reopens the session once. A second miss fails closed.
- **Fail closed.** These all raise the socket error, so `KeiProxyEvaluator`
  denies with the matching reason:

  | Failure | Reason class |
  | --- | --- |
  | An unreachable daemon | `proxy_unavailable` |
  | A timeout | `proxy_timeout` |
  | A non-2xx answer | `proxy_error`, carrying the daemon's `code` |
  | A body that is not a JSON object, or the wrong schema | `malformed_response` |
  | No `KEI_RUNTIME_TOKEN` | `missing_token` |

  Only an explicit `permit`/`allow` is an allow. A deny is returned, not
  raised.
- **Token.** `KEI_RUNTIME_TOKEN` is read per request and sent only as
  `Authorization: Bearer`. It never appears in a log line, an error, a repr,
  `JSON.stringify`/`util.inspect`, or `String()`. Errors carry the daemon's
  error code, never its message or a body.
- **Ambient proxies never reroute socket requests.** Tool sync, which goes to
  the control plane over HTTP(S), routes through `KEI_PROXY_EGRESS_PROXY`
  unless `KEI_PROXY_NO_PROXY` bypasses the host. Without an egress proxy it
  uses `HTTPS_PROXY` / `HTTP_PROXY` and `NO_PROXY`. It refuses to `PUT` an
  empty manifest when tools are expected, because the PUT replaces the
  installation's tools.

## Platform and language notes

- **Go is Unix only.** On Windows every socket call refuses with
  `proxy_unavailable`, wrapping `runtimesocket.ErrUnsupportedPlatform`.
  `toolsync` works everywhere.
- **Python adapter.** `KeiProxySocketAuthorizationClient.authorize` is
  synchronous, because Python's `KeiProxyEvaluator` is. It runs the async
  client on a worker-thread event loop.
- **Go context.** A cancelled caller context is a refusal that wraps
  `ctx.Err()`: `proxy_unavailable`, or `proxy_timeout` when the caller's
  deadline passed.
