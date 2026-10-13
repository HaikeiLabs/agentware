//go:build !windows

package runtimesocket

import (
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

// fakeResponse is one daemon answer: {status, body} (JSON), {status, raw,
// content_type} (verbatim), {status} (no body) or {stall} (never answers).
type fakeResponse struct {
	Status      int             `json:"status"`
	Body        json.RawMessage `json:"body"`
	Raw         *string         `json:"raw"`
	ContentType string          `json:"content_type"`
	Stall       bool            `json:"stall"`
}

// recordedRequest is what the fake daemon saw.
type recordedRequest struct {
	Method        string `json:"method"`
	Path          string `json:"path"`
	Session       string `json:"session"`
	Body          any    `json:"body"`
	Authorization string `json:"-"`
}

// fakeDaemon answers requests on a Unix socket with responses in order.
type fakeDaemon struct {
	path      string
	mu        sync.Mutex
	responses []fakeResponse
	recorded  []recordedRequest
	stall     chan struct{}
}

// shortSocketPath returns a socket path in a fresh short directory: macOS
// caps Unix socket paths near 104 bytes, and t.TempDir() is too long there.
func shortSocketPath(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("/tmp", "kei-")
	if err != nil {
		t.Fatalf("mkdir temp: %v", err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	return filepath.Join(dir, "rt.sock")
}

func startDaemon(t *testing.T, responses ...fakeResponse) *fakeDaemon {
	t.Helper()
	d := &fakeDaemon{responses: responses, stall: make(chan struct{})}
	t.Cleanup(func() { close(d.stall) })
	d.path = startServer(t, d)
	return d
}

// startServer serves handler on a fresh Unix socket and returns its path.
func startServer(t *testing.T, handler http.Handler) string {
	t.Helper()
	path := shortSocketPath(t)
	ln, err := net.Listen("unix", path)
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	srv := &http.Server{Handler: handler}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close() })
	return path
}

func (d *fakeDaemon) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	raw, _ := io.ReadAll(r.Body)
	var body any
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &body); err != nil {
			body = "<invalid json: " + string(raw) + ">"
		}
	}
	d.mu.Lock()
	i := len(d.recorded)
	d.recorded = append(d.recorded, recordedRequest{
		Method:        r.Method,
		Path:          r.URL.Path,
		Session:       r.Header.Get(SessionHeader),
		Body:          body,
		Authorization: r.Header.Get("Authorization"),
	})
	var resp *fakeResponse
	if i < len(d.responses) {
		resp = &d.responses[i]
	}
	d.mu.Unlock()

	switch {
	case resp == nil:
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"error":{"code":"unexpected_request"}}`))
	case resp.Stall:
		select {
		case <-d.stall:
		case <-r.Context().Done():
		}
	case resp.Raw != nil:
		if resp.ContentType != "" {
			w.Header().Set("Content-Type", resp.ContentType)
		}
		w.WriteHeader(resp.Status)
		_, _ = w.Write([]byte(*resp.Raw))
	case len(resp.Body) > 0:
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(resp.Status)
		_, _ = w.Write(resp.Body)
	default:
		w.WriteHeader(resp.Status)
	}
}

func (d *fakeDaemon) requests() []recordedRequest {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]recordedRequest{}, d.recorded...)
}

// jsonResponse is a {status, body} answer.
func jsonResponse(status int, body any) fakeResponse {
	data, err := json.Marshal(body)
	if err != nil {
		panic(errors.New("marshal fake response: " + err.Error()))
	}
	return fakeResponse{Status: status, Body: data}
}

// normalize round-trips v through JSON so maps, slices and numbers compare
// the same however they were built.
func normalize(t *testing.T, v any) any {
	t.Helper()
	data, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var out any
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return out
}
