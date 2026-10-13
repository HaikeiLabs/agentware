//go:build windows

package runtimesocket

import (
	"context"
	"errors"
	"testing"

	"github.com/soypete/pedro-agentware/go/kei/evaluator"
)

func TestWindowsRefusesWithUnsupportedPlatform(t *testing.T) {
	c := &Client{Env: map[string]string{evaluator.RuntimeTokenEnv: testToken}}
	_, err := c.OpenSession(context.Background(), Identity{Source: "s", ExternalID: "e"})
	var se *Error
	if !errors.As(err, &se) || se.Class != evaluator.ReasonProxyUnavailable || !errors.Is(err, ErrUnsupportedPlatform) {
		t.Errorf("got %v", err)
	}
}
