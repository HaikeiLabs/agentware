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

The `AUDIT` line shows the tool name and the local policy decision. The
summary follows it. The policy allows `hn_top_stories` and denies
`hn_post_comment`; the comment tool is not registered for the model.

## 5. Add your own tool

Add a tool to the registry in `agent.ts`, give it a name, description and
input schema, and route its execution through `client.execute`. Add its
implementation and an allow rule to the local evaluator. Try a new prompt
that asks the model to use it.
