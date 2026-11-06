# Build a Hacker News research agent

This agent asks a model to summarize the top five Hacker News stories. It uses
one tool to fetch their titles and URLs, checks calls against a local policy,
and prints an audit record. You need Node 22.18 or later and an
OpenAI-compatible API key.

## 1. Install

From the repository root, build the SDK and install the example:

```sh
cd typescript
npm ci
npm run build
cd ../examples/typescript/first-agent
npm ci
```

## 2. Set the key

```sh
export OPENAI_API_KEY="your-api-key"
```

The default model is `gpt-4o-mini`. To use another model or an
OpenAI-compatible endpoint, set `MODEL` and optionally `OPENAI_BASE_URL`:

```sh
export MODEL="your-model"
export OPENAI_BASE_URL="https://your-endpoint.example/v1"
```

## 3. Run

```sh
npm start
```

The agent asks: “Summarize today's top five Hacker News stories.” It needs
network access to the model endpoint and the Hacker News API.

## 4. Read the output

The run prints an `AUDIT` line for every tool call, with the tool name and the
local policy decision. First the model calls `hn_top_stories`, which the policy
allows, and the summary follows:

```text
AUDIT {"session_id":"","tool_name":"hn_top_stories","args":{},"decision":{"action":"allow","rule":"read-stories","reason":"Reading stories is allowed",...},...,"framework":"first-agent"}
Here are today's top five Hacker News stories: ...
```

Then the run tries `hn_post_comment` through the same client. The local policy
denies it, so the tool never runs, and the deny is audited too:

```text
Now trying hn_post_comment, a tool the local policy denies:
AUDIT {"session_id":"","tool_name":"hn_post_comment","args":{"story_id":1,"text":"Nice post"},"decision":{"action":"deny","rule":"deny-comments","reason":"Only reading stories is allowed",...},...,"framework":"first-agent"}
DENIED hn_post_comment: denied by policy: Only reading stories is allowed
```

That deny is a guardrail with no Kei involved: `withPolicy` in `agent.ts` holds
the rule. The comment tool is not registered with the model, so only your code
can call it, and the policy still stops it.

## 5. Add your own tool

Add a tool to the registry in `agent.ts`, give it a name, description and
input schema, and route its execution through `client.execute`. Add its
implementation and an allow rule to the local evaluator. Try a new prompt
that asks the model to use it.
