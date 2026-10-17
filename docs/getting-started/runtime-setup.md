# Set up a local Kei runtime for an Agentware SDK harness

This guide connects a custom Go, Python, or TypeScript harness to a local
`kei-proxy` daemon. The daemon is the runtime and policy enforcement point; Kei
stores installation and harness metadata. Provider payloads, customer content,
and credentials stay out of the control plane.

## Prerequisites

- A macOS or Linux host where your harness and `kei-proxy` can run as the same
  operating-system user.
- A Kei organization and workspace. An organization owner or admin must create
  the organization/workspace in the Kei console and configure the policies
  needed by the harness. The CLI does not create organizations or policies for
  you.
- An organization owner or admin who can approve `kei login` and provision a
  runtime installation credential.
- `kei` 0.1.32 or later (the installed version at the time of writing) and
  `kei-proxy` 0.2.11 or later.

## Install and sign in

Install the Kei CLI and its bundled `kei-proxy` runtime:

```sh
curl -fsSL https://haikeilabs.com/install.sh | sh
kei --version
kei-proxy --help
```

If you install to a user-writable directory, add that directory to `PATH` as
described by the installer documentation. Sign in as an organization owner or
admin; approve the device authorization in your browser when prompted:

```sh
kei login
```

The CLI stores its login credential in the operating system credential store.
Do not copy it into a file, command argument, or harness environment.

## Create a runtime installation

Create pending installation metadata for this local SDK runtime. Use the
workspace name or ID from the console:

```sh
kei bot init --platform cli --name "My Agentware SDK harness" --workspace "WORKSPACE_NAME"
```

Record the printed installation ID. Request its runtime credential and deliver
it directly into your approved secret manager; do not print or paste the
credential into a terminal transcript, repository, or harness source. The CLI
supports piping credential output directly to a secret-manager import command;
use your organization's approved import command, for example:

```sh
kei bot credential --installation INSTALLATION_ID --workspace WORKSPACE_ID | secret-manager import
```

Then configure the local runtime:

```sh
kei setup
```

Follow the prompts to configure the Kei control-plane URL and runtime
installation credential. `kei setup` stores local configuration in
`~/.config/kei.yaml` with `0600` permissions. Keep the credential out of
harness code and logs.

## Bootstrap the runtime

On a workstation, bootstrap from the `kei` configuration you just created. This
verifies the runtime credential, sends the first heartbeat, and syncs the
policy bundle:

```sh
kei runtime bootstrap
```

For a deployed runtime configured with environment variables instead of
workstation config, run the runtime binary directly:

```sh
kei-proxy runtime bootstrap
```

## Start the daemon and check readiness

Choose a short, user-writable socket path and configure it in the environment
of `kei-proxy` and the harness. macOS limits Unix socket paths to 104 bytes;
keep the full path comfortably below that limit. The SDK reads
`KEI_RUNTIME_SOCKET_PATH` from its process environment and otherwise uses
`/run/kei-proxy/runtime.sock`.

For example, set the socket path in the shell that starts the daemon and
harness:

```sh
export KEI_RUNTIME_SOCKET_PATH="$HOME/.kei/runtime.sock"
```

If the harness provides a tool manifest (a JSON file describing its tools),
also set `KEI_TOOL_MANIFEST` to its path. The daemon reads this during
bootstrap and makes the tools available for sync:

```sh
export KEI_TOOL_MANIFEST="$PWD/tools.json"
```

**Known limitation:** `kei-proxy serve` does not yet read
`~/.config/kei.yaml`. Although `kei setup` saves workstation configuration
there for `kei runtime bootstrap`, `KEI_RUNTIME_TOKEN` and
`KEI_RUNTIME_CONTROL_PLANE_URL` must also be injected into the daemon's
environment by your secret manager. This limitation is not fixed yet. Never
put the token in the command line, shell history, this guide, or harness source.

Start the daemon with the secret manager supplying those environment
variables:

```sh
kei-proxy serve
```

`kei-proxy serve` keeps the runtime heartbeat running itself. Check readiness
through the Unix socket from another terminal:

```sh
curl --unix-socket "$KEI_RUNTIME_SOCKET_PATH" http://localhost/readyz
```

`/readyz` returns success when the runtime has the required control-plane URL
and runtime credential configured. Then confirm the control plane has received
the first heartbeat:

If readiness fails, check that the daemon is running, both processes use the
same socket path and OS user, and the socket directory is writable by the
runtime user.

```sh
kei bot status --installation INSTALLATION_ID
```

Verify that `last_heartbeat_at` is present, then bind the installation:

```sh
kei bot bind --installation INSTALLATION_ID
```

If installation or binding reports that organization, workspace, agent, or
policy setup is missing, ask an organization owner/admin to resolve it in the
Kei console.

## Register the SDK harness and sync its tools

The user-facing harness name is **SDK (Agentware)**; its API kind is `custom`.
Register it against the runtime installation:

```sh
kei harness add --kind custom --installation INSTALLATION_ID
```

Create `tools.json` in the working directory using the harness tool manifest
format. It must contain a non-empty top-level `tools` array; each tool includes
`name` and `route`, with optional `source`, `required_capabilities`, and
`resource_types`. The daemon reads the manifest from the `KEI_TOOL_MANIFEST`
environment variable (set before starting `kei-proxy serve` as shown above).
Then sync the tool metadata from the runtime to the control plane:

```sh
kei harness sync --harness custom
```

The manifest is tool metadata, not a credential store or a permission grant.
The proxy and Kei policy determine whether a call is allowed. Keep credentials
and customer payloads out of the manifest.

The Go, Python, and TypeScript Agentware socket clients automatically discover
the daemon using `KEI_RUNTIME_SOCKET_PATH`; each also accepts an explicit socket
path in its client configuration. The SDK sends authorization requests through
that local socket. Some session-based SDK flows also need `KEI_RUNTIME_TOKEN`
in the harness process environment; see the language-specific guide. Supply it
through your secret manager, never in source code, arguments, or logs.

## Understand a denial

A policy denial is a normal authorization result. The response includes a deny
decision and may include a `reason_code`, reason text, policy ID, or a hint. The
SDK returns the denial to the caller; it does not authorize tool execution.
Proceed to the tool handler only after an explicit allow. Connection and
protocol errors are errors and fail closed.

For reason codes and next-step guidance, see the [deny hints reference](../deny-hints.md).
An organization owner/admin must create or adjust policies in the Kei console
when a denial requires a policy change.
