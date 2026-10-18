package toolsync

import (
	"context"
	"errors"

	"github.com/haikeilabs/agentware/go/toolspec"
)

// Registrar sends a set of shared ToolSpecs to the runtime registration API.
type Registrar interface {
	Send(ctx context.Context, specs []toolspec.ToolSpec) error
}

// ToolManifestSender transports serialized manifests over a local runtime socket.
type ToolManifestSender interface {
	SendTools(ctx context.Context, manifest []byte) error
}

// SocketRegistrar serializes ToolSpecs as manifest v3, then sends the metadata
// to POST /v1/tools:register through Client. It does not send provider payloads.
type SocketRegistrar struct{ Client ToolManifestSender }

var errNilToolManifestSender = errors.New("tool manifest sender is required")

// Send validates and serializes specs before sending one manifest to the socket.
func (r SocketRegistrar) Send(ctx context.Context, specs []toolspec.ToolSpec) error {
	if r.Client == nil {
		return errNilToolManifestSender
	}
	if len(specs) == 0 {
		return newError(ErrEmptyManifest, "refusing to send an empty manifest to /v1/tools:register")
	}
	manifest, err := toolspec.MarshalManifestV3(specs)
	if err != nil {
		return err
	}
	return r.Client.SendTools(ctx, manifest)
}
