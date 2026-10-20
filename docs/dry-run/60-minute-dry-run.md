# Agentware 60-minute cold-start dry run

## Background

The Agentware Developer Path aims to take an engineer from installing the SDK
to seeing one governed allow and one governed deny in their own Go, Python, or
TypeScript harness in under an hour, using public documentation only. This
moderated dry run records where that path works and where participants need
help.

## Hypothesis

An engineer who has not used Agentware before can complete the SDK setup on a
clean machine in less than 60 minutes, without undocumented intervention, and
observe an allow, a deny with its `reason_code` and `hint`, and the audit entry
for each authorization.

## Participant profile

- Software engineer comfortable with their chosen language and its package
  manager.
- Has a Kei organization account and a workspace, with an organization
  owner/admin available to create console resources and approve CLI sign-in.
- Uses a clean macOS or Linux machine with Go, Python, or Node installed.
- Has not followed the Agentware SDK setup before. The moderator observes and
  records friction but does not coach or intervene unless the participant asks.

## Versions and prerequisites

| Component | Version |
| --- | --- |
| agentware | 0.11.0 |
| kei-cli | 0.2.0 |
| kei-proxy | v0.3.0 |

The Agentware README lists these as compatible minimums. The runtime setup
guide currently lists older minimum versions; see **Doc gaps found** in the PR
description.

Before starting the timer, confirm the participant has the Kei org account and
workspace, an owner/admin who can approve sign-in and create console policy
resources, a clean macOS or Linux machine, and Go, Python, or Node installed.
Do not preinstall Agentware or Kei CLI. Record the chosen language and exact
tool versions in the notes.

## Step log

Use elapsed minutes from the start of the session. The target allocations sum
to 60 minutes; the participant may spend time differently. Record any help,
including help from search, another person, or an undocumented source.

| Step | Target | Start | End | Status | Notes |
| --- | ---: | --- | --- | --- | --- |
| 0. Sign-up / prerequisites | 5 min | | | | Confirm org/workspace, admin availability, clean machine, and language toolchain. Start the timer. |
| 1. Install the SDK | 5 min | | | | Use the participant's language-specific install command below. |
| 2. Install Kei CLI and log in | 8 min | | | | Follow “Install and sign in” in the [runtime setup guide](../getting-started/runtime-setup.md). Approve device login in the browser. |
| 3. Connect a runtime | 12 min | | | | Follow “Create a runtime installation,” “Bootstrap the runtime,” and “Start the daemon and check readiness” in the [runtime setup guide](../getting-started/runtime-setup.md). Start `kei-proxy serve`. |
| 4. Register the harness and its tools | 7 min | | | | Follow “Register the SDK harness and sync its tools” in the [runtime setup guide](../getting-started/runtime-setup.md). |
| 5. Create group and policies in the console | 10 min | | | | Create a group, assign the participant/runtime as appropriate, and create one permit rule and one explicit deny rule for the example tools. Record the rule names and the console locations used. |
| 6. Run the minimal harness and inspect decisions/audit | 13 min | | | | Run the matching [Go](../../examples/go/minimal-harness/README.md), [Python](../../examples/python/minimal-harness/README.md), or [TypeScript](../../examples/typescript/minimal-harness/README.md) example against the live socket. Record allow, deny `reason_code`, deny `hint`, and the corresponding audit entry. |

### SDK install commands

Use only the command for the participant's language. These commands are copied
from the repository [README](../../README.md) and language guides.

```sh
go get github.com/haikeilabs/agentware/go
```

```sh
pip install haikeilabs-agentware
```

```sh
npm install @haikeilabs/agentware
```

For step 6, follow the matching language getting-started guide. Its run
commands are copied there and may depend on the example's documented working
directory. Do not use the example's `--fake` mode: that mode demonstrates the
SDK interface but does not exercise the participant's Kei organization or
console policies.

The deny should be a normal authorization result, not a connection or protocol
error. Compare its `reason_code` and `hint` with the [deny hints reference](../deny-hints.md).
Stop before the protected tool handler runs. Capture the audit record that
corresponds to each authorization without recording tokens or customer data.

## Judging criteria

Pass only when all of these are true:

- The participant completes the path in **under 60 minutes**.
- There are **zero undocumented interventions**; all help comes from public
  repository docs and examples.
- The live harness produces one governed allow and one governed deny.
- The deny includes a `reason_code` and `hint` consistent with
  [`docs/deny-hints.md`](../deny-hints.md).
- The participant observes an audit entry for both authorization attempts.
- The denied tool handler does not run.

If a step cannot be completed from published docs, mark it blocked and record
the doc location and what was missing. Do not fill the gap with an improvised
command during the session.

## Scorecard

| Measure | Result |
| --- | --- |
| Language and toolchain versions | |
| Total elapsed time | |
| Completed in under 60 minutes? | Yes / No |
| Governed allow observed? | Yes / No |
| Governed deny observed? | Yes / No |
| Deny `reason_code` and `hint` observed? | Yes / No |
| Audit entry observed for both calls? | Yes / No |
| Denied handler stayed unexecuted? | Yes / No |
| Undocumented interventions | 0 / count and description |
| Overall | Pass / Fail |

## Friction ledger

| # | Step | Participant quote or observed friction | Severity (low / medium / high) | Doc or example location | Follow-up |
| --- | --- | --- | --- | --- | --- |
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |

## Linear action plan

Create or update Linear issues after the session. Keep each action tied to
evidence in the friction ledger.

| Priority | Finding | Proposed action | Owner | Linear issue |
| --- | --- | --- | --- | --- |
| P0 | Participant cannot complete a required step from public docs | Resolve the blocking setup or example gap | | |
| P1 | Participant needs an undocumented explanation or workaround | Clarify the relevant public guide | | |
| P2 | Participant completes the flow but loses time or confidence | Improve navigation, labels, or example output | | |

## Debrief questions

1. At what point were you least sure what to do next?
2. Which command, instruction, or concept took the longest to understand?
3. Did you know whether the allow and deny came from the live policy setup?
4. Could you tell why the call was denied and what action the hint recommended?
5. Where did you find the audit entry, and could you connect it to each call?
6. What did you search for or expect the docs to explain but could not find?
7. What would you change first to make this setup easier for the next engineer?
