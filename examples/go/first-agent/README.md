# First agent in Go

Build an agent that reads today's top Hacker News stories and summarizes them.
It runs on any OpenAI-compatible endpoint. You don't need a Kei account.

The agent asks one question: "Summarize today's top five Hacker News
stories." Here is what happens:

- `InferenceExecutor` sends the question to the model, runs the
  `hn_top_stories` tool when the model asks for it, and returns the answer.
- Every tool call goes through `AuditedToolClient`, which writes an audit
  record.
- A local rule denies `hn_post_comment`. The tool never runs, and the denial
  is audited too.

## Layout

| File | What it does |
| --- | --- |
| `main.go` | Wiring only: reads the settings, builds the backend, runs the agent, prints the output |
| `agent/agent.go` | The loop, the audited tool client and the audit line format |
| `agent/hn.go` | The `hn_top_stories` and `hn_post_comment` tools |
| `agent/policy.go` | The local rule that denies `hn_post_comment` |
| `agent/settings.go` | Reads `OPENAI_API_KEY`, `OPENAI_BASE_URL` and `MODEL` |
| `agent/agent_test.go` | Runs the loop against a fake model and a fake Hacker News API |

## 1. Install

You need Go 1.27 or later. With an older Go, `go` downloads 1.27 for you.

```sh
git clone https://github.com/HaikeiLabs/agentware.git
cd agentware/examples/go/first-agent
```

The example builds against the Go module in this repository (see the
`replace` line in `go.mod`).

## 2. Set the key

```sh
export OPENAI_API_KEY=your-key
export MODEL=gpt-4o-mini
```

For any other OpenAI-compatible endpoint, also set `OPENAI_BASE_URL`. For
example, for a local Ollama server:

```sh
export OPENAI_BASE_URL=http://localhost:11434/v1
export OPENAI_API_KEY=ollama   # Ollama ignores the key, but it must be set
export MODEL=qwen3
```

Pick a model that supports tool calling.

## 3. Run

```sh
go run .
```

The backend logs each model request to stderr, on lines that start with
`[LLM]`. To hide them, run `go run . 2>/dev/null`. A local model can take a
minute or two to answer.

## 4. Read the output

Output from a real run, shortened:

```text
> Summarize today's top five Hacker News stories.

Here's a summary of today's top five Hacker News stories:

## 1. Search for everyone (394 points)
Kagi announced a change related to search access ...
🔗 https://kagi.com/changelog#11820
...

audit tool=hn_top_stories decision=allow rule=local:allow success=true args_sha256=a03c8657a575 latency_ms=381

hn_post_comment: denied by policy: hn_post_comment is blocked by the local rule local:deny-hn_post_comment
audit tool=hn_post_comment decision=deny rule=local:deny-hn_post_comment success=false args_sha256=6a509c020f64 latency_ms=0
```

- The first block is the model's answer. The model called `hn_top_stories`
  to get the titles and URLs.
- Each `audit` line is one tool call: which tool ran, the decision, the rule
  that made it, and whether the call succeeded. `args_sha256` is the start of
  a SHA-256 digest of the arguments. The audit record never stores the
  arguments themselves.
- The last two lines come from `main.go`, which tries `hn_post_comment` on
  purpose. The local rule denies it, so the tool never runs.

## 5. Add your own tool

Give the agent a tool that reads a story's comments. Add this to a new file,
`agent/comments.go`:

```go
package agent

import (
    "context"
    "fmt"
    "net/http"
    "strings"

    "github.com/haikeilabs/agentware/go/tools"
)

// storyCommentsTool reads the first comments on one story.
type storyCommentsTool struct {
    baseURL string
    client  *http.Client
}

func (t *storyCommentsTool) Name() string { return "hn_story_comments" }

func (t *storyCommentsTool) Description() string {
    return "Read the first five comments on a Hacker News story."
}

func (t *storyCommentsTool) InputSchema() map[string]any {
    return map[string]any{
        "type": "object",
        "properties": map[string]any{
            "story_id": map[string]any{"type": "integer", "description": "The story ID."},
        },
        "required": []string{"story_id"},
    }
}

func (t *storyCommentsTool) Execute(ctx context.Context, args map[string]any) (*tools.Result, error) {
    id, _ := args["story_id"].(float64)
    type item struct {
        Kids []int  `json:"kids"`
        By   string `json:"by"`
        Text string `json:"text"`
    }
    story, err := getJSON[item](ctx, t.client, fmt.Sprintf("%s/item/%d.json", t.baseURL, int(id)))
    if err != nil {
        return nil, err
    }
    var out strings.Builder
    for _, kid := range story.Kids[:min(5, len(story.Kids))] {
        c, err := getJSON[item](ctx, t.client, fmt.Sprintf("%s/item/%d.json", t.baseURL, kid))
        if err != nil {
            return nil, err
        }
        fmt.Fprintf(&out, "%s: %s\n", c.By, c.Text)
    }
    return &tools.Result{Success: true, Output: out.String()}, nil
}
```

Register it in `New` in `agent/agent.go`, next to the other tools:

```go
registry.Register(&storyCommentsTool{baseURL: strings.TrimSuffix(cfg.HNBaseURL, "/"), client: cfg.HTTPClient})
```

Then change `Prompt` in `agent/agent.go` to a question that needs the new
tool, such as "What do commenters think of the top story?", and run
`go run .` again. The new tool's calls show up in the audit lines.

To block a tool, add its name to `localPolicy` in `agent/policy.go`.

## Test

```sh
go test ./...
```

The test runs the loop against a fake OpenAI-compatible server and a fake
Hacker News API, so it needs no key and no network.

## Next

To have Kei allow or deny tool calls instead of a local rule, see the
[minimal harness](../minimal-harness/README.md).
