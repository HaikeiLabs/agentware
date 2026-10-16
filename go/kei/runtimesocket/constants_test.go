package runtimesocket

import (
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/haikeilabs/agentware/go/kei/evaluator"
)

// fixtureDir holds the cross-language fixtures. They live at the repository
// root, outside the go/ module, so they are not part of the module zip.
const fixtureDir = "../../../testing/contracts/runtime-socket"

func loadFixture(t *testing.T, name string, v any) {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(fixtureDir, name))
	if errors.Is(err, fs.ErrNotExist) && inModuleCache(t) {
		t.Skipf("contract fixtures ship with the repository, not the module zip: %v", err)
	}
	if err != nil {
		t.Fatalf("read fixture %s: %v", name, err)
	}
	if err := json.Unmarshal(data, v); err != nil {
		t.Fatalf("decode fixture %s: %v", name, err)
	}
}

// inModuleCache reports whether the package runs from a downloaded module
// (a "<module>@<version>" directory), where `go test all` in a consumer would
// otherwise fail on the missing repository fixtures. Inside the repository a
// missing fixture is still a failure.
func inModuleCache(t *testing.T) bool {
	t.Helper()
	wd, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	return strings.Contains(filepath.ToSlash(wd), "@v")
}

func TestConstantsMatchContract(t *testing.T) {
	var c struct {
		ContractVersion int `json:"contract_version"`
		Socket          struct {
			DefaultPath           string `json:"default_path"`
			PathEnv               string `json:"path_env"`
			TokenEnv              string `json:"token_env"`
			SessionHeader         string `json:"session_header"`
			SessionNotFound       string `json:"session_not_found"`
			DefaultTimeoutMS      int64  `json:"default_timeout_ms"`
			DefaultModelTimeoutMS int64  `json:"default_model_timeout_ms"`
		} `json:"socket"`
		Schemas struct {
			Session            string `json:"session"`
			AuthorizeRequest   string `json:"authorize_request"`
			AuthorizeResponse  string `json:"authorize_response"`
			PolicyBundleStatus string `json:"policy_bundle_status"`
		} `json:"schemas"`
		ReasonClasses []string `json:"socket_error_reason_classes"`
	}
	loadFixture(t, "constants.json", &c)

	known := map[string]bool{}
	for _, rc := range evaluator.ReasonClasses {
		known[string(rc)] = true
	}
	for _, rc := range c.ReasonClasses {
		if !known[rc] {
			t.Errorf("reason class %q is not an evaluator.ReasonClass", rc)
		}
	}
	checks := []struct {
		name      string
		got, want any
	}{
		{"contract_version", c.ContractVersion, 1},
		{"default_path", DefaultSocketPath, c.Socket.DefaultPath},
		{"path_env", SocketPathEnv, c.Socket.PathEnv},
		{"token_env", evaluator.RuntimeTokenEnv, c.Socket.TokenEnv},
		{"session_header", SessionHeader, c.Socket.SessionHeader},
		{"session_not_found", SessionNotFound, c.Socket.SessionNotFound},
		{"default_timeout", DefaultTimeout, time.Duration(c.Socket.DefaultTimeoutMS) * time.Millisecond},
		{"default_model_timeout", DefaultModelTimeout, time.Duration(c.Socket.DefaultModelTimeoutMS) * time.Millisecond},
		{"session schema", SessionSchema, c.Schemas.Session},
		{"authorize_request schema", AuthorizeRequestSchema, c.Schemas.AuthorizeRequest},
		{"authorize_response schema", AuthorizeResponseSchema, c.Schemas.AuthorizeResponse},
		{"policy_bundle_status schema", PolicyBundleStatusSchema, c.Schemas.PolicyBundleStatus},
	}
	for _, ch := range checks {
		if !reflect.DeepEqual(ch.got, ch.want) {
			t.Errorf("%s = %v, contract says %v", ch.name, ch.got, ch.want)
		}
	}
}
