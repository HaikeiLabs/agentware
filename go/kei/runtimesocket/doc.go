// Package runtimesocket is a client for the `kei-proxy serve` daemon over its
// Unix socket.
//
// It is the Go port of pedro_agentware.kei.runtime_socket and of the
// TypeScript SDK's KeiProxySocketAuthorizeClient. All three replay the shared
// fixtures in testing/contracts/runtime-socket. It covers the routes a harness
// needs:
//
//   - PUT / DELETE /v1/session: open and close a named runtime session
//     (kei.session/v2) for an explicit source / external_id Identity. The
//     daemon resolves the Kei user from that identity; requests in the session
//     carry X-Kei-Session and never name the user themselves.
//   - POST /v1/authorize: one kei.authorize-request/v1 decision.
//   - GET /v1/models and POST /v1/chat/completions: the model routes, each
//     call made inside a session so the daemon authorizes and audits
//     model.invoke for the session's user.
//   - GET /v1/policy-bundle/status: which bundle the daemon holds.
//
// Fail closed: an unreachable daemon, a timeout, a non-2xx answer, a body that
// is not a JSON object, or a response with the wrong schema is an *Error. It
// unwraps to an *evaluator.AuthorizeError, so evaluator.KeiProxyEvaluator
// turns every one of them into a DENY with the matching reason class. A
// session the daemon no longer knows (session_not_found) is reopened once; a
// second miss fails closed. A cancelled caller context is also an *Error
// (proxy_unavailable, or proxy_timeout for a caller deadline) that wraps
// ctx.Err(), so errors.Is(err, context.Canceled) holds and it still denies.
//
// Token rule: requests carry the runtime token (KEI_RUNTIME_TOKEN) in the
// Authorization header and nowhere else. It is read on every request, never
// stored on the client, and never appears in a log line, an error message or
// String(). Errors carry only the daemon's error code: daemon messages and
// response bodies are never copied into them.
//
// Socket path rule: Client.SocketPath, else KEI_RUNTIME_SOCKET_PATH, else
// DefaultSocketPath. SelectAuthorizationClient uses the socket only when a
// path is configured (SelectOptions.SocketPath or KEI_RUNTIME_SOCKET_PATH);
// otherwise it returns the `kei-proxy authorize` subprocess client. A
// configured socket that is down is a refusal, never a fall back.
//
// The socket is Unix only. On Windows every request fails with a
// proxy_unavailable *Error that wraps ErrUnsupportedPlatform.
package runtimesocket
