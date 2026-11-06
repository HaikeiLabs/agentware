package agent

import "github.com/haikeilabs/agentware/go/middleware"

// Rule IDs recorded on each audit record.
const (
	DenyRuleID  = "local:deny-hn_post_comment"
	AllowRuleID = "local:allow"
)

// denyTools is a local policy: it denies the listed tools and allows the
// rest. It runs in this process. Kei is not involved.
type denyTools map[string]string

func (d denyTools) Evaluate(toolName string, _ map[string]any, _ middleware.CallerContext) middleware.Decision {
	if rule, ok := d[toolName]; ok {
		return middleware.Decision{
			Action: middleware.ActionDeny,
			Rule:   rule,
			Reason: toolName + " is blocked by the local rule " + rule,
		}
	}
	return middleware.Decision{Action: middleware.ActionAllow, Rule: AllowRuleID, Reason: "allowed by the local policy"}
}

// localPolicy denies hn_post_comment. Add a tool name here to block it too.
func localPolicy() denyTools {
	return denyTools{ToolPostComment: DenyRuleID}
}
