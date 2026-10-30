package reasoning

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// FuzzAdapter fuzzes the reasoning adapter entry points (Parse, Strip and
// Extract) with the shared fixture raw responses and arbitrary input.
//
// Properties: the adapter never panics, the entry points agree on whether the
// input fails closed, an error produces no partial output, and a successful
// parse yields a well-formed context tree (nodes chained root to leaf with
// RootID pointing at the first node).
func FuzzAdapter(f *testing.F) {
	files, err := filepath.Glob(filepath.Join(reasoningFixtures, "*-cases.json"))
	if err != nil {
		f.Fatal(err)
	}
	if len(files) == 0 {
		f.Fatal("no reasoning fixture files found")
	}
	for _, file := range files {
		data, err := os.ReadFile(file)
		if err != nil {
			f.Fatal(err)
		}
		var fx reasoningFixture
		if err := json.Unmarshal(data, &fx); err != nil {
			f.Fatal(err)
		}
		for _, ex := range fx.Examples {
			b, err := json.Marshal(ex.RawResponse)
			if err != nil {
				f.Fatal(err)
			}
			f.Add(string(b))
		}
	}
	for _, seed := range []string{
		"",
		"hello world",
		"<thinking>why</thinking>answer",
		"<thinking>unbalanced",
		"closed</thinking>first",
		"[THINK]why[/THINK]answer",
		"[THINK]unbalanced",
		`{"content": "hi", "reasoning_content": "why"}`,
		`{"content": "hi", "reasoning_content": 5}`,
		`{"content": {"a": 1}}`,
		`{"content": null, "reasoning": "why"}`,
		`{"content": [{"type": "text", "text": "hi"}, {"type": "thinking", "thinking": "why"}]}`,
		`{"content": [{"type": "text"}]}`,
		`{"message": {"content": "hi", "reasoning_content": "why"}}`,
		`{"choices": [{"message": {"content": "hi"}}]}`,
		`{"message": 42}`,
		`{"unrelated": true}`,
		"{invalid",
	} {
		f.Add(seed)
	}

	f.Fuzz(func(t *testing.T, raw string) {
		a := New()
		clean, tree, err := a.Parse(raw, "fuzz-model", "fuzz-backend")
		stripped, stripErr := a.Strip(raw)
		extracted, extractErr := a.Extract(raw, "fuzz-model", "fuzz-backend")
		// The same bytes through the []byte entry exercise analyzeBytes; it
		// must not panic either.
		_, _ = a.Strip([]byte(raw))

		if (err == nil) != (stripErr == nil) {
			t.Errorf("Parse and Strip disagree on error: %v vs %v", err, stripErr)
		}
		if (err == nil) != (extractErr == nil) {
			t.Errorf("Parse and Extract disagree on error: %v vs %v", err, extractErr)
		}
		if err != nil {
			if clean != "" || tree != nil || stripped != "" || extracted != nil {
				t.Errorf("fail-closed violation: error %v with partial output (clean=%q tree=%+v)", err, clean, tree)
			}
			return
		}
		if stripped != clean {
			t.Errorf("Strip and Parse disagree on clean content: %q vs %q", stripped, clean)
		}
		if tree == nil {
			t.Fatal("Extract returned a nil tree without an error")
		}
		if len(tree.Nodes) == 0 {
			if tree.RootID != "" {
				t.Errorf("empty tree has RootID %q", tree.RootID)
			}
			return
		}
		if tree.RootID != tree.Nodes[0].ID {
			t.Errorf("RootID = %q, want the first node id %q", tree.RootID, tree.Nodes[0].ID)
		}
		for i, node := range tree.Nodes {
			wantParent := ""
			if i > 0 {
				wantParent = tree.Nodes[i-1].ID
			}
			if node.ParentID != wantParent {
				t.Errorf("node %d: ParentID = %q, want %q", i, node.ParentID, wantParent)
			}
		}
	})
}
