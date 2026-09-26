// Package runtimelink defines the harness → Agentware → kei-connector-runtime
// heartbeat contract: configuration, status, identity, failure classes, the
// child's JSONL wire events, and redacted lifecycle events.
//
// This package is the Go reference for docs/specs/runtime-heartbeat-liveness.md
// (HAI-141). It defines the contract types and the RuntimeLink implementation.
// The only heartbeat path is harness → RuntimeLink → kei-connector-runtime;
// nothing here talks to the catalog. Python
// (pedro_agentware.kei.runtime_link) and TypeScript (src/kei/runtimeLink.ts)
// mirror it and share the fixtures in testing/contracts/runtime-link.
package runtimelink

import (
	"context"
	"slices"
	"time"
)

// ContractVersion is the child stdout event version ("v") this SDK accepts.
const ContractVersion = 1

// MaxChildLineBytes bounds one child stdout line, excluding the newline.
const MaxChildLineBytes = 4096

// EventBufferSize is the capacity of the RuntimeLink event ring.
const EventBufferSize = 64

// SDKLang identifies this implementation in lifecycle events and child env.
const SDKLang = "go"

// HarnessKind is the closed set of harnesses that may start a RuntimeLink.
type HarnessKind string

const (
	HarnessAssistant   HarnessKind = "assistant"
	HarnessPDE         HarnessKind = "pde"
	HarnessChatDiscord HarnessKind = "chat-discord"
	HarnessChatSlack   HarnessKind = "chat-slack"
	HarnessChatTeams   HarnessKind = "chat-teams"
	HarnessCLI         HarnessKind = "cli"
)

// HarnessKinds lists every valid HarnessKind in contract order.
var HarnessKinds = []HarnessKind{
	HarnessAssistant, HarnessPDE, HarnessChatDiscord, HarnessChatSlack, HarnessChatTeams, HarnessCLI,
}

// Valid reports whether k is in the closed set.
func (k HarnessKind) Valid() bool { return slices.Contains(HarnessKinds, k) }

// LinkState is the RuntimeLink supervisor state.
type LinkState string

const (
	StateDisabled     LinkState = "disabled"
	StateStarting     LinkState = "starting"
	StateConnected    LinkState = "connected"
	StateDegraded     LinkState = "degraded"
	StateReconnecting LinkState = "reconnecting"
	StateTerminal     LinkState = "terminal"
	StateStopped      LinkState = "stopped"
)

// LinkStates lists every valid LinkState in contract order.
var LinkStates = []LinkState{
	StateDisabled, StateStarting, StateConnected, StateDegraded, StateReconnecting, StateTerminal, StateStopped,
}

// Valid reports whether s is in the closed set.
func (s LinkState) Valid() bool { return slices.Contains(LinkStates, s) }

// FailureClass is the closed failure taxonomy shared by status, events,
// metrics labels, and alerts (spec §6.3). It separates harness↔runtime
// failures from runtime↔catalog failures so alerts route to the right owner.
type FailureClass string

const (
	FailureConfigInvalid            FailureClass = "config_invalid"
	FailureRuntimeUnavailable       FailureClass = "runtime_unavailable"
	FailureRuntimeCrashloop         FailureClass = "runtime_crashloop"
	FailureRuntimeUnresponsive      FailureClass = "runtime_unresponsive"
	FailureContractMismatch         FailureClass = "contract_mismatch"
	FailureCatalogUnreachable       FailureClass = "catalog_unreachable"
	FailureCatalogTimeout           FailureClass = "catalog_timeout"
	FailureCatalogError             FailureClass = "catalog_error"
	FailureCatalogBackpressure      FailureClass = "catalog_backpressure"
	FailureInstallationUnauthorized FailureClass = "installation_unauthorized"
	FailureInstallationStale        FailureClass = "installation_stale"
	FailureInstallationOffline      FailureClass = "installation_offline"
	FailureAuditBacklog             FailureClass = "audit_backlog"
	FailureLegacyRuntimeUnverified  FailureClass = "legacy_runtime_unverified"
)

// FailureClasses lists every valid FailureClass in contract order.
var FailureClasses = []FailureClass{
	FailureConfigInvalid, FailureRuntimeUnavailable, FailureRuntimeCrashloop, FailureRuntimeUnresponsive,
	FailureContractMismatch, FailureCatalogUnreachable, FailureCatalogTimeout, FailureCatalogError,
	FailureCatalogBackpressure, FailureInstallationUnauthorized, FailureInstallationStale,
	FailureInstallationOffline, FailureAuditBacklog, FailureLegacyRuntimeUnverified,
}

// Valid reports whether c is in the closed set. The empty class is valid
// only where a field documents it as "no failure".
func (c FailureClass) Valid() bool { return slices.Contains(FailureClasses, c) }

// BeatOutcome is the runtime-reported result of one catalog heartbeat POST.
type BeatOutcome string

const (
	OutcomeOK                  BeatOutcome = "ok"
	OutcomeCatalogUnreachable  BeatOutcome = "catalog_unreachable"
	OutcomeCatalogTimeout      BeatOutcome = "catalog_timeout"
	OutcomeCatalogError        BeatOutcome = "catalog_error"
	OutcomeCatalogBackpressure BeatOutcome = "catalog_backpressure"
	OutcomeCatalogRejected     BeatOutcome = "catalog_rejected"
	OutcomeUnauthorized        BeatOutcome = "unauthorized"
)

// BeatOutcomes lists every valid BeatOutcome in contract order.
var BeatOutcomes = []BeatOutcome{
	OutcomeOK, OutcomeCatalogUnreachable, OutcomeCatalogTimeout, OutcomeCatalogError,
	OutcomeCatalogBackpressure, OutcomeCatalogRejected, OutcomeUnauthorized,
}

// Valid reports whether o is in the closed set.
func (o BeatOutcome) Valid() bool { return slices.Contains(BeatOutcomes, o) }

// ChildEnvAllowlist is the exact environment a runtime child may receive.
// The SDK never passes os.Environ() through.
var ChildEnvAllowlist = []string{
	"KEI_RUNTIME_TOKEN",
	"KEI_RUNTIME_CONTROL_PLANE_URL",
	"KEI_HARNESS_KIND",
	"KEI_HARNESS_VERSION",
	"KEI_DEPLOYMENT_ENV",
	"KEI_AGENTWARE_SDK_LANG",
	"KEI_AGENTWARE_SDK_VERSION",
	"KEI_HEARTBEAT_RUN_ID",
	"PATH",
	"HOME",
	"TZ",
}

// LinkStatus is a point-in-time snapshot of a RuntimeLink. It is diagnostic
// only: harness readiness must never depend on it (spec §6.3).
type LinkStatus struct {
	State            LinkState
	Reason           FailureClass // empty when connected
	LastBeatAt       time.Time    // last beat event from the child, any outcome
	LastCatalogOKAt  time.Time    // last beat the runtime reported as ok
	ConsecutiveFails int
	RunID            string
	Restarts         int
}

// RuntimeIdentity is the authoritative installation identity the runtime
// reports from catalog whoami. Declared harness config never overrides it.
type RuntimeIdentity struct {
	RunID          string `json:"run_id"`
	InstallationID string `json:"installation_id"`
	OrgID          string `json:"org_id"`
	WorkspaceID    string `json:"workspace_id"`
	Platform       string `json:"platform"`
	Status         string `json:"status"`
	BindingStatus  string `json:"binding_status"`
	RuntimeVersion string `json:"runtime_version"`
	// DefaultAgentID is the installation's default agent from whoami. Empty
	// when the runtime reports none (older runtimes omit it). A harness reads
	// its agent from here; it must not require an agent-ID env var.
	DefaultAgentID string `json:"agent_id"`
	// Agents are the agents assigned to the installation. Never nil; empty
	// when the runtime reports none.
	Agents []AssignedAgent `json:"agents"`
}

// AssignedAgent is one agent assigned to a runtime installation.
type AssignedAgent struct {
	AgentID   string `json:"agent_id"`
	IsDefault bool   `json:"is_default"`
}

// MetricsSink receives heartbeat metrics from the RuntimeLink supervisor.
// Implementations must be non-blocking and must not delay the watchdog.
type MetricsSink interface {
	// EmitBeat is called for every parsed child beat event.
	EmitBeat(ctx context.Context, beat BeatEvent)
	// EmitRestart is called when the child is restarted, before backoff.
	EmitRestart(ctx context.Context, attempt int, cause FailureClass)
	// EmitLifecycle is called on every state transition.
	EmitLifecycle(ctx context.Context, state LinkState, reason FailureClass)
}

// LifecycleAuditSink receives redacted lifecycle events. Implementations
// must be non-blocking; the supervisor drops events that cannot be delivered.
type LifecycleAuditSink interface {
	// RecordLifecycle is called on every state transition with a validated
	// and redacted LinkEvent. The event has been serialization-checked and
	// is safe to persist.
	RecordLifecycle(ctx context.Context, event LinkEvent)
}

// RuntimeLink supervises the harness→runtime heartbeat. Implementations
// arrive in a later slice; this interface fixes the contract.
type RuntimeLink interface {
	// Start is non-blocking. It returns an error only for invalid config,
	// never for network or runtime state. Cancelling ctx stops the link.
	Start(ctx context.Context) error
	// Stop is idempotent and bounded by Config.StopTimeout.
	Stop(ctx context.Context) error
	// Status returns a lock-free snapshot, safe to call from /diag handlers.
	Status() LinkStatus
	// Identity returns false until the runtime reports identity.
	Identity() (RuntimeIdentity, bool)
	// Events is bounded (EventBufferSize); the oldest event is dropped.
	Events() <-chan LinkEvent
}
