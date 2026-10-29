package toolsync

import (
	"context"
	"os"

	"github.com/haikeilabs/agentware/go/toolspec"
)

// SyncSpecs registers typed ToolSpecs, serialized as manifest v3. It sends
// exactly what Sync would send for the same manifest bytes.
func SyncSpecs(ctx context.Context, specs []toolspec.ToolSpec, cfg Config, expectTools bool) (Result, error) {
	manifest, err := toolspec.MarshalManifestV3(specs)
	if err != nil {
		return Result{}, err
	}
	return Sync(ctx, manifest, cfg, expectTools)
}

// SyncManifestFile reads a kei.tool-manifest/v3 file and registers it,
// sending exactly what Sync would send for the same manifest bytes. Errors
// reading the file are ordinary I/O errors.
func SyncManifestFile(ctx context.Context, path string, cfg Config, expectTools bool) (Result, error) {
	manifest, err := os.ReadFile(path)
	if err != nil {
		return Result{}, err
	}
	return Sync(ctx, manifest, cfg, expectTools)
}
