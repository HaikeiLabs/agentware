package evaluator

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
	"unicode/utf16"
)

// ResourcesTouched derives "type:kind:id" resource identifiers from a tool
// call's arguments, e.g. "github:repo:owner/sales-pipeline". Only resources
// named in the arguments are knowable before the call.
func ResourcesTouched(toolName string, args map[string]any) []string {
	head, _, _ := strings.Cut(toolName, ".")
	head, _, _ = strings.Cut(head, "_")
	resourceType := strings.ToLower(head)
	if resourceType == "" {
		return nil
	}
	var touched []string
	add := func(kind string, identifier any) {
		if identifier == nil {
			return
		}
		value := strings.TrimSpace(stringify(identifier))
		if value == "" {
			return
		}
		entry := resourceType + ":" + kind + ":" + value
		for _, t := range touched {
			if t == entry {
				return
			}
		}
		touched = append(touched, entry)
	}
	owner := first(args, "owner")
	repo := first(args, "repo", "repository")
	switch {
	case owner != nil && repo != nil:
		add("repo", stringify(owner)+"/"+stringify(repo))
	case repo != nil:
		add("repo", repo)
	}
	add("issue", first(args, "issue_number"))
	add("pull_request", first(args, "pull_number", "pr_number"))
	add("branch", first(args, "branch", "ref"))
	add("file", first(args, "path", "file_path"))
	return touched
}

// first returns the first truthy value among keys, like Python's `a or b`.
func first(args map[string]any, keys ...string) any {
	for _, k := range keys {
		if v, ok := args[k]; ok && truthy(v) {
			return v
		}
	}
	return nil
}

func truthy(v any) bool {
	switch t := v.(type) {
	case nil:
		return false
	case string:
		return t != ""
	case bool:
		return t
	case float64:
		return t != 0
	case int:
		return t != 0
	}
	return true
}

// ToolArgsDigest is the SHA-256 of args as compact, sorted-key, ASCII-escaped
// JSON: byte for byte Python's json.dumps(args, sort_keys=True,
// separators=(",", ":")) for strings, integers, booleans, null, arrays and
// objects.
func ToolArgsDigest(args map[string]any) string {
	if args == nil {
		args = map[string]any{}
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(args); err != nil {
		buf.Reset()
		fmt.Fprint(&buf, args)
	}
	sum := sha256.Sum256([]byte(asciiEscape(strings.TrimSuffix(buf.String(), "\n"))))
	return hex.EncodeToString(sum[:])
}

// asciiEscape rewrites every non-ASCII rune as \uXXXX (UTF-16, lowercase hex),
// as Python's ensure_ascii does. Non-ASCII only occurs inside JSON strings.
func asciiEscape(s string) string {
	var b strings.Builder
	for _, r := range s {
		if r < 0x80 {
			b.WriteRune(r)
			continue
		}
		if r > 0xffff {
			hi, lo := utf16.EncodeRune(r)
			fmt.Fprintf(&b, `\u%04x\u%04x`, hi, lo)
			continue
		}
		fmt.Fprintf(&b, `\u%04x`, r)
	}
	return b.String()
}
