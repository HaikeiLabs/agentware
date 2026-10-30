package toolspec_test

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/haikeilabs/agentware/go/toolspec"
)

// FuzzParseManifestV3 fuzzes the manifest v3 loader with the shared fixture
// documents and arbitrary input.
//
// Properties: parsing never panics, a rejected document is always a
// *toolspec.ManifestError, and specs that parse successfully re-marshal via
// MarshalManifestV3 and re-parse to the same specs (modulo parameters, which
// the v3 marshaller intentionally does not write).
func FuzzParseManifestV3(f *testing.F) {
	for _, dir := range []string{"valid", "invalid"} {
		files, err := filepath.Glob(filepath.Join(manifestV3Fixtures, dir, "*.json"))
		if err != nil {
			f.Fatal(err)
		}
		if len(files) == 0 {
			f.Fatalf("no %s fixture files found", dir)
		}
		for _, file := range files {
			data, err := os.ReadFile(file)
			if err != nil {
				f.Fatal(err)
			}
			f.Add(data)
		}
	}

	f.Fuzz(func(t *testing.T, data []byte) {
		specs, err := toolspec.ParseManifestV3(data)
		if err != nil {
			var me *toolspec.ManifestError
			if !errors.As(err, &me) {
				t.Fatalf("ParseManifestV3 error %v is not a *toolspec.ManifestError", err)
			}
			return
		}
		out, err := toolspec.MarshalManifestV3(specs)
		if err != nil {
			t.Fatalf("MarshalManifestV3 rejected specs that ParseManifestV3 accepted: %v", err)
		}
		reparsed, err := toolspec.ParseManifestV3(out)
		if err != nil {
			t.Fatalf("re-parse of marshaled manifest failed: %v", err)
		}
		if !reflect.DeepEqual(stripParameters(specs), stripParameters(reparsed)) {
			t.Errorf("round trip changed the specs\n got:  %#v\n want: %#v", reparsed, specs)
		}
	})
}

// stripParameters clears the Parameters field, which the manifest v3
// marshaller intentionally does not write, so a spec and its round-tripped
// copy compare equal.
func stripParameters(specs []toolspec.ToolSpec) []toolspec.ToolSpec {
	out := make([]toolspec.ToolSpec, len(specs))
	for i, spec := range specs {
		spec.Parameters = nil
		out[i] = spec
	}
	return out
}
