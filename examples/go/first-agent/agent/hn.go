package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"

	"github.com/haikeilabs/agentware/go/tools"
)

// Tool names. They match the TypeScript first-agent example.
const (
	ToolTopStories  = "hn_top_stories"
	ToolPostComment = "hn_post_comment"
)

// HNBaseURL is the public Hacker News API.
const HNBaseURL = "https://hacker-news.firebaseio.com/v0"

const (
	defaultStories = 5
	maxStories     = 10
)

// Story is one Hacker News story.
type Story struct {
	ID    int    `json:"id"`
	Title string `json:"title"`
	URL   string `json:"url"`
	Score int    `json:"score"`
}

// topStoriesTool reads the current top stories from the Hacker News API.
type topStoriesTool struct {
	baseURL string
	client  *http.Client
}

func (t *topStoriesTool) Name() string { return ToolTopStories }

func (t *topStoriesTool) Description() string {
	return "List today's top Hacker News stories with their titles, URLs and scores."
}

func (t *topStoriesTool) InputSchema() map[string]any {
	return map[string]any{
		"type": "object",
		"properties": map[string]any{
			"limit": map[string]any{
				"type":        "integer",
				"description": "How many stories to return, from 1 to 10. Defaults to 5.",
				"minimum":     1,
				"maximum":     maxStories,
			},
		},
	}
}

func (t *topStoriesTool) Examples() []tools.ToolExample { return nil }

func (t *topStoriesTool) Execute(ctx context.Context, args map[string]any) (*tools.Result, error) {
	ids, err := getJSON[[]int](ctx, t.client, t.baseURL+"/topstories.json")
	if err != nil {
		return nil, fmt.Errorf("read top stories: %w", err)
	}
	limit := storyLimit(args["limit"])
	if len(ids) > limit {
		ids = ids[:limit]
	}

	var out strings.Builder
	for i, id := range ids {
		story, err := getJSON[Story](ctx, t.client, fmt.Sprintf("%s/item/%d.json", t.baseURL, id))
		if err != nil {
			return nil, fmt.Errorf("read story %d: %w", id, err)
		}
		if story.URL == "" {
			// Ask HN and similar posts have no external link.
			story.URL = fmt.Sprintf("https://news.ycombinator.com/item?id=%d", story.ID)
		}
		fmt.Fprintf(&out, "%d. %s\n   %s (%d points)\n", i+1, story.Title, story.URL, story.Score)
	}
	return &tools.Result{Success: true, Output: out.String()}, nil
}

// storyLimit turns the model's limit argument into a count from 1 to 10.
func storyLimit(v any) int {
	n := defaultStories
	switch x := v.(type) {
	case float64:
		n = int(x)
	case int:
		n = x
	}
	if n < 1 {
		return defaultStories
	}
	return min(n, maxStories)
}

func getJSON[T any](ctx context.Context, client *http.Client, url string) (T, error) {
	var zero T
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return zero, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return zero, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return zero, fmt.Errorf("%s returned %s", url, resp.Status)
	}
	var v T
	if err := json.NewDecoder(resp.Body).Decode(&v); err != nil {
		return zero, fmt.Errorf("decode %s: %w", url, err)
	}
	return v, nil
}

// postCommentTool stands in for a tool with side effects. The deny rule in
// policy.go stops every call before Execute runs.
type postCommentTool struct {
	onExecute func(context.Context, map[string]any)
}

func (t *postCommentTool) Name() string { return ToolPostComment }

func (t *postCommentTool) Description() string {
	return "Post a comment on a Hacker News story."
}

func (t *postCommentTool) InputSchema() map[string]any {
	return map[string]any{
		"type": "object",
		"properties": map[string]any{
			"story_id": map[string]any{"type": "integer", "description": "The story to comment on."},
			"text":     map[string]any{"type": "string", "description": "The comment text."},
		},
		"required": []string{"story_id", "text"},
	}
}

func (t *postCommentTool) Examples() []tools.ToolExample { return nil }

func (t *postCommentTool) Execute(ctx context.Context, args map[string]any) (*tools.Result, error) {
	if t.onExecute != nil {
		t.onExecute(ctx, args)
	}
	return nil, fmt.Errorf("%s is not implemented in this example", ToolPostComment)
}
