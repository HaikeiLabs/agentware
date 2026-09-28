package evaluator

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// RuntimeTokenEnv carries the runtime installation credential to kei-proxy.
const RuntimeTokenEnv = "KEI_RUNTIME_TOKEN"

// DefaultTimeout bounds one authorize call when CLIClient.Timeout is zero.
const DefaultTimeout = 10 * time.Second

// maxStdout bounds how much authorize output is read.
const maxStdout = 1 << 20

// maxExecutable bounds how much of a pinned executable is hashed.
const maxExecutable = 256 << 20

// AuthorizeChildEnvAllowlist lists the parent variables the kei-proxy child
// may inherit. PATH and HOME are deliberately absent, as are KEI_PROXY_*
// identity variables: identity travels as flags.
var AuthorizeChildEnvAllowlist = []string{
	"TZ",
	RuntimeTokenEnv,
	"KEI_RUNTIME_CONTROL_PLANE_URL",
	"KEI_RUNTIME_VERSION",
	"KEI_API_URL",
	"KEI_PROXY_REGISTRY",
	"KEI_PROXY_AUDIT",
	"KEI_PROXY_AUDIT_MAX_BYTES",
	"KEI_PROXY_PROVIDER_USER",
	"KEI_CREDENTIAL_STORE_INSTALLATION_ID",
	"KEI_AWS_SECRETS_MANAGER_ENDPOINT",
}

// CLIClient runs `kei-proxy authorize` once per call. The runtime token
// reaches the child only through its environment, never argv.
//
// The child sees only the allowlisted environment (no PATH, no HOME), so the
// executable is resolved to an absolute path in the parent. The child runs in
// its own process group, and a timeout SIGKILLs the whole group. With
// ExpectedSHA256 set, the binary is re-proved before every spawn: the path
// must equal its realpath, it is opened with O_NOFOLLOW, must be a regular
// file, and its SHA-256 is read from that descriptor and compared in constant
// time; any drift denies with ReasonPinMismatch without spawning. What
// remains is the hash-then-exec race; close it by making the binary's
// directory and its ancestors writable only by the deploy owner.
type CLIClient struct {
	// Executable is the path or name of the kei-proxy binary. Default
	// "kei-proxy". A bare name is looked up in the absolute entries of the
	// parent PATH (Env["PATH"] when Env is set).
	Executable string
	// ExpectedSHA256 pins the binary to this SHA-256 (64 hex digits). The
	// Executable must then be an absolute, symlink-free path. A malformed pin
	// or a relative path denies every call with ReasonPinMismatch.
	ExpectedSHA256 string
	// Timeout bounds one call. Default DefaultTimeout.
	Timeout time.Duration
	// Env is the parent environment allowlisted variables are drawn from.
	// Nil means the process environment.
	Env map[string]string
	// ExtraEnv is passed to the child verbatim, for settings outside the
	// allowlist (for example a secret-store profile).
	ExtraEnv map[string]string
}

var _ Client = (*CLIClient)(nil)

func (c *CLIClient) lookupParent(k string) (string, bool) {
	if c.Env != nil {
		v, ok := c.Env[k]
		return v, ok
	}
	return os.LookupEnv(k)
}

// ChildEnv returns the exact environment the kei-proxy child receives.
func (c *CLIClient) ChildEnv() map[string]string {
	lookup := c.lookupParent
	env := make(map[string]string, len(AuthorizeChildEnvAllowlist)+len(c.ExtraEnv))
	for _, k := range AuthorizeChildEnvAllowlist {
		if v, ok := lookup(k); ok {
			env[k] = v
		}
	}
	for k, v := range c.ExtraEnv {
		env[k] = v
	}
	return env
}

// Authorize runs one authorize call. Every failure is an *AuthorizeError.
func (c *CLIClient) Authorize(ctx context.Context, req AuthorizeRequest) (map[string]any, error) {
	env := c.ChildEnv()
	if env[RuntimeTokenEnv] == "" {
		return nil, &AuthorizeError{Class: ReasonMissingToken, Detail: RuntimeTokenEnv + " is not set"}
	}
	name := c.Executable
	if name == "" {
		name = "kei-proxy"
	}
	if c.ExpectedSHA256 != "" {
		if err := proveExecutable(name, c.ExpectedSHA256); err != nil {
			return nil, err
		}
	}
	parentPath, _ := c.lookupParent("PATH")
	executable, ok := resolveExecutable(name, parentPath)
	if !ok {
		return nil, &AuthorizeError{Class: ReasonProxyUnavailable, Detail: "kei-proxy was not found on PATH"}
	}
	timeout := c.Timeout
	if timeout <= 0 {
		timeout = DefaultTimeout
	}

	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, executable, AuthorizeArgv(req)...)
	cmd.Env = make([]string, 0, len(env))
	for k, v := range env {
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	var stdout limitedBuffer
	stdout.limit = maxStdout
	cmd.Stdout = &stdout
	// stderr is free-form and may carry secrets: it is discarded.
	cmd.Stderr = nil
	cmd.WaitDelay = 100 * time.Millisecond
	startInNewGroup(cmd)

	runErr := cmd.Run()
	if ctx.Err() != nil && errors.Is(ctx.Err(), context.DeadlineExceeded) {
		return nil, &AuthorizeError{Class: ReasonProxyTimeout, Detail: fmt.Sprintf("no answer within %s", timeout)}
	}
	exitCode := 0
	if runErr != nil {
		var exitErr *exec.ExitError
		if !errors.As(runErr, &exitErr) {
			return nil, &AuthorizeError{Class: ReasonProxyUnavailable, Detail: "could not start kei-proxy"}
		}
		exitCode = exitErr.ExitCode()
		if exitCode < 0 {
			return nil, &AuthorizeError{Class: ReasonProxyError, Detail: "kei-proxy terminated by a signal"}
		}
	}
	if stdout.overflow {
		return nil, &AuthorizeError{Class: ReasonMalformedResponse, Detail: "stdout exceeded the size limit"}
	}
	return ParseAuthorizeOutput(stdout.Bytes(), exitCode)
}

// resolveExecutable returns an absolute path for name. Names containing a
// separator resolve against the working directory; bare names are looked up
// in the absolute entries of pathList (relative entries are skipped).
func resolveExecutable(name, pathList string) (string, bool) {
	if filepath.IsAbs(name) {
		return name, true
	}
	if strings.ContainsRune(name, os.PathSeparator) || strings.ContainsRune(name, '/') {
		abs, err := filepath.Abs(name)
		return abs, err == nil
	}
	for _, dir := range filepath.SplitList(pathList) {
		if dir == "" || !filepath.IsAbs(dir) {
			continue
		}
		candidate := filepath.Join(dir, name)
		if fi, err := os.Stat(candidate); err == nil && isExecutable(fi) {
			return candidate, true
		}
	}
	return "", false
}

func pinMismatch(detail string) *AuthorizeError {
	return &AuthorizeError{Class: ReasonPinMismatch, Detail: detail}
}

// proveExecutable re-proves the pinned executable immediately before a spawn.
func proveExecutable(path, pin string) error {
	expected, err := hex.DecodeString(pin)
	if err != nil || len(expected) != sha256.Size {
		return pinMismatch("the pin is not 64 hex digits")
	}
	if !filepath.IsAbs(path) {
		return pinMismatch("a pinned executable must be an absolute path")
	}
	resolved, err := filepath.EvalSymlinks(path)
	if err != nil {
		return pinMismatch("executable is missing")
	}
	if resolved != path {
		return pinMismatch("executable path is not canonical")
	}
	f, err := openNoFollow(path)
	if err != nil {
		return pinMismatch("executable could not be opened without following links")
	}
	defer func() { _ = f.Close() }()
	fi, err := f.Stat()
	if err != nil || !fi.Mode().IsRegular() {
		return pinMismatch("executable is not a regular file")
	}
	h := sha256.New()
	n, err := io.Copy(h, io.LimitReader(f, maxExecutable+1))
	if err != nil {
		return pinMismatch("executable could not be read")
	}
	if n > maxExecutable {
		return pinMismatch("executable exceeds the size limit")
	}
	if subtle.ConstantTimeCompare(h.Sum(nil), expected) != 1 {
		return pinMismatch("executable digest does not match the pin")
	}
	return nil
}

// AuthorizeArgv returns the argv (after the executable) for one call.
// AgentID and WorkspaceID are not sent: kei-proxy (>= 0.1.11) resolves the
// agent from the runtime installation and workspace scope from the token.
func AuthorizeArgv(req AuthorizeRequest) []string {
	argv := []string{
		"authorize",
		"--user", req.UserID,
		"--tool", req.Tool,
		"--action", req.Action,
		"--resource", req.Resource,
	}
	depth := ""
	if req.DelegationDepth > 0 {
		depth = strconv.Itoa(req.DelegationDepth)
	}
	optional := [][2]string{
		{"--span-id", req.SpanID},
		{"--invoking-subject", req.InvokingSubject},
		{"--parent-span", req.ParentSpan},
		{"--delegation-depth", depth},
		{"--agent-version", req.AgentVersion},
		{"--framework", req.Framework},
		{"--tool-args-digest", req.ToolArgsDigest},
		{"--resources", strings.Join(req.Resources, ",")},
	}
	for _, kv := range optional {
		if kv[1] != "" {
			argv = append(argv, kv[0], kv[1])
		}
	}
	return argv
}

// ParseAuthorizeOutput validates one authorize result against kei-proxy's
// exit-code contract: 0 for allow/enrollment_required, 1 for deny, and 1 with
// empty stdout for every error.
func ParseAuthorizeOutput(stdout []byte, exitCode int) (map[string]any, error) {
	if exitCode != 0 && exitCode != 1 {
		return nil, &AuthorizeError{Class: ReasonProxyError, Detail: fmt.Sprintf("kei-proxy exited %d", exitCode)}
	}
	text := bytes.TrimSpace(stdout)
	if len(text) == 0 {
		if exitCode != 0 {
			return nil, &AuthorizeError{Class: ReasonProxyError, Detail: fmt.Sprintf("kei-proxy exited %d", exitCode)}
		}
		return nil, &AuthorizeError{Class: ReasonEmptyResponse, Detail: "kei-proxy printed nothing"}
	}
	var parsed any
	if err := json.Unmarshal(text, &parsed); err != nil {
		return nil, &AuthorizeError{Class: ReasonMalformedResponse, Detail: "stdout is not JSON"}
	}
	result, ok := parsed.(map[string]any)
	if !ok {
		return nil, &AuthorizeError{Class: ReasonMalformedResponse, Detail: "stdout is not a JSON object"}
	}
	if d, ok := result["decision"].(string); ok && exitCode != 0 && isAffirmative(strings.ToLower(strings.TrimSpace(d))) {
		return nil, &AuthorizeError{Class: ReasonExitMismatch, Detail: fmt.Sprintf("%s with exit %d", d, exitCode)}
	}
	return result, nil
}

// limitedBuffer keeps at most limit bytes and records whether more arrived.
type limitedBuffer struct {
	bytes.Buffer
	limit    int
	overflow bool
}

func (b *limitedBuffer) Write(p []byte) (int, error) {
	if room := b.limit - b.Len(); len(p) > room {
		b.overflow = true
		if room > 0 {
			b.Buffer.Write(p[:room])
		}
		return len(p), nil
	}
	return b.Buffer.Write(p)
}
