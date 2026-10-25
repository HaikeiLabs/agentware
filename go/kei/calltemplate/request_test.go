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

package calltemplate_test

import (
	"encoding/base64"
	"encoding/json"
	"io"
	"strings"
	"testing"

	"github.com/haikeilabs/agentware/go/kei/calltemplate"
)

func TestBuild_github_file_write(t *testing.T) {
	data := loadFixture(t, "schemas", "github.tool-definition.v1.json")
	tpls, err := calltemplate.Parse(data)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}

	// Find github.file_write.
	var tpl calltemplate.Template
	var found bool
	for _, tp := range tpls {
		if tp.Name == "github.file_write" {
			tpl = tp
			found = true
			break
		}
	}
	if !found {
		t.Fatal("github.file_write not found in fixture")
	}

	args := map[string]any{
		"owner":          "HaikeiLabs",
		"repo":           "agentware",
		"path":           "src/test.txt",
		"content":        "hello world",
		"branch":         "main",
		"commit_message": "test commit",
	}

	req, err := calltemplate.BuildRequest(tpl, args)
	if err != nil {
		t.Fatalf("BuildRequest: %v", err)
	}

	if req.Method != "PUT" {
		t.Errorf("method = %q, want PUT", req.Method)
	}

	// URL should have path placeholders filled. '/' in the path arg is
	// url.PathEscaped (%2F) because it sits inside a URL path segment.
	expectedURL := "https://api.github.com/repos/HaikeiLabs/agentware/contents/src%2Ftest.txt"
	if req.URL != expectedURL {
		t.Errorf("URL = %q, want %q", req.URL, expectedURL)
	}

	// Content-Type should be application/json.
	if ct := req.Headers["Content-Type"]; ct != "application/json" {
		t.Errorf("Content-Type header = %q, want application/json", ct)
	}

	// No credential should be present.
	for key := range req.Headers {
		if key == "Authorization" || strings.Contains(key, "auth") || strings.Contains(key, "token") {
			t.Errorf("credential header %q present in request", key)
		}
	}

	// Read and verify body.
	if req.Body == nil {
		t.Fatal("body is nil, expected JSON body")
	}
	bodyBytes, err := io.ReadAll(req.Body)
	if err != nil {
		t.Fatalf("reading body: %v", err)
	}
	var bodyMap map[string]any
	if err := json.Unmarshal(bodyBytes, &bodyMap); err != nil {
		t.Fatalf("body is not JSON: %v", err)
	}

	// content should be base64-encoded.
	contentEnc, ok := bodyMap["content"].(string)
	if !ok {
		t.Fatal("body.content missing or not a string")
	}
	decoded, err := io.ReadAll(strings.NewReader(contentEnc))
	if err != nil {
		t.Fatalf("reading content encoding: %v", err)
	}
	// Check it looks like base64 (no direct decode — just verify it's encoded).
	if len(decoded) == 0 {
		t.Error("content is empty")
	}
	// Verify it decodes back to "hello world".
	rawContent, err := base64decode(contentEnc)
	if err != nil {
		t.Fatalf("content is not valid base64: %v", err)
	}
	if string(rawContent) != "hello world" {
		t.Errorf("content decodes to %q, want %q", string(rawContent), "hello world")
	}

	// branch should appear as-is.
	if bodyMap["branch"] != "main" {
		t.Errorf("body.branch = %v, want main", bodyMap["branch"])
	}

	// commit_message should be renamed to message.
	if _, ok := bodyMap["commit_message"]; ok {
		t.Error("body.commit_message should not exist (renamed to message)")
	}
	if bodyMap["message"] != "test commit" {
		t.Errorf("body.message = %v, want test commit", bodyMap["message"])
	}
}

func base64decode(s string) ([]byte, error) {
	return base64.StdEncoding.DecodeString(s)
}

func TestBuild_fills_path_placeholders(t *testing.T) {
	tpl := calltemplate.Template{
		Name:        "test.get_item",
		Description: "Get an item",
		Schema:      "kei.tool-definition/v1",
		Permission:  "test.read",
		Service:     "test",
		Binding: &calltemplate.Binding{
			ConnectorID: "conn_test",
			Transport: &calltemplate.Transport{
				Method:      "GET",
				URLTemplate: "https://api.example.com/{type}/{id}",
				Args: map[string]calltemplate.Arg{
					"type": {In: "path"},
					"id":   {In: "path"},
				},
			},
		},
	}

	req, err := calltemplate.BuildRequest(tpl, map[string]any{
		"type": "widget",
		"id":   "42",
	})
	if err != nil {
		t.Fatalf("BuildRequest: %v", err)
	}

	expected := "https://api.example.com/widget/42"
	if req.URL != expected {
		t.Errorf("URL = %q, want %q", req.URL, expected)
	}
}

func TestBuild_missing_path_arg(t *testing.T) {
	tpl := calltemplate.Template{
		Name:        "test.get_item",
		Description: "Get an item",
		Schema:      "kei.tool-definition/v1",
		Permission:  "test.read",
		Service:     "test",
		Binding: &calltemplate.Binding{
			ConnectorID: "conn_test",
			Transport: &calltemplate.Transport{
				Method:      "GET",
				URLTemplate: "https://api.example.com/{id}",
				Args: map[string]calltemplate.Arg{
					"id": {In: "path"},
				},
			},
		},
	}

	_, err := calltemplate.BuildRequest(tpl, map[string]any{})
	if err == nil {
		t.Fatal("expected error for missing path arg, got nil")
	}
	if !strings.Contains(err.Error(), "missing") && !strings.Contains(err.Error(), "id") {
		t.Errorf("error does not mention missing arg: %v", err)
	}
}

func TestBuild_no_credential_in_result(t *testing.T) {
	data := loadFixture(t, "schemas", "github.tool-definition.v1.json")
	tpls, err := calltemplate.Parse(data)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}

	for _, tpl := range tpls {
		if tpl.Binding == nil || tpl.Binding.Transport == nil {
			continue
		}
		// Build with minimal args (path args only, skip optionals).
		args := makeArgsForTemplate(t, tpl)
		req, err := calltemplate.BuildRequest(tpl, args)
		if err != nil {
			t.Fatalf("BuildRequest(%s): %v", tpl.Name, err)
		}
		// Check no credential headers.
		for key := range req.Headers {
			kl := strings.ToLower(key)
			if strings.Contains(kl, "authorization") ||
				strings.Contains(kl, "bearer") ||
				strings.Contains(kl, "token") ||
				strings.Contains(kl, "api-key") {
				t.Errorf("%s: credential header %q present in request", tpl.Name, key)
			}
		}
		// Check URL doesn't contain credential patterns.
		urlStr := req.URL
		if strings.Contains(strings.ToLower(urlStr), "access_token=") ||
			strings.Contains(strings.ToLower(urlStr), "api_key=") {
			t.Errorf("%s: credential in URL: %s", tpl.Name, urlStr)
		}
	}
}

// makeArgsForTemplate constructs minimal arguments for a template, filling
// path args with dummy values.
func makeArgsForTemplate(t *testing.T, tpl calltemplate.Template) map[string]any {
	t.Helper()
	args := map[string]any{}
	if tpl.Binding == nil || tpl.Binding.Transport == nil {
		return args
	}
	tr := tpl.Binding.Transport
	for paramName, a := range tr.Args {
		switch a.In {
		case "path":
			args[paramName] = "test-" + paramName
		case "query":
			args[paramName] = "q-" + paramName
		case "body":
			if a.Encoding == "base64" {
				args[paramName] = "test content for " + paramName
			} else {
				args[paramName] = "body-" + paramName
			}
		case "header":
			args[paramName] = "hdr-" + paramName
		}
	}
	return args
}

func TestBuild_query_params(t *testing.T) {
	tpl := calltemplate.Template{
		Name:        "test.search",
		Description: "Search items",
		Schema:      "kei.tool-definition/v1",
		Permission:  "test.read",
		Service:     "test",
		Binding: &calltemplate.Binding{
			ConnectorID: "conn_test",
			Transport: &calltemplate.Transport{
				Method:      "GET",
				URLTemplate: "https://api.example.com/search",
				Args: map[string]calltemplate.Arg{
					"q":     {In: "query"},
					"limit": {In: "query"},
				},
			},
		},
	}

	req, err := calltemplate.BuildRequest(tpl, map[string]any{
		"q":     "hello",
		"limit": 10,
	})
	if err != nil {
		t.Fatalf("BuildRequest: %v", err)
	}

	if !strings.Contains(req.URL, "q=hello") {
		t.Errorf("URL missing q=hello: %s", req.URL)
	}
	if !strings.Contains(req.URL, "limit=10") {
		t.Errorf("URL missing limit=10: %s", req.URL)
	}
	// No body for GET.
	if req.Body != nil {
		t.Error("expected nil body for GET request")
	}
}

func TestBuild_body_base64_encoding(t *testing.T) {
	tpl := calltemplate.Template{
		Name:        "test.write",
		Description: "Write data",
		Schema:      "kei.tool-definition/v1",
		Permission:  "test.write",
		Service:     "test",
		Binding: &calltemplate.Binding{
			ConnectorID: "conn_test",
			Transport: &calltemplate.Transport{
				Method:      "POST",
				URLTemplate: "https://api.example.com/data",
				Args: map[string]calltemplate.Arg{
					"content": {In: "body", Encoding: "base64"},
					"label":   {In: "body"},
				},
			},
		},
	}

	req, err := calltemplate.BuildRequest(tpl, map[string]any{
		"content": "raw data to encode",
		"label":   "my-label",
	})
	if err != nil {
		t.Fatalf("BuildRequest: %v", err)
	}

	if req.Body == nil {
		t.Fatal("body is nil")
	}
	bodyBytes, _ := io.ReadAll(req.Body)
	var bodyMap map[string]any
	if err := json.Unmarshal(bodyBytes, &bodyMap); err != nil {
		t.Fatalf("body not JSON: %v", err)
	}
	contentEnc, ok := bodyMap["content"].(string)
	if !ok {
		t.Fatal("body.content missing or not string")
	}
	// Verify base64.
	decoded, err := base64decode(contentEnc)
	if err != nil {
		t.Fatalf("content is not valid base64: %v", err)
	}
	if string(decoded) != "raw data to encode" {
		t.Errorf("content decodes to %q, want %q", string(decoded), "raw data to encode")
	}
	if bodyMap["label"] != "my-label" {
		t.Errorf("label = %v, want my-label", bodyMap["label"])
	}
}

func TestBuild_no_body(t *testing.T) {
	tpl := calltemplate.Template{
		Name:        "test.delete",
		Description: "Delete item",
		Schema:      "kei.tool-definition/v1",
		Permission:  "test.write",
		Service:     "test",
		Binding: &calltemplate.Binding{
			ConnectorID: "conn_test",
			Transport: &calltemplate.Transport{
				Method:      "DELETE",
				URLTemplate: "https://api.example.com/{id}",
				Args: map[string]calltemplate.Arg{
					"id": {In: "path"},
				},
			},
		},
	}

	req, err := calltemplate.BuildRequest(tpl, map[string]any{"id": "99"})
	if err != nil {
		t.Fatalf("BuildRequest: %v", err)
	}

	if req.Method != "DELETE" {
		t.Errorf("method = %q, want DELETE", req.Method)
	}
	expected := "https://api.example.com/99"
	if req.URL != expected {
		t.Errorf("URL = %q, want %q", req.URL, expected)
	}
	if req.Body != nil {
		t.Error("expected nil body for DELETE request")
	}
	// No Content-Type for body-less request.
	if _, ok := req.Headers["Content-Type"]; ok {
		t.Error("unexpected Content-Type header for body-less request")
	}
}

func TestBuild_missing_transport(t *testing.T) {
	t.Run("nil binding", func(t *testing.T) {
		tpl := calltemplate.Template{
			Name:   "test.no_binding",
			Schema: "kei.tool-definition/v1",
		}
		_, err := calltemplate.BuildRequest(tpl, map[string]any{})
		if err == nil {
			t.Fatal("expected error for nil binding")
		}
		if !strings.Contains(err.Error(), "no transport") {
			t.Errorf("error does not mention transport: %v", err)
		}
	})

	t.Run("nil transport", func(t *testing.T) {
		tpl := calltemplate.Template{
			Name:    "test.no_transport",
			Schema:  "kei.tool-definition/v1",
			Binding: &calltemplate.Binding{ConnectorID: "conn_test"},
		}
		_, err := calltemplate.BuildRequest(tpl, map[string]any{})
		if err == nil {
			t.Fatal("expected error for nil transport")
		}
		if !strings.Contains(err.Error(), "no transport") {
			t.Errorf("error does not mention transport: %v", err)
		}
	})
}

func TestBuild_optional_body_arg(t *testing.T) {
	tpl := calltemplate.Template{
		Name:        "test.update",
		Description: "Update item with optional fields",
		Schema:      "kei.tool-definition/v1",
		Permission:  "test.write",
		Service:     "test",
		Binding: &calltemplate.Binding{
			ConnectorID: "conn_test",
			Transport: &calltemplate.Transport{
				Method:      "PATCH",
				URLTemplate: "https://api.example.com/{id}",
				Args: map[string]calltemplate.Arg{
					"id":      {In: "path"},
					"title":   {In: "body"},
					"summary": {In: "body"},
				},
			},
		},
	}

	// Omit the optional summary arg.
	req, err := calltemplate.BuildRequest(tpl, map[string]any{
		"id":    "42",
		"title": "New Title",
		// summary omitted
	})
	if err != nil {
		t.Fatalf("BuildRequest: %v", err)
	}

	if req.Body == nil {
		t.Fatal("body is nil")
	}
	bodyBytes, _ := io.ReadAll(req.Body)
	var bodyMap map[string]any
	if err := json.Unmarshal(bodyBytes, &bodyMap); err != nil {
		t.Fatalf("body not JSON: %v", err)
	}
	if bodyMap["title"] != "New Title" {
		t.Errorf("title = %v, want New Title", bodyMap["title"])
	}
	if _, exists := bodyMap["summary"]; exists {
		t.Error("summary should not be present when omitted")
	}
}
