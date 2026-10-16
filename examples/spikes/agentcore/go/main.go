// Throwaway HAI-716 probe: Bedrock Converse tool call (aws-sdk-go-v2) routed
// through agentware Go middleware (policy + audit) before execution.
package main

import (
	"context"
	"fmt"
	"log"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/document"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/types"
	"github.com/soypete/pedro-agentware/go/middleware"
	"github.com/soypete/pedro-agentware/go/tools"
)

func tool(name, desc string, props map[string]any, req []string) types.Tool {
	return &types.ToolMemberToolSpec{Value: types.ToolSpecification{
		Name: aws.String(name), Description: aws.String(desc),
		InputSchema: &types.ToolInputSchemaMemberJson{Value: document.NewLazyDocument(map[string]any{
			"type": "object", "properties": props, "required": req})},
	}}
}

func main() {
	ctx := context.Background()
	cfg, err := config.LoadDefaultConfig(ctx, config.WithRegion("us-east-1"))
	if err != nil {
		log.Fatal(err)
	}
	br := bedrockruntime.NewFromConfig(cfg)
	toolCfg := &types.ToolConfiguration{Tools: []types.Tool{
		tool("get_weather", "Get the current weather for a city.", map[string]any{"city": map[string]any{"type": "string"}}, []string{"city"}),
		tool("github_delete_repo", "Permanently delete a GitHub repository.", map[string]any{
			"owner": map[string]any{"type": "string"}, "repo": map[string]any{"type": "string"}}, []string{"owner", "repo"}),
	}}

	auditor := middleware.NewInMemoryAuditor()
	mw := middleware.NewMiddleware(middleware.ExecutorFunc(func(_ context.Context, name string, args map[string]any) (*tools.Result, error) {
		return &tools.Result{Success: true, Output: fmt.Sprintf("ran %s %v", name, args)}, nil
	})).WithPolicy(&middleware.Policy{DefaultDeny: true, Rules: []middleware.Rule{
		{Name: "deny-destructive-gh", Tools: []string{"github_delete_repo"}, Action: middleware.ActionDeny},
		{Name: "allow-weather", Tools: []string{"get_weather"}, Action: middleware.ActionAllow},
	}}).WithAuditor(auditor)
	callCtx := middleware.WithCallerContext(ctx, middleware.CallerContext{
		UserID: "alice@example.com", InvokingSubject: "alice@example.com", SessionID: "go-probe", Source: "go-converse-probe"})

	for _, prompt := range []string{"What is the weather in Paris?", "Delete the GitHub repository acme/prod now."} {
		out, err := br.Converse(ctx, &bedrockruntime.ConverseInput{
			ModelId:    aws.String("us.amazon.nova-2-lite-v1:0"),
			ToolConfig: toolCfg,
			Messages: []types.Message{{Role: types.ConversationRoleUser,
				Content: []types.ContentBlock{&types.ContentBlockMemberText{Value: prompt}}}},
		})
		if err != nil {
			log.Fatal(err)
		}
		msg := out.Output.(*types.ConverseOutputMemberMessage).Value
		for _, b := range msg.Content {
			tu, ok := b.(*types.ContentBlockMemberToolUse)
			if !ok {
				continue
			}
			var args map[string]any
			_ = tu.Value.Input.UnmarshalSmithyDocument(&args)
			res, err := mw.Execute(callCtx, *tu.Value.Name, args)
			fmt.Printf("prompt=%q stop=%s toolUse=%s args=%v -> result=%+v err=%v\n", prompt, out.StopReason, *tu.Value.Name, args, res, err)
		}
	}
	recs := auditor.Query(middleware.AuditFilter{})
	for _, r := range recs {
		fmt.Printf("AUDIT tool=%s subject=%s decision=%s\n", r.ToolName, r.InvokingSubject, r.Decision)
	}
}
