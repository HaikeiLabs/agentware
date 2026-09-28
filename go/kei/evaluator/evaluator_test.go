package evaluator

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/soypete/pedro-agentware/go/middleware"
)

// fixtureDir holds the cross-language KeiProxyEvaluator fixtures. They live
// at the repository root, outside the go/ module, so they are not part of the
// module zip.
const fixtureDir = "../../../fixtures/kei"

type authorizeCase struct {
	ID          string `json:"id"`
	DerivedFrom string `json:"derived_from"`
	Input       struct {
		User   string         `json:"user"`
		Tool   string         `json:"tool"`
		Args   map[string]any `json:"args"`
		Caller *struct {
			UserID          string `json:"user_id"`
			ParentSpan      string `json:"parent_span"`
			DelegationDepth int    `json:"delegation_depth"`
		} `json:"caller"`
	} `json:"input"`
	Proxy struct {
		Stdout        string `json:"stdout"`
		Stderr        string `json:"stderr"`
		ExitCode      int    `json:"exit_code"`
		Hang          bool   `json:"hang"`
		MissingBinary bool   `json:"missing_binary"`
	} `json:"proxy"`
	Env struct {
		OmitToken bool `json:"omit_token"`
	} `json:"env"`
	Expected struct {
		Action         string         `json:"action"`
		ReasonClass    string         `json:"reason_class"`
		Rule           string         `json:"rule"`
		Enrollment     map[string]any `json:"enrollment"`
		Invoked        bool           `json:"invoked"`
		ReasonContains string         `json:"reason_contains"`
		Argv           []string       `json:"argv"`
	} `json:"expected"`
}

type casesTable struct {
	Policy         string `json:"policy"`
	TimeoutMS      int    `json:"timeout_ms"`
	CallerDefaults struct {
		SpanID string `json:"span_id"`
	} `json:"caller_defaults"`
	ReasonClasses []string          `json:"reason_classes"`
	Canaries      map[string]string `json:"canaries"`
	ChildEnv      struct {
		Allowlist []string `json:"allowlist"`
		Stripped  []string `json:"stripped"`
	} `json:"child_env"`
	Cases []authorizeCase `json:"cases"`
}

type seededPolicy struct {
	Workspace struct {
		ID string `json:"id"`
	} `json:"workspace"`
	Agent struct {
		ID        string `json:"id"`
		Version   string `json:"version"`
		Framework string `json:"framework"`
	} `json:"agent"`
	Users map[string]struct {
		ID string `json:"id"`
	} `json:"users"`
}

func loadFixture(t *testing.T, name string, v any) {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(fixtureDir, name))
	if errors.Is(err, fs.ErrNotExist) && strings.Contains(filepath.ToSlash(mustGetwd(t)), "@v") {
		t.Skipf("kei fixtures ship with the repository, not the module zip: %v", err)
	}
	if err != nil {
		t.Fatalf("read fixture %s: %v", name, err)
	}
	if err := json.Unmarshal(data, v); err != nil {
		t.Fatalf("decode fixture %s: %v", name, err)
	}
}

func mustGetwd(t *testing.T) string {
	t.Helper()
	wd, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	return wd
}

func loadTable(t *testing.T) (casesTable, seededPolicy) {
	t.Helper()
	var table casesTable
	loadFixture(t, "authorize-cases.v1.json", &table)
	var policy seededPolicy
	loadFixture(t, table.Policy, &policy)
	return table, policy
}

func buildCaller(table casesTable, policy seededPolicy, c authorizeCase) middleware.CallerContext {
	subject := policy.Users[c.Input.User].ID
	caller := middleware.CallerContext{
		UserID:          subject,
		InvokingSubject: subject,
		Metadata: map[string]string{
			"span_id":       table.CallerDefaults.SpanID,
			"agent_id":      policy.Agent.ID,
			"agent_version": policy.Agent.Version,
			"framework":     policy.Agent.Framework,
			"workspace_id":  policy.Workspace.ID,
		},
	}
	if o := c.Input.Caller; o != nil {
		if o.UserID != "" {
			caller.UserID = o.UserID
		}
		caller.ParentSpan = o.ParentSpan
		caller.DelegationDepth = o.DelegationDepth
	}
	return caller
}

func buildEnv(table casesTable, c authorizeCase) map[string]string {
	env := map[string]string{"PATH": "/usr/bin:/bin"}
	for _, name := range table.ChildEnv.Stripped {
		env[name] = "leaked-" + name
	}
	if !c.Env.OmitToken {
		env["KEI_RUNTIME_TOKEN"] = table.Canaries["runtime_token"]
	}
	return env
}

func installFake(t *testing.T, c authorizeCase, dir string) string {
	t.Helper()
	fake := filepath.Join(dir, "kei-proxy")
	if c.Proxy.MissingBinary {
		return fake
	}
	script, err := os.ReadFile(filepath.Join(fixtureDir, "fake-kei-proxy.sh"))
	if err != nil {
		t.Fatalf("read fake proxy: %v", err)
	}
	files := map[string]string{
		"kei-proxy": string(script),
		"stdout":    c.Proxy.Stdout,
		"stderr":    c.Proxy.Stderr,
		"exit_code": strconv.Itoa(c.Proxy.ExitCode),
	}
	if c.Proxy.Hang {
		files["hang"] = ""
	}
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o755); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}
	return fake
}

type run struct {
	decision middleware.Decision
	dir      string
	logs     string
}

func runCase(t *testing.T, table casesTable, policy seededPolicy, c authorizeCase, extraEnv map[string]string) run {
	t.Helper()
	dir := t.TempDir()
	var logs bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&logs, &slog.HandlerOptions{Level: slog.LevelDebug}))
	client := &CLIClient{
		Executable: installFake(t, c, dir),
		Timeout:    time.Duration(table.TimeoutMS) * time.Millisecond,
		Env:        buildEnv(table, c),
		ExtraEnv:   extraEnv,
	}
	eval := NewKeiProxyEvaluator(client, WithLogger(logger))
	decision := eval.Evaluate(c.Input.Tool, c.Input.Args, buildCaller(table, policy, c))
	return run{decision: decision, dir: dir, logs: logs.String()}
}

func skipOnWindows(t *testing.T) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("fake kei-proxy is a POSIX sh script")
	}
}

func TestAuthorizeCases(t *testing.T) {
	skipOnWindows(t)
	table, policy := loadTable(t)
	for _, c := range table.Cases {
		t.Run(c.ID, func(t *testing.T) {
			got := runCase(t, table, policy, c, nil)
			want := c.Expected

			if string(got.decision.Action) != want.Action {
				t.Errorf("action = %q, want %q", got.decision.Action, want.Action)
			}
			class := "kei-proxy " + want.ReasonClass
			if got.decision.Reason != class && !strings.HasPrefix(got.decision.Reason, class+":") {
				t.Errorf("reason = %q, want class %q", got.decision.Reason, want.ReasonClass)
			}
			if got.decision.Rule != want.Rule {
				t.Errorf("rule = %q, want %q", got.decision.Rule, want.Rule)
			}
			if !reflect.DeepEqual(got.decision.Enrollment, want.Enrollment) {
				t.Errorf("enrollment = %v, want %v", got.decision.Enrollment, want.Enrollment)
			}
			if want.ReasonContains != "" && !strings.Contains(got.decision.Reason, want.ReasonContains) {
				t.Errorf("reason %q does not contain %q", got.decision.Reason, want.ReasonContains)
			}
			for name, canary := range table.Canaries {
				if strings.Contains(got.decision.Reason, canary) {
					t.Errorf("%s leaked into the reason", name)
				}
				if strings.Contains(got.logs, canary) {
					t.Errorf("%s leaked into the logs", name)
				}
			}

			argvData, err := os.ReadFile(filepath.Join(got.dir, "argv"))
			invoked := err == nil
			if invoked != want.Invoked {
				t.Fatalf("invoked = %v, want %v", invoked, want.Invoked)
			}
			if !invoked {
				return
			}
			argv := strings.Split(strings.TrimRight(string(argvData), "\n"), "\n")
			for _, arg := range argv {
				if strings.Contains(arg, table.Canaries["runtime_token"]) {
					t.Error("runtime token appeared in argv")
				}
			}
			if want.Argv != nil && !reflect.DeepEqual(argv, want.Argv) {
				t.Errorf("argv =\n%q\nwant\n%q", argv, want.Argv)
			}
		})
	}
}

func findCase(t *testing.T, table casesTable, id string) authorizeCase {
	t.Helper()
	for _, c := range table.Cases {
		if c.ID == id {
			return c
		}
	}
	t.Fatalf("case %s not found", id)
	return authorizeCase{}
}

func readChildEnv(t *testing.T, dir string) map[string]string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(dir, "env"))
	if err != nil {
		t.Fatalf("read env: %v", err)
	}
	env := map[string]string{}
	for _, line := range strings.Split(string(data), "\n") {
		if k, v, ok := strings.Cut(line, "="); ok {
			env[k] = v
		}
	}
	return env
}

func TestChildEnvCarriesTokenAndOnlyAllowlistedVars(t *testing.T) {
	skipOnWindows(t)
	table, policy := loadTable(t)
	got := runCase(t, table, policy, findCase(t, table, "allow_member_read"), nil)
	env := readChildEnv(t, got.dir)

	if env["KEI_RUNTIME_TOKEN"] != table.Canaries["runtime_token"] {
		t.Error("KEI_RUNTIME_TOKEN not passed through the child env")
	}
	for _, name := range table.ChildEnv.Stripped {
		if _, ok := env[name]; ok {
			t.Errorf("%s reached the child", name)
		}
	}
	allowed := map[string]bool{"PWD": true, "SHLVL": true, "_": true, "OLDPWD": true}
	for _, k := range table.ChildEnv.Allowlist {
		allowed[k] = true
	}
	for k := range env {
		if !allowed[k] {
			t.Errorf("unexpected child env var %s", k)
		}
	}
}

func TestExtraEnvIsPassedExplicitly(t *testing.T) {
	skipOnWindows(t)
	table, policy := loadTable(t)
	got := runCase(t, table, policy, findCase(t, table, "allow_member_read"), map[string]string{"AWS_PROFILE": "fixture"})
	if readChildEnv(t, got.dir)["AWS_PROFILE"] != "fixture" {
		t.Error("extra env not passed to the child")
	}
}

func TestContractConstantsMatchFixture(t *testing.T) {
	table, _ := loadTable(t)
	if !reflect.DeepEqual(AuthorizeChildEnvAllowlist, table.ChildEnv.Allowlist) {
		t.Errorf("AuthorizeChildEnvAllowlist = %v, want %v", AuthorizeChildEnvAllowlist, table.ChildEnv.Allowlist)
	}
	classes := make([]string, len(ReasonClasses))
	for i, c := range ReasonClasses {
		classes[i] = string(c)
	}
	if !reflect.DeepEqual(classes, table.ReasonClasses) {
		t.Errorf("ReasonClasses = %v, want %v", classes, table.ReasonClasses)
	}
}

type clientFunc func(context.Context, AuthorizeRequest) (map[string]any, error)

func (f clientFunc) Authorize(ctx context.Context, req AuthorizeRequest) (map[string]any, error) {
	return f(ctx, req)
}

func quietEvaluator(c Client) *KeiProxyEvaluator {
	return NewKeiProxyEvaluator(c, WithLogger(slog.New(slog.NewTextHandler(&bytes.Buffer{}, nil))))
}

func TestClientErrorDeniesWithProxyError(t *testing.T) {
	eval := quietEvaluator(clientFunc(func(context.Context, AuthorizeRequest) (map[string]any, error) {
		return nil, errors.New("proxy unreachable")
	}))
	got := eval.Evaluate("github.read", nil, middleware.CallerContext{UserID: "U123"})
	if got.Action != middleware.ActionDeny {
		t.Fatalf("action = %q, want deny", got.Action)
	}
	if got.Reason != "kei-proxy proxy_error: authorize failed: proxy unreachable" {
		t.Errorf("reason = %q", got.Reason)
	}
}

func TestTypedAuthorizeErrorKeepsItsClass(t *testing.T) {
	eval := quietEvaluator(clientFunc(func(context.Context, AuthorizeRequest) (map[string]any, error) {
		return nil, &AuthorizeError{Class: ReasonProxyTimeout, Detail: "no answer"}
	}))
	got := eval.Evaluate("github.read", nil, middleware.CallerContext{UserID: "U123"})
	if got.Reason != "kei-proxy proxy_timeout: no answer" {
		t.Errorf("reason = %q", got.Reason)
	}
}

func TestRequestCarriesInvokingSubjectAndDerivedResource(t *testing.T) {
	var seen AuthorizeRequest
	eval := quietEvaluator(clientFunc(func(_ context.Context, req AuthorizeRequest) (map[string]any, error) {
		seen = req
		return map[string]any{"decision": "allow"}, nil
	}))
	eval.Evaluate("github.read", map[string]any{"owner": "acme", "repo": "pipe"},
		middleware.CallerContext{UserID: "U123", InvokingSubject: "U_HUMAN"})
	if seen.UserID != "U_HUMAN" || seen.Resource != "github:repo:acme/pipe" || seen.Action != "execute" {
		t.Errorf("request = %+v", seen)
	}
}

func TestResourcesTouched(t *testing.T) {
	got := ResourcesTouched("github.comment", map[string]any{"owner": "acme", "repo": "sales-pipeline", "issue_number": 123})
	want := []string{"github:repo:acme/sales-pipeline", "github:issue:123"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("ResourcesTouched = %v, want %v", got, want)
	}
	if got := ResourcesTouched("github_create_issue", map[string]any{"issue_number": 5}); !reflect.DeepEqual(got, []string{"github:issue:5"}) {
		t.Errorf("underscore tool: %v", got)
	}
}

func TestToolArgsDigestMatchesPython(t *testing.T) {
	// python: json.dumps({"e": "\U0001F600", "z": [" ", 1, True, None]},
	//                    sort_keys=True, separators=(",", ":"))
	got := ToolArgsDigest(map[string]any{"z": []any{" ", 1, true, nil}, "e": "\U0001F600"})
	if want := "303e9fb3a4f9a08e4b959e3558a1705fe29840918a9605df263ef27eaabfa25f"; got != want {
		t.Errorf("ToolArgsDigest = %s, want %s", got, want)
	}
}
