# Using agentware as a team

Start with the language guide for your harness — [Go](getting-started/go.md),
[Python](getting-started/python.md), or
[TypeScript](getting-started/typescript.md) — then choose the installation
boundary that matches where the harness runs. Agentware applies policy for
each caller session; an agent profile and a user session serve different
purposes.

## Developer machines

Use one runtime installation per developer environment. Each developer links
their own chat identity and uses their own runtime credential, supplied to the
harness through the environment or a secret manager. Keep separate socket
paths and credentials for separate local environments. Do not share one
developer's credential across the team.

## Staging and production

Use a distinct installation for each deployed environment, such as staging
and production. Inject that environment's runtime credential at process start
from its secret manager. Keep the credential out of manifests, source control,
command-line arguments, and logs. The `kei-proxy` runtime socket is the local
authorization boundary used by the harness.

## CI

Use a dedicated agent profile for CI instead of a developer identity. Bind the
CI runtime installation and its credential to the CI environment, and keep
policy scoped to the operations that the pipeline needs. The profile describes
the agent; each human-triggered run should still retain the initiating user's
identity where the harness provides one. Do not use CI credentials for local
development.

## Sessions and delegation

Open or reuse a runtime session for the authenticated caller identity. Keep
sessions isolated per user; do not use a shared service identity to represent
all users. When your agent delegates work, carry the original invoking subject
through the delegation context so audit records continue to identify the
initiating human.

## Policy and execution

Treat an explicit allow as permission to call the tool, not proof that the
provider operation succeeded. Execute the handler only after the allow, and
record the final execution result through your application's normal audit
path. A deny includes `reason_code` and may include `hint`; the complete
reference is in the `kei-connector-runtime` README.
