//go:build !windows

package runtimesocket

import (
	"context"
	"net"
)

// dialSocket connects to the daemon's Unix socket.
func dialSocket(ctx context.Context, path string) (net.Conn, error) {
	var d net.Dialer
	return d.DialContext(ctx, "unix", path)
}
