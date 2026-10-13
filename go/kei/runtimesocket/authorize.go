package runtimesocket

import (
	"context"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/soypete/pedro-agentware/go/kei/evaluator"
)

// AuthorizationClient is the socket client as an evaluator.Client.
//
// evaluator.KeiProxyEvaluator names the acting user (UserID, the invoking
// subject) on every call. AuthorizationClient opens, or reuses, the daemon
// session for Identity{Source, UserID} and authorizes in it.
type AuthorizationClient struct {
	// Source is the session source for every subject, e.g. "openwebui" or
	// "discord": the identity namespace the daemon resolves UserID in.
	Source string
	// Socket is the daemon client. It must be non-nil.
	Socket *Client
}

var _ evaluator.Client = (*AuthorizationClient)(nil)

// String names the source and the socket path, never the token.
func (a *AuthorizationClient) String() string {
	return fmt.Sprintf("runtimesocket.AuthorizationClient{Source: %q, Socket: %v}", a.Source, a.Socket)
}

// Authorize authorizes one call for req.UserID over the socket.
func (a *AuthorizationClient) Authorize(ctx context.Context, req evaluator.AuthorizeRequest) (map[string]any, error) {
	if a.Socket == nil {
		return nil, refusal(evaluator.ReasonProxyUnavailable, "no socket client configured")
	}
	if req.UserID == "" {
		return nil, refusal(evaluator.ReasonProxyError, "no invoking subject to open a session for")
	}
	session, err := a.Socket.OpenSession(ctx, Identity{Source: a.Source, ExternalID: req.UserID})
	if err != nil {
		return nil, err
	}
	return a.Socket.Authorize(ctx, session, req)
}

// SelectOptions configures SelectAuthorizationClient.
type SelectOptions struct {
	// SocketPath selects the socket client explicitly.
	SocketPath string
	// Env is where KEI_RUNTIME_SOCKET_PATH and KEI_RUNTIME_TOKEN are read
	// (by either client). Nil means the process environment.
	Env map[string]string
	// Timeout bounds one authorize call. Default DefaultTimeout.
	Timeout time.Duration
	// Executable and ExpectedSHA256 configure the subprocess client; see
	// evaluator.CLIClient.
	Executable     string
	ExpectedSHA256 string
}

// SelectAuthorizationClient returns the evaluator.Client for
// evaluator.KeiProxyEvaluator: the socket client when a socket is configured
// (opts.SocketPath, or KEI_RUNTIME_SOCKET_PATH in opts.Env), otherwise the
// `kei-proxy authorize` subprocess client. The choice is made once, here: a
// configured socket that is down is a refusal, never a fall back to the
// subprocess. An empty source is ErrInvalidSource.
func SelectAuthorizationClient(source string, opts SelectOptions) (evaluator.Client, error) {
	if strings.TrimSpace(source) == "" {
		return nil, fmt.Errorf("select authorization client: %w", ErrInvalidSource)
	}
	path := opts.SocketPath
	if path == "" {
		if opts.Env != nil {
			path = opts.Env[SocketPathEnv]
		} else {
			path = os.Getenv(SocketPathEnv)
		}
	}
	if path != "" {
		return &AuthorizationClient{
			Source: source,
			Socket: &Client{SocketPath: path, Timeout: opts.Timeout, Env: opts.Env},
		}, nil
	}
	return &evaluator.CLIClient{
		Executable:     opts.Executable,
		ExpectedSHA256: opts.ExpectedSHA256,
		Timeout:        opts.Timeout,
		Env:            opts.Env,
	}, nil
}
