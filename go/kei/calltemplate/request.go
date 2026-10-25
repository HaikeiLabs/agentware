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

package calltemplate

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"strings"
)

// ProviderRequest describes a provider HTTP call built from a validated
// template and tool call arguments. The credential is NOT included — the
// caller injects it after the allow decision using the template's auth
// placement metadata.
type ProviderRequest struct {
	Method  string
	URL     string
	Headers map[string]string
	Body    io.Reader
}

// BuildRequest constructs a ProviderRequest from a validated template and
// the tool call arguments (as provided by the LLM).
//
// The returned request has placeholders filled, query parameters set,
// headers populated (from args marked in: header), and the body
// constructed (from args marked in: body, with base64 encoding applied
// where specified). The credential is never included; the caller injects
// it after the allow using t.Binding.Transport.Auth.
func BuildRequest(t Template, args map[string]any) (*ProviderRequest, error) {
	if t.Binding == nil || t.Binding.Transport == nil {
		return nil, fmt.Errorf("calltemplate: %q has no transport definition", t.Name)
	}

	tr := t.Binding.Transport

	// Build a lookup for transport arg definitions keyed by tool parameter name.
	transportArgs := tr.Args
	if transportArgs == nil {
		transportArgs = map[string]Arg{}
	}

	// 1. Build URL: fill path placeholders, then append query args.
	rawURL, err := fillPathPlaceholders(tr.URLTemplate, transportArgs, args)
	if err != nil {
		return nil, fmt.Errorf("calltemplate: %q: %w", t.Name, err)
	}

	u, err := url.Parse(rawURL)
	if err != nil {
		return nil, fmt.Errorf("calltemplate: %q: invalid URL %q: %w", t.Name, rawURL, err)
	}

	// Collect query params (args with in: query).
	q := u.Query()
	for paramName, a := range transportArgs {
		if a.In != "query" {
			continue
		}
		val, ok := args[paramName]
		if !ok {
			continue // optional param omitted
		}
		strVal, err := argToString(val)
		if err != nil {
			return nil, fmt.Errorf("calltemplate: %q: arg %q: %w", t.Name, paramName, err)
		}
		// Use Arg.Name if set, otherwise the tool parameter name.
		key := a.Name
		if key == "" {
			key = paramName
		}
		q.Set(key, strVal)
	}
	u.RawQuery = q.Encode()

	// 2. Build headers (args with in: header).
	headers := map[string]string{}
	for paramName, a := range transportArgs {
		if a.In != "header" {
			continue
		}
		val, ok := args[paramName]
		if !ok {
			continue
		}
		strVal, err := argToString(val)
		if err != nil {
			return nil, fmt.Errorf("calltemplate: %q: arg %q: %w", t.Name, paramName, err)
		}
		headerName := a.Name
		if headerName == "" {
			headerName = paramName
		}
		headers[headerName] = strVal
	}

	// 3. Build body (args with in: body).
	var body io.Reader
	bodyArgs := collectBodyArgs(transportArgs, args)
	if len(bodyArgs) > 0 {
		encodedBody, err := applyEncodings(bodyArgs, transportArgs)
		if err != nil {
			return nil, fmt.Errorf("calltemplate: %q: encoding body: %w", t.Name, err)
		}
		bodyBytes, err := json.Marshal(encodedBody)
		if err != nil {
			return nil, fmt.Errorf("calltemplate: %q: marshaling body: %w", t.Name, err)
		}
		body = bytes.NewReader(bodyBytes)
		headers["Content-Type"] = "application/json"
	}

	return &ProviderRequest{
		Method:  tr.Method,
		URL:     u.String(),
		Headers: headers,
		Body:    body,
	}, nil
}

// fillPathPlaceholders replaces {name} placeholders in the URL template with
// the corresponding argument values. Args are expected to have in: path.
func fillPathPlaceholders(urlTemplate string, transportArgs map[string]Arg, args map[string]any) (string, error) {
	result := urlTemplate
	// Find all {name} placeholders (including those in URL path segments).
	// We iterate and replace one at a time to handle overlapping/multiple.
	for {
		start := strings.Index(result, "{")
		if start == -1 {
			break
		}
		end := strings.Index(result[start+1:], "}")
		if end == -1 {
			return "", fmt.Errorf("unmatched { in URL template")
		}
		placeholder := result[start : start+end+2] // includes { }
		name := result[start+1 : start+end+1]      // without { }

		a, ok := transportArgs[name]
		if !ok {
			return "", fmt.Errorf("placeholder {%s} has no transport arg definition", name)
		}
		if a.In != "path" {
			return "", fmt.Errorf("placeholder {%s} is defined as in: %q, expected in: path", name, a.In)
		}

		val, ok := args[name]
		if !ok {
			return "", fmt.Errorf("placeholder {%s}: missing required argument %q", name, name)
		}
		strVal, err := argToString(val)
		if err != nil {
			return "", fmt.Errorf("placeholder {%s}: %w", name, err)
		}
		if strVal == "" {
			return "", fmt.Errorf("placeholder {%s}: argument %q is empty", name, name)
		}
		// URL-encode path segments.
		strVal = url.PathEscape(strVal)
		result = strings.Replace(result, placeholder, strVal, 1)
	}
	return result, nil
}

// collectBodyArgs collects args marked in: body into a map for JSON marshaling.
// The key is the API-level field name (Arg.Name or the tool parameter name).
func collectBodyArgs(transportArgs map[string]Arg, args map[string]any) map[string]any {
	body := make(map[string]any)
	for paramName, a := range transportArgs {
		if a.In != "body" {
			continue
		}
		val, ok := args[paramName]
		if !ok {
			continue
		}
		key := a.Name
		if key == "" {
			key = paramName
		}
		body[key] = val
	}
	return body
}

// applyEncodings applies any encodings (e.g. base64) to body values.
func applyEncodings(body map[string]any, transportArgs map[string]Arg) (map[string]any, error) {
	encoded := make(map[string]any, len(body))
	for key, val := range body {
		encoded[key] = val // default: pass through
	}
	for paramName, a := range transportArgs {
		if a.In != "body" {
			continue
		}
		if a.Encoding == "" {
			continue
		}
		key := a.Name
		if key == "" {
			key = paramName
		}
		val, ok := body[key]
		if !ok {
			continue
		}
		strVal, err := argToString(val)
		if err != nil {
			return nil, fmt.Errorf("%q: %w", key, err)
		}
		switch a.Encoding {
		case "base64":
			encoded[key] = base64.StdEncoding.EncodeToString([]byte(strVal))
		default:
			return nil, fmt.Errorf("unsupported encoding %q for %q", a.Encoding, key)
		}
	}
	return encoded, nil
}

// argToString converts a tool call argument value to its string representation.
func argToString(val any) (string, error) {
	switch v := val.(type) {
	case string:
		return v, nil
	case float64:
		return fmt.Sprintf("%v", v), nil
	case bool:
		if v {
			return "true", nil
		}
		return "false", nil
	case nil:
		return "", nil
	default:
		b, err := json.Marshal(v)
		if err != nil {
			return "", fmt.Errorf("cannot convert argument to string: %w", err)
		}
		return string(b), nil
	}
}
