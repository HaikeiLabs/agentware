package middleware

type PolicyEvaluator interface {
	Evaluate(toolName string, args map[string]any, caller CallerContext) Decision
}
