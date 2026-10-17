package guardrails

import "fmt"

func ExampleNewGuardrails() {
	config, err := LoadConfig([]byte(`{"version":1,"tool_names":["lookup","answer"],"required_steps":["lookup"],"terminal_tool":"answer"}`))
	if err != nil {
		panic(err)
	}
	guardrails, err := NewGuardrails(config)
	if err != nil {
		panic(err)
	}
	first := guardrails.Check("session-1", []ToolCall{{Tool: "lookup"}})
	fmt.Println(first.Outcome)
	guardrails.Record("session-1", first.ToolCalls[0], nil, "")
	final := guardrails.Check("session-1", []ToolCall{{Tool: "answer"}})
	fmt.Println(final.Outcome)
	// Output:
	// execute
	// execute
}
