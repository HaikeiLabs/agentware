//go:build windows

package runtimesocket

import (
	"context"
	"net"
)

// dialSocket always fails: the runtime socket is Unix only.
func dialSocket(context.Context, string) (net.Conn, error) {
	return nil, ErrUnsupportedPlatform
}
