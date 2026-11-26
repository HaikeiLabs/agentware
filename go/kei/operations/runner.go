// Copyright 2026 Haikei Labs
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package operations

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"regexp"
	"strings"

	connection "github.com/HaikeiLabs/kei-connector-contracts/gen/go/kei/connection"
	contracts "github.com/HaikeiLabs/kei-connector-contracts/gen/go/kei/contracts/v1"
	"github.com/haikeilabs/agentware/go/kei/evaluator"
	"github.com/haikeilabs/agentware/go/kei/runtimesocket"
	"github.com/haikeilabs/agentware/go/middleware"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// Authorizer is the socket-client surface the Runner needs: authorize one
// operation call and release the scoped credential for it.
// *runtimesocket.AuthorizationClient satisfies it.
type Authorizer interface {
	Authorize(ctx context.Context, req evaluator.AuthorizeRequest) (map[string]any, error)
	ReleaseCredential(ctx context.Context, userID string, req runtimesocket.ReleaseRequest) (*runtimesocket.ReleaseResult, error)
}

// ConnectorConfig describes the installed connector the Runner serves.
// Conn is the connector's connection spec (kind and header name); BaseURL
// is the provider origin the generated operations target.
type ConnectorConfig struct {
	// ConnectorID is the contract connector id, e.g. "github": the operation
	// id's prefix the runner serves.
	ConnectorID string
	// InstalledConnectorID is the installed connector's id in the policy
	// bundle (policy_set.connectors[].id), sent as the credential release's
	// connector_id. Empty falls back to ConnectorID.
	InstalledConnectorID string
	// ConnectionName is the connection the runner uses, e.g. "user_oauth".
	// It must be listed on the operation's descriptor Connections.
	ConnectionName string
	// Conn is the connection spec from the installed connector.
	Conn *contracts.Connection
	// BaseURL is the provider origin for built connections.
	BaseURL string
	// BindingVersion is the optional version of the installed tool binding
	// (from the tool manifest's binding version); when set, the runner sends
	// it on authorize and kei-proxy denies binding_stale on a mismatch.
	BindingVersion string
	// DeriveConnector releases the credential without a connector: kei-proxy
	// 0.12.0 or later derives it from the bound tool's binding, so the
	// harness never handles the installed connector's ID. It needs
	// WithToolCall (the tool and its arguments); without them Run denies
	// connector_not_named. ConnectorConfigs sets it; InstalledConnectorID is
	// then ignored.
	DeriveConnector bool
}

// AuditRecord is metadata-only: it never carries arguments, responses or
// credentials.
type AuditRecord struct {
	OperationID string
	Decision    string // "allow" or "deny"
	Outcome     string // "success", "denied" or "error"
}

// AuditSink receives one AuditRecord per run. Sink errors and panics are
// swallowed: audit must not expose execution data or alter results.
type AuditSink func(AuditRecord)

// Runner executes generated connector operations behind kei-proxy: every
// run is authorized, the scoped credential is released for exactly that
// call, and the released connection is handed to the caller's closure.
// The Runner never logs or audits the credential.
type Runner struct {
	client Authorizer
	userID string
	cfg    ConnectorConfig

	http   *http.Client
	logger *slog.Logger
	audit  AuditSink
}

// Option customizes a Runner.
type Option func(*Runner)

// WithLogger sets the runner's logger (default: discard). The runner only
// logs operation ids, decisions and reason codes — never credentials.
func WithLogger(l *slog.Logger) Option { return func(r *Runner) { r.logger = l } }

// WithAuditSink sets the metadata-only audit sink.
func WithAuditSink(s AuditSink) Option { return func(r *Runner) { r.audit = s } }

// WithHTTPClient sets the HTTP client released connections use.
func WithHTTPClient(c *http.Client) Option { return func(r *Runner) { r.http = c } }

// NewRunner builds a Runner for one connector config bound to userID (the
// invoking subject every authorize and release is attributed to).
func NewRunner(client Authorizer, userID string, cfg ConnectorConfig, opts ...Option) *Runner {
	r := &Runner{
		client: client,
		userID: userID,
		cfg:    cfg,
		http:   http.DefaultClient,
		logger: slog.New(slog.DiscardHandler),
	}
	for _, opt := range opts {
		opt(r)
	}
	return r
}

// OperationDeniedError is returned when kei-proxy denies the call. It
// carries the decision's reason code and a redacted decision summary.
type OperationDeniedError struct {
	ReasonCode string
	Decision   middleware.Decision
}

func (e *OperationDeniedError) Error() string { return "operation denied: " + e.ReasonCode }

// EnrollmentRequiredError is returned when the user must enroll with the
// provider first. Enrollment and Connect are the proxy's opaque objects —
// surface them to the user, never log them (the url is a one-time claim
// link).
type EnrollmentRequiredError struct {
	Enrollment map[string]any
	Connect    map[string]any
}

func (e *EnrollmentRequiredError) Error() string { return "enrollment required before this call" }

// ErrEnrollmentRequired is the sentinel callers can errors.Is against.
var ErrEnrollmentRequired = errors.New("enrollment required before this call")

func (e *EnrollmentRequiredError) Unwrap() error { return ErrEnrollmentRequired }

// ConnectRequiredError is returned when the user must connect (link) the
// provider account first. Connect is the proxy's opaque connect object —
// surface it to the user, never log it.
type ConnectRequiredError struct {
	Connect map[string]any
}

func (e *ConnectRequiredError) Error() string { return "connecting the provider account is required" }

// ErrConnectRequired is the sentinel callers can errors.Is against.
var ErrConnectRequired = errors.New("connecting the provider account is required")

func (e *ConnectRequiredError) Unwrap() error { return ErrConnectRequired }

// ErrUnsupportedConnection is returned for connection kinds the runner
// cannot execute (SQL today; SQL support is a follow-up).
var ErrUnsupportedConnection = errors.New("connection kind is not supported by the operation runner")

var safeReasonPattern = regexp.MustCompile(`^[a-z][a-z0-9_]{0,99}$`)

// stringOf coerces a JSON value to a string when it is one.
func stringOf(v any) string {
	s, _ := v.(string)
	return s
}

// safeReason keeps the proxy's reason code when it is a well-formed
// snake_case token; anything else falls back to a generic code.
func safeReason(raw, fallback string) string {
	if safeReasonPattern.MatchString(raw) {
		return raw
	}
	return fallback
}

// RunOption customizes one Run.
type RunOption func(*runConfig)

type runConfig struct {
	tool        string
	toolArgs    map[string]any
	toolContext map[string]any
}

// WithToolCall names the registered tool the call is authorized as and
// forwards the tool call's arguments and context. For a connector_binding
// tool, args must satisfy the bundle plan's args_schema and toolContext its
// context_schema: kei-proxy derives the operation from them, denies
// preflight_required without them, and re-derives the plan before releasing
// the credential. The runner cannot derive them from the request message.
// Both stay on the tenant-local socket and never reach an audit record.
func WithToolCall(tool string, args, toolContext map[string]any) RunOption {
	return func(c *runConfig) {
		c.tool, c.toolArgs, c.toolContext = tool, args, toolContext
	}
}

// Run authorizes and executes one operation call.
//
// op is the generated operation descriptor (resource template, method
// name, connections and transport). call receives the released, already
// authenticated connection and performs the provider call. req is the
// request message the resource template renders from. Pass WithToolCall for
// a tool registered as a connector_binding in a v2 policy bundle.
func Run[Req, Resp proto.Message](ctx context.Context, r *Runner, caller middleware.CallerContext, op connection.Op, call func(conn any) (Resp, error), req Req, opts ...RunOption) (Resp, error) {
	var zero Resp
	var rc runConfig
	for _, opt := range opts {
		opt(&rc)
	}

	decision, outcome := "deny", "denied"
	defer func() {
		if r.audit == nil {
			return
		}
		func() {
			defer func() { _ = recover() }()
			r.audit(AuditRecord{OperationID: op.ID, Decision: decision, Outcome: outcome})
		}()
	}()

	if op.ID == "" || r.userID == "" {
		return zero, denied("invalid_context")
	}
	prefix := op.ID
	if i := strings.Index(op.ID, "."); i >= 0 {
		prefix = op.ID[:i]
	}
	if prefix != r.cfg.ConnectorID {
		return zero, denied("connector_not_named")
	}
	if r.cfg.DeriveConnector && (rc.tool == "" || rc.toolArgs == nil) {
		return zero, denied("connector_not_named")
	}
	if op.ResourceTemplate == "" || op.MethodName == "" {
		return zero, denied("invalid_operation")
	}
	if !opConnects(op, r.cfg.ConnectionName) {
		return zero, denied("invalid_connection")
	}
	if op.Transport != connection.TransportHTTP && op.Transport != connection.TransportGraphQL {
		return zero, &OperationDeniedError{ReasonCode: "unsupported_connection",
			Decision: middleware.Decision{Action: middleware.ActionDeny}}
	}

	resource, err := renderResource(op.ResourceTemplate, req)
	if err != nil {
		return zero, denied("invalid_resource")
	}

	// The release capability is the bare operation id: the runtime's
	// capability pattern (^[a-z0-9_]+(\.[a-z0-9_]+)*$) rejects ':'.
	capability := op.ID
	authz, err := r.client.Authorize(ctx, evaluator.AuthorizeRequest{
		UserID:          r.userID,
		InvokingSubject: r.userID,
		Tool:            rc.tool,
		Resource:        resource,
		ParentSpan:      caller.ParentSpan,
		DelegationDepth: caller.DelegationDepth,
		BindingVersion:  r.cfg.BindingVersion,
		ToolArgs:        rc.toolArgs,
		ToolContext:     rc.toolContext,
	})
	if err != nil {
		r.logger.WarnContext(ctx, "operation authorize failed", "operation", op.ID)
		return zero, denied("proxy_unavailable")
	}
	decision, outcome = "deny", "denied"
	if err := checkDecision(authz); err != nil {
		r.logger.WarnContext(ctx, "operation denied", "operation", op.ID, "reason_code", reasonCodeOf(err))
		return zero, err
	}

	connectorID := r.cfg.InstalledConnectorID
	if connectorID == "" {
		connectorID = r.cfg.ConnectorID
	}
	if r.cfg.DeriveConnector {
		connectorID, capability = "", ""
	}
	rel, err := r.client.ReleaseCredential(ctx, r.userID, runtimesocket.ReleaseRequest{
		Tool:            rc.tool,
		ConnectorID:     connectorID,
		Capability:      capability,
		Resource:        resource,
		ParentSpan:      caller.ParentSpan,
		DelegationDepth: caller.DelegationDepth,
		ToolArgs:        rc.toolArgs,
		ToolContext:     rc.toolContext,
	})
	if err != nil {
		r.logger.WarnContext(ctx, "credential release failed", "operation", op.ID)
		return zero, denied("credential_release_failed")
	}
	if rel.Decision != "allow" && rel.Decision != "permit" {
		r.logger.WarnContext(ctx, "credential release denied", "operation", op.ID,
			"reason_code", safeReason(rel.ReasonCode, "denied"))
		return zero, denied(safeReason(rel.ReasonCode, "denied"))
	}
	if rel.Credential == nil || rel.Credential.AccessToken == "" {
		r.logger.WarnContext(ctx, "credential release returned no credential", "operation", op.ID)
		return zero, denied("credential_release_failed")
	}

	conn, err := connection.NewHTTP(r.cfg.BaseURL, r.cfg.Conn, rel.Credential.AccessToken, r.http)
	if err != nil {
		return zero, fmt.Errorf("operation connection build failed: %w", err)
	}

	decision = "allow"
	resp, err := call(conn)
	if err != nil {
		outcome = "error"
		return zero, fmt.Errorf("operation execution failed: %w", err)
	}
	outcome = "success"
	return resp, nil
}

// opConnects reports whether the config's connection is listed on the
// operation descriptor.
func opConnects(op connection.Op, name string) bool {
	if name == "" {
		return false
	}
	for _, c := range op.Connections {
		if c == name {
			return true
		}
	}
	return false
}

// denied builds a generic OperationDeniedError with the deny decision.
func denied(reasonCode string) *OperationDeniedError {
	return &OperationDeniedError{
		ReasonCode: reasonCode,
		Decision:   middleware.Decision{Action: middleware.ActionDeny, ReasonCode: reasonCode},
	}
}

// checkDecision maps an authorize response onto the runner's outcomes:
// allow/permit passes; enrollment_required and connect_required surface
// their objects; anything else denies.
func checkDecision(response map[string]any) error {
	if response == nil {
		return denied("no_decision")
	}
	rawDecision, _ := response["decision"].(string)
	normalized := strings.ToLower(strings.TrimSpace(rawDecision))
	if normalized == "allow" || normalized == "permit" {
		return nil
	}
	rawReason, _ := response["reason_code"].(string)
	reasonCode := strings.ToLower(strings.TrimSpace(rawReason))
	enrollment, _ := response["enrollment"].(map[string]any)
	connect, _ := response["connect"].(map[string]any)
	if normalized == "enrollment_required" || reasonCode == "enrollment_required" || enrollment != nil {
		return &EnrollmentRequiredError{Enrollment: enrollment, Connect: connect}
	}
	if normalized == "connect_required" || reasonCode == "connect_required" || connect != nil {
		return &ConnectRequiredError{Connect: connect}
	}
	return &OperationDeniedError{
		ReasonCode: safeReason(reasonCode, "denied"),
		Decision: middleware.Decision{
			Action:     middleware.ActionDeny,
			Reason:     stringOf(response["reason"]),
			Hint:       stringOf(response["hint"]),
			ReasonCode: reasonCode,
			Enrollment: enrollment,
			Connect:    connect,
		},
	}
}

// reasonCodeOf extracts a reason code from a runner error for logging.
func reasonCodeOf(err error) string {
	var denied *OperationDeniedError
	if errors.As(err, &denied) {
		return denied.ReasonCode
	}
	var ere *EnrollmentRequiredError
	if errors.As(err, &ere) {
		return "enrollment_required"
	}
	var cre *ConnectRequiredError
	if errors.As(err, &cre) {
		return "connect_required"
	}
	return "denied"
}

// renderResource substitutes {field} placeholders in the operation's
// resource template with fields from req. Values are escaped like path
// segments (a value of only dots becomes one %2E per dot, matching the
// generated path encoder). A missing or empty field is invalid.
func renderResource(template string, req proto.Message) (string, error) {
	var out strings.Builder
	ref := req.ProtoReflect()
	for {
		brace := strings.Index(template, "{")
		if brace < 0 {
			out.WriteString(template)
			break
		}
		end := strings.Index(template[brace:], "}")
		if end < 0 {
			return "", errors.New("unterminated resource template")
		}
		out.WriteString(template[:brace])
		name := template[brace+1 : brace+end]
		value, ok := protoField(ref, name)
		if !ok || value == "" {
			return "", fmt.Errorf("resource field %q missing or empty", name)
		}
		if strings.Trim(value, ".") == "" {
			out.WriteString(strings.Repeat("%2E", len(value)))
		} else {
			out.WriteString(strings.ReplaceAll(strings.ReplaceAll(value, "%", "%25"), "/", "%2F"))
		}
		template = template[brace+end+1:]
	}
	return out.String(), nil
}

// protoField reads the field with the given JSON name as a string.
func protoField(ref protoreflect.Message, jsonName string) (string, bool) {
	fields := ref.Descriptor().Fields()
	for i := 0; i < fields.Len(); i++ {
		fd := fields.Get(i)
		if fd.JSONName() != jsonName && string(fd.Name()) != jsonName {
			continue
		}
		v := ref.Get(fd)
		if !v.IsValid() {
			return "", false
		}
		switch fd.Kind() {
		case protoreflect.StringKind:
			return v.String(), true
		case protoreflect.Int32Kind, protoreflect.Sint32Kind, protoreflect.Sfixed32Kind,
			protoreflect.Int64Kind, protoreflect.Sint64Kind, protoreflect.Sfixed64Kind:
			return fmt.Sprintf("%d", v.Int()), true
		case protoreflect.Uint32Kind, protoreflect.Fixed32Kind,
			protoreflect.Uint64Kind, protoreflect.Fixed64Kind:
			return fmt.Sprintf("%d", v.Uint()), true
		case protoreflect.BoolKind:
			if v.Bool() {
				return "true", true
			}
			return "false", true
		default:
			return "", false
		}
	}
	return "", false
}
