//go:build !windows

package runtimesocket

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/soypete/pedro-agentware/go/kei/evaluator"
)

type clientCases struct {
	ContractVersion int          `json:"contract_version"`
	Token           string       `json:"token"`
	Cases           []clientCase `json:"cases"`
}

type clientCase struct {
	Name      string             `json:"name"`
	Daemon    string             `json:"daemon"`
	Env       *map[string]string `json:"env"`
	TimeoutMS int                `json:"timeout_ms"`
	Steps     []struct {
		Op     string          `json:"op"`
		Args   json.RawMessage `json:"args"`
		Expect struct {
			Result          json.RawMessage `json:"result"`
			Error           *expectedError  `json:"error"`
			InvalidArgument bool            `json:"invalid_argument"`
		} `json:"expect"`
	} `json:"steps"`
	Exchanges []struct {
		Request  json.RawMessage `json:"request"`
		Response fakeResponse    `json:"response"`
	} `json:"exchanges"`
}

type expectedError struct {
	ReasonClass string `json:"reason_class"`
	Code        string `json:"code"`
}

type stepArgs struct {
	Source     string         `json:"source"`
	ExternalID string         `json:"external_id"`
	Payload    map[string]any `json:"payload"`
	Session    struct {
		Source     string `json:"source"`
		ExternalID string `json:"external_id"`
		SessionID  string `json:"session_id"`
	} `json:"session"`
	Request struct {
		Tool            string `json:"tool"`
		Action          string `json:"action"`
		Resource        string `json:"resource"`
		SpanID          string `json:"span_id"`
		ParentSpan      string `json:"parent_span"`
		DelegationDepth int    `json:"delegation_depth"`
		AgentID         string `json:"agent_id"`
		Framework       string `json:"framework"`
		ToolArgsDigest  string `json:"tool_args_digest"`
	} `json:"request"`
}

func (a stepArgs) session() Session {
	return Session{Source: a.Session.Source, ExternalID: a.Session.ExternalID, SessionID: a.Session.SessionID}
}

// runStep runs one fixture op and returns its result in fixture form.
func runStep(ctx context.Context, c *Client, op string, a stepArgs) (any, error) {
	switch op {
	case "open_session":
		s, err := c.OpenSession(ctx, Identity{Source: a.Source, ExternalID: a.ExternalID})
		if err != nil {
			return nil, err
		}
		return map[string]any{"session_id": s.SessionID}, nil
	case "close_session":
		return nil, c.CloseSession(ctx, a.session())
	case "forget_session":
		c.ForgetSession(Identity{Source: a.Source, ExternalID: a.ExternalID})
		return nil, nil
	case "authorize":
		r := a.Request
		return c.Authorize(ctx, a.session(), evaluator.AuthorizeRequest{
			Tool: r.Tool, Action: r.Action, Resource: r.Resource, SpanID: r.SpanID,
			ParentSpan: r.ParentSpan, DelegationDepth: r.DelegationDepth, AgentID: r.AgentID,
			Framework: r.Framework, ToolArgsDigest: r.ToolArgsDigest,
		})
	case "list_models":
		return c.ListModels(ctx)
	case "chat_completion":
		return c.ChatCompletion(ctx, a.session(), a.Payload)
	case "stream_chat_completion":
		lines := []string{}
		err := c.StreamChatCompletion(ctx, a.session(), a.Payload, func(line string) error {
			lines = append(lines, line)
			return nil
		})
		return lines, err
	case "policy_bundle_status":
		return c.PolicyBundleStatus(ctx)
	}
	return nil, fmt.Errorf("unknown op %q", op)
}

func TestClientContractCases(t *testing.T) {
	var fixture clientCases
	loadFixture(t, "client-cases.json", &fixture)
	if fixture.ContractVersion != 1 || len(fixture.Cases) == 0 {
		t.Fatalf("unexpected fixture: version %d, %d cases", fixture.ContractVersion, len(fixture.Cases))
	}
	for _, tc := range fixture.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			var daemon *fakeDaemon
			path := shortSocketPath(t)
			if tc.Daemon != "absent" {
				responses := make([]fakeResponse, len(tc.Exchanges))
				for i, ex := range tc.Exchanges {
					responses[i] = ex.Response
				}
				daemon = startDaemon(t, responses...)
				path = daemon.path
			}
			env := map[string]string{evaluator.RuntimeTokenEnv: fixture.Token}
			if tc.Env != nil {
				env = *tc.Env
			}
			client := &Client{SocketPath: path, Env: env}
			if tc.TimeoutMS > 0 {
				client.Timeout = time.Duration(tc.TimeoutMS) * time.Millisecond
			}

			for i, step := range tc.Steps {
				var args stepArgs
				if err := json.Unmarshal(step.Args, &args); err != nil {
					t.Fatalf("step %d args: %v", i, err)
				}
				got, err := runStep(context.Background(), client, step.Op, args)
				switch {
				case step.Expect.InvalidArgument:
					var se *Error
					if err == nil || errors.As(err, &se) {
						t.Errorf("step %d %s: want a plain caller error, got %v", i, step.Op, err)
					}
				case step.Expect.Error != nil:
					var se *Error
					if !errors.As(err, &se) {
						t.Fatalf("step %d %s: want *Error, got %v (result %v)", i, step.Op, err, got)
					}
					if string(se.Class) != step.Expect.Error.ReasonClass || se.Code != step.Expect.Error.Code {
						t.Errorf("step %d %s: got class %q code %q, want %q %q", i, step.Op, se.Class, se.Code,
							step.Expect.Error.ReasonClass, step.Expect.Error.Code)
					}
					if strings.Contains(err.Error(), fixture.Token) {
						t.Errorf("step %d %s: error leaks the token: %v", i, step.Op, err)
					}
				default:
					if err != nil {
						t.Fatalf("step %d %s: unexpected error %v", i, step.Op, err)
					}
					var want any
					if err := json.Unmarshal(step.Expect.Result, &want); err != nil {
						t.Fatalf("step %d expected result: %v", i, err)
					}
					if g := normalize(t, got); !reflect.DeepEqual(g, want) {
						t.Errorf("step %d %s: got %#v, want %#v", i, step.Op, g, want)
					}
				}
			}

			if daemon == nil {
				return
			}
			recorded := daemon.requests()
			want := make([]any, len(tc.Exchanges))
			for i, ex := range tc.Exchanges {
				if err := json.Unmarshal(ex.Request, &want[i]); err != nil {
					t.Fatalf("exchange %d request: %v", i, err)
				}
			}
			if got := normalize(t, recorded); !reflect.DeepEqual(got, normalize(t, want)) {
				t.Errorf("requests:\n got %v\nwant %v", got, want)
			}
			for i, r := range recorded {
				if r.Authorization != "Bearer "+fixture.Token {
					t.Errorf("request %d: Authorization header is not the bearer token", i)
				}
			}
		})
	}
}
