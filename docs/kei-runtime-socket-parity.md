# kei-proxy runtime socket client: SDK parity

The `kei-proxy serve` daemon listens on a Unix socket. Each SDK ships the
same client for it, plus the tool-manifest sync a harness runs before it
serves. All four replay the shared cases in
[`testing/contracts/runtime-socket`](../testing/contracts/runtime-socket/README.md),
so the request and response shapes, error classes and fail-closed rules
cannot drift apart.

| | Python (`haikeilabs.agentware.kei`) | TypeScript (`@haikeilabs/agentware`) | Go (`kei/runtimesocket`, `kei/toolsync`) | Rust (`haikeilabs_agentware::runtimesocket`) |
| --- | --- | --- | --- | --- |
| Client | `KeiProxySocketAuthorizeClient(socket_path=, timeout=, model_timeout=, env=)` | `new KeiProxySocketAuthorizeClient({ socketPath, timeoutMs, modelTimeoutMs, env })` | `&runtimesocket.Client{SocketPath, Timeout, ModelTimeout, Env}` | `runtimesocket::Client::new(socket_path)` (fields `timeout`, `model_timeout`, `env`, `caller_timeout`) |
| Identity / session | `KeiProxySocketIdentity(source, external_id)`, `KeiProxySocketSession` | `{ source, externalId }`, `KeiProxySocketSession` | `runtimesocket.Identity{Source, ExternalID}`, `runtimesocket.Session` | `runtimesocket::Identity { source, external_id }`, `runtimesocket::Session` |
| `PUT /v1/session` | `await open_session(identity)` | `await openSession(identity)` | `OpenSession(ctx, identity)` | `open_session(identity)` |
| `DELETE /v1/session` | `await close_session(session)` | `await closeSession(session)` | `CloseSession(ctx, session)` | `close_session(&session)` |
| Drop cached session | `forget_session(identity)` | `forgetSession(identity)` | `ForgetSession(identity)` | `forget_session(&identity)` |
| `POST /v1/authorize` | `await authorize(session, tool=, action=, ...)` | `await authorize(session, { tool, action, ... })` | `Authorize(ctx, session, evaluator.AuthorizeRequest{...})` | `authorize(&session, &fields)` |
| `POST /v1/credentials:release` | `await release_credential(session, connector_id=, capability=, resource=, ...)` | `await releaseCredential(session, { connectorId, capability, resource, ... })` | `ReleaseCredential(ctx, session, ReleaseRequest{...})` | `release_credential(&session, &ReleaseRequest { .. })` |
| `GET /v1/model-config` | `await model_config(agent_id=None, profile_id=None)` | `await modelConfig(agentId?, profileId?)` | `ModelConfig(ctx, agentID, profileID)` | `model_config(agent_id, profile_id)` |
| `GET /v1/model-profiles` | `await list_model_profiles(agent_id=None)` | `await listModelProfiles(agentId?)` | `ListModelProfiles(ctx, agentID)` | `list_model_profiles(agent_id)` |
| `GET /v1/models` | `await list_models(agent_id=None, profile_id=None)` | `await listModels(agentId?, profileId?)` | `ListModels(ctx, agentID, profileID)` | `list_models(agent_id, profile_id)` |
| `POST /v1/chat/completions` | `await chat_completion(session, payload)` | `await chatCompletion(session, payload)` | `ChatCompletion(ctx, session, payload)` | `chat_completion(&session, &payload)` |
| Streamed chat (SSE `data:` lines) | `async for line in stream_chat_completion(session, payload)` | `for await (const line of streamChatCompletion(session, payload))` | `StreamChatCompletion(ctx, session, payload, func(line string) error)` | `stream_chat_completion(&session, &payload, callback)` |
| `GET /v1/policy-bundle/status` | `await policy_bundle_status()` | `await policyBundleStatus()` | `PolicyBundleStatus(ctx)` | `policy_bundle_status()` |
| Refusal error | `KeiProxySocketError` (`reason_class`, `code`) ⊂ `KeiProxyAuthorizeError` | `KeiProxySocketError` (`reasonClass`, `code`) ⊂ `KeiProxyAuthorizeError` | `*runtimesocket.Error` (`Class`, `Code`), unwraps to `*evaluator.AuthorizeError` | `runtimesocket::SocketError` (`kind`, `code`) |
| Evaluator adapter | `KeiProxySocketAuthorizationClient(source, client)` | `new KeiProxySocketAuthorizationClient(source, client)` | `&runtimesocket.AuthorizationClient{Source, Socket}` | `KeiProxyEvaluator::new(runtimesocket::Client)` |
| Socket or CLI selector | `select_authorization_client(source, socket_path=, env=, ...)` | `selectAuthorizationClient(source, { socketPath, env, ... })` | `runtimesocket.SelectAuthorizationClient(source, runtimesocket.SelectOptions{...})` | — |
| CLI fallback | `KeiProxyAuthorizeClient` | `KeiProxyAuthorizeClient` | `evaluator.CLIClient` | — |
| Tool sync config | `ToolManifestSyncConfig.from_env(env)` | `ToolManifestSyncConfig.fromEnv(env)` | `toolsync.ConfigFromEnv(env)` | `toolsync::Config::from_env(env)` |
| Tool sync | `sync_tool_manifest(manifest, config, expect_tools=)` | `await syncToolManifest(manifest, config, { expectTools })` | `toolsync.Sync(ctx, manifest, cfg, expectTools)` / `toolsync.SyncDocument(...)` | `toolsync::sync(manifest, config, expect_tools)` |
| Tool sync error | `ToolManifestSyncError` | `ToolManifestSyncError` | wraps `toolsync.ErrSyncFailed` (+ `ErrEmptyManifest`, `ErrInvalidManifest`, `ErrInvalidConfig`) | `toolsync::SyncError` |

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
- **Model profile selection.** `agent_id` and `profile_id` are query parameters on
  `GET /v1/model-config`, `/v1/model-profiles`, and `/v1/models`. The chat
  completion selector, when used, is supplied in the JSON request fields
  `agent_id` and `profile_id`; all four SDKs otherwise preserve caller payloads.
  Omitting `profile_id` selects the chosen agent's default. The profile-list
  response is `{"model_profiles": [...]}`. SDK model-config/list methods remove
  credential, API-key, and secret-reference fields before returning data; they
  never surface API keys.
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
- **Rust is Unix only.** The socket client dials with
  `std::os::unix::net::UnixStream`, so the crate does not build on Windows.
  `toolsync` works everywhere. Rust has no `kei-proxy authorize` CLI
  fallback or socket-or-CLI selector yet, so `KeiProxyEvaluator` needs a
  configured socket.
- **Python adapter.** `KeiProxySocketAuthorizationClient.authorize` is
  synchronous, because Python's `KeiProxyEvaluator` is. It runs the async
  client on a worker-thread event loop.
- **Go context.** A cancelled caller context is a refusal that wraps
  `ctx.Err()`: `proxy_unavailable`, or `proxy_timeout` when the caller's
  deadline passed.
