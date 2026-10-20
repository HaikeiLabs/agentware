# Go: governed socket harness

For a real Kei runtime setup walkthrough, see the
[runtime setup guide](runtime-setup.md) and [deny hints](../deny-hints.md).

This guide shows the `runtimesocket` client authorizing a governed allow and
deny through the local `kei-proxy` socket. For package installation and
supported versions, see the repository [README](../../README.md). For the
complete working harness layout, see
[`examples/go/minimal-harness`](../../examples/go/minimal-harness).

## Install

```sh
go get github.com/haikeilabs/agentware/go
```

Run `kei-proxy serve` beside the harness and set
`KEI_RUNTIME_SOCKET_PATH` and `KEI_RUNTIME_TOKEN` in the harness process
environment. The daemon owns the user session and policy decision. Do not put
the token in code, arguments, or logs.

To try the snippet without a live installation, start the repository's fake
socket in one terminal from the repository root:

```sh
python3 fixtures/kei/fake-kei-proxy-socket.py ./.runtime.sock --audit ./.runtime-audit.jsonl
```

In the other terminal, copy the Go snippet into your harness module's `main.go`
and run it with `KEI_RUNTIME_SOCKET_PATH` set to that socket and
`KEI_RUNTIME_TOKEN=offline-example-token`. The fake has canned decisions and
is for local examples only; use a real runtime installation for governed work.
The complete [minimal harness](../../examples/go/minimal-harness) prints an
allowed result followed by a deny with its reason code and hint, and confirms
the denied handler did not run:

```text
ALLOW tool=echo policy_id=fake-allow-echo result=map[echo:hello harness]
DENY tool=delete_everything reason_code=no_matching_policy hint=Add a policy for this tool: kei policies create handler=not_run
```

## Authorize a tool

This snippet opens a session for the caller, makes one allowed and one denied
request, and prints only the decision metadata. Add your tool handler after an
explicit allow. A deny response is a normal authorization result; socket or
protocol failures return an error and fail closed.

```go
package main

import (
	"context"
	"fmt"
	"log"

	"github.com/haikeilabs/agentware/go/kei/evaluator"
	"github.com/haikeilabs/agentware/go/kei/runtimesocket"
)

func main() {
	ctx := context.Background()
	runtime := &runtimesocket.Client{}
	session, err := runtime.OpenSession(ctx, runtimesocket.Identity{
		Source: "my-harness", ExternalID: "user-123",
	})
	if err != nil {
		log.Fatal(err)
	}
	defer runtime.CloseSession(ctx, session)

	for _, tool := range []string{"echo", "delete_everything"} {
		answer, err := runtime.Authorize(ctx, session, evaluator.AuthorizeRequest{
			Tool: tool, Action: "execute",
		})
		if err != nil {
			log.Fatal(err)
		}
		fmt.Printf("tool=%s decision=%v reason_code=%v hint=%v\n",
			tool, answer["decision"], answer["reason_code"], answer["hint"])
	}
}
```

The fake socket permits `echo` and denies `delete_everything` with
`reason_code=no_matching_policy` and an actionable `hint`. The complete
`reason_code`/`hint` reference is documented in the `kei-connector-runtime`
README.

For the next layer, use agentware middleware to guard tool execution and its
guardrails to bound your agent loop. See the [middleware guide](../go/README.md).
