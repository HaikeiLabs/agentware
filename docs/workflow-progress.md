# Workflow progress contract

An application can use `ProgressTracker` to track named, application-verified
requirements within a single workflow. The tracker records facts, not model
claims. It does not authorize calls, perform tools, prove that an API response
satisfied a business outcome, or finish a workflow.

The initial Go, Python, and TypeScript implementations share
`fixtures/workflow/progress.v1.json`:

- Requirements are distinct, nonempty names scoped to one run. Independent
  facts can be observed in any order.
- Observing a new verified fact resets the consecutive no-progress counter.
  Repeating an already-known fact or invalidating a fact does not reset it.
- An application can invalidate verification after a relevant state change.
  Readiness means all required observations are currently present.
- The no-progress budget is exhausted after more than the configured maximum
  failures. The caller must enforce an additional total turn, time, and cost
  budget; a sequence of unique observations must not run indefinitely.
- Callers must serialize access, validate observations against their actual
  downstream system, isolate state per workflow, and preserve the independent
  policy and audit boundary. A readiness result never overrides authorization.

Python additionally supports atomic `observe_many` for callbacks that produce
several facts. Framework integrations may choose how to deliver nudges, but a
tracker never manufactures a successful terminal call.

The Rust implementation is proposed separately so its smaller SDK surface and
shared-fixture compatibility can be reviewed independently.
