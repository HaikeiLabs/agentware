import {
  CONTRACT_VERSION,
  ContextTree,
  Format,
  isContextTreeEmpty,
  MalformedContentError,
  MalformedInputError,
  MalformedJSONError,
  MalformedReasoningFieldError,
  NodeKind,
  NodeStatus,
  ReasoningAdapter,
  ReasoningOptions,
  ReasoningTooLargeError,
  TooDeepError,
  TooManyNodesError,
  UnbalancedThinkingError,
} from "../src/reasoning/index.js";

const FIXED = new Date("2026-09-10T12:00:00Z");

function fixedClock(): Date {
  return new Date(FIXED.getTime());
}

describe("strip", () => {
  it("accepts a bare OpenAI message", () => {
    const adapter = new ReasoningAdapter();
    expect(
      adapter.strip({ role: "assistant", content: "answer", reasoning_content: "thought" })
    ).toBe("answer");
  });

  it("unwraps choices[0].message from a full OpenAI response", () => {
    const adapter = new ReasoningAdapter();
    expect(
      adapter.strip({
        choices: [{ message: { role: "assistant", content: "answer", reasoning_content: "thought" } }],
      })
    ).toBe("answer");
  });

  it("extracts Anthropic typed content parts", () => {
    const adapter = new ReasoningAdapter();
    const raw = { role: "assistant", content: [
      { type: "thinking", thinking: "first thought" },
      { type: "tool_use", name: "search", input: {} },
      { type: "text", text: "answer" },
    ] };
    expect(adapter.strip(raw)).toBe("answer");
    expect(adapter.extract(raw).nodes.map((node) => node.summary)).toEqual(["first thought"]);
  });

  it("handles native reasoning_content", () => {
    const adapter = new ReasoningAdapter();
    const raw = {
      message: {
        role: "assistant",
        content: '{"tool": "search", "args": {"q": "x"}}',
        reasoning_content: "I need to search first.",
      },
    };
    expect(adapter.strip(raw)).toBe('{"tool": "search", "args": {"q": "x"}}');
  });

  it("strips thinking tags", () => {
    const adapter = new ReasoningAdapter();
    const raw = '<thinking>First, check the index.</thinking>\n[TOOL_CALLS] tool1: {"a":1} [/TOOL_CALLS]';
    expect(adapter.strip(raw)).toBe('\n[TOOL_CALLS] tool1: {"a":1} [/TOOL_CALLS]');
  });

  it("passes through content with no reasoning", () => {
    const adapter = new ReasoningAdapter();
    const cases = [
      '{"tool": "search", "args": {}}',
      '[{"id": "1", "name": "search", "arguments": {}}]',
      "plain text response",
      "",
    ];
    for (const raw of cases) {
      expect(adapter.strip(raw)).toBe(raw);
    }
  });

  it("handles a model-specific field", () => {
    const adapter = new ReasoningAdapter();
    adapter.registerModelField("spark", "thinking_payload");
    const raw = { content: "hello", thinking_payload: "spark reasoning" };
    expect(adapter.strip(raw)).toBe("hello");
  });
});

describe("fail-closed", () => {
  it("rejects a non-string reasoning field", () => {
    const adapter = new ReasoningAdapter();
    expect(() => adapter.strip({ reasoning_content: ["not", "a", "string"] })).toThrow(
      MalformedReasoningFieldError
    );
  });

  it("rejects a non-string content field", () => {
    const adapter = new ReasoningAdapter();
    expect(() => adapter.strip({ content: ["parts"] })).toThrow(MalformedContentError);
  });

  it("rejects malformed Anthropic content parts", () => {
    const adapter = new ReasoningAdapter();
    expect(() => adapter.strip({ content: ["not an object"] })).toThrow(MalformedContentError);
  });

  it("rejects unbalanced thinking tags", () => {
    const adapter = new ReasoningAdapter();
    expect(() => adapter.strip("<thinking>never closed")).toThrow(UnbalancedThinkingError);
  });

  it("rejects a dangling close tag", () => {
    const adapter = new ReasoningAdapter();
    expect(() => adapter.strip("no open tag [/THINK]")).toThrow(UnbalancedThinkingError);
  });

  it("rejects malformed JSON that looks structured", () => {
    const adapter = new ReasoningAdapter();
    expect(() => adapter.strip('{"message": {"content": "x"}')).toThrow(MalformedJSONError);
  });

  it("rejects unsupported types", () => {
    const adapter = new ReasoningAdapter();
    expect(() => adapter.strip(42)).toThrow(MalformedInputError);
  });
});

describe("bounds", () => {
  it("rejects reasoning over the byte cap", () => {
    const adapter = new ReasoningAdapter({ max_reasoning_bytes: 16 });
    expect(() =>
      adapter.extract({ content: "", reasoning_content: "x".repeat(64) }, "deepseek", "test")
    ).toThrow(ReasoningTooLargeError);
  });

  it("rejects too many nodes", () => {
    const adapter = new ReasoningAdapter({ max_nodes: 2, clock: fixedClock });
    expect(() => adapter.extract("<thinking>p1\n\np2\n\np3\n\np4</thinking>", "m", "b")).toThrow(
      TooManyNodesError
    );
  });

  it("rejects a chain that is too deep", () => {
    const adapter = new ReasoningAdapter({ max_depth: 2, clock: fixedClock });
    expect(() => adapter.extract("<thinking>a\n\nb\n\nc</thinking>", "m", "b")).toThrow(
      TooDeepError
    );
  });
});

describe("extract", () => {
  it("builds a versioned context tree", () => {
    const adapter = new ReasoningAdapter({ clock: fixedClock });
    const raw =
      "<thinking>" +
      "goal: verify the payment gateway\n\n" +
      "observation: gateway returned 200 [E1]\n\n" +
      "decision: call refund tool\n\n" +
      'tool-call: refund_tool: {"id": "txn1"}\n\n' +
      "tool-result: refund_tool: ok [SUPERSEDED]\n\n" +
      "conclusion: refund issued\n\n" +
      "blocker: regulatory approval pending" +
      "</thinking>";

    const tree = adapter.extract(raw, "deepseek-reasoner", "test-backend");

    expect(tree.version).toBe(CONTRACT_VERSION);
    expect(tree.format).toBe(Format.THINKING);
    expect(tree.model).toBe("deepseek-reasoner");
    expect(tree.backend).toBe("test-backend");
    expect(tree.root_id).not.toBe("");

    expect(tree.nodes).toHaveLength(7);
    expect(tree.nodes.map((n) => n.kind)).toEqual([
      NodeKind.GOAL,
      NodeKind.OBSERVATION,
      NodeKind.DECISION,
      NodeKind.TOOL_CALL,
      NodeKind.TOOL_RESULT,
      NodeKind.CONCLUSION,
      NodeKind.BLOCKER,
    ]);

    // Chain: root has empty parent, every other node's parent is its predecessor.
    expect(tree.nodes[0].parent_id).toBe("");
    for (let i = 1; i < tree.nodes.length; i++) {
      expect(tree.nodes[i].parent_id).toBe(tree.nodes[i - 1].id);
    }

    expect(tree.nodes[0].status).toBe(NodeStatus.COMPLETE);
    expect(tree.nodes[4].status).toBe(NodeStatus.SUPERSEDED);
    expect(tree.nodes[6].status).toBe(NodeStatus.BLOCKED);

    expect(tree.nodes[1].evidence_refs).toEqual(["E1"]);
    expect(tree.nodes[3].tool_call_refs).toEqual(["refund_tool"]);

    for (const node of tree.nodes) {
      expect(node.created_at.toISOString()).toBe(FIXED.toISOString());
      expect(node.updated_at.toISOString()).toBe(FIXED.toISOString());
    }
  });

  it("returns an empty tree when there is no reasoning", () => {
    const adapter = new ReasoningAdapter({ clock: fixedClock });
    const tree = adapter.extract({ content: "hello" }, "m", "b");
    expect(tree.nodes).toHaveLength(0);
    expect(tree.root_id).toBe("");
    expect(tree.format).toBe(Format.NONE);
  });

  it("produces deterministic node ids", () => {
    const adapter = new ReasoningAdapter({ clock: fixedClock });
    const raw = "<thinking>observation: x\n\ndecision: y</thinking>";
    const t1 = adapter.extract(raw, "m", "b");
    const t2 = adapter.extract(raw, "m", "b");
    expect(t1.nodes.map((n) => n.id)).toEqual(t2.nodes.map((n) => n.id));
    expect(t1.root_id).toBe(t2.root_id);
  });

  it("bounds summaries and never stores raw reasoning", () => {
    const adapter = new ReasoningAdapter({ max_summary_chars: 10, clock: fixedClock });
    const raw = "<thinking>observation: " + "abcdefg".repeat(20) + "</thinking>";
    const tree = adapter.extract(raw, "m", "b");
    expect(tree.nodes).toHaveLength(1);
    expect(Array.from(tree.nodes[0].summary).length).toBeLessThanOrEqual(10);
    expect(tree.nodes[0].summary).not.toContain("abcdefg".repeat(20));
  });

  it("truncates on code-point boundaries", () => {
    const adapter = new ReasoningAdapter({ max_summary_chars: 3, clock: fixedClock });
    const raw = "<thinking>observation: héllo wörld 🚀 end</thinking>";
    const tree = adapter.extract(raw, "m", "b");
    expect(tree.nodes[0].summary).toBe("hél");
  });
});

describe("parse", () => {
  it("returns clean content and the tree", () => {
    const adapter = new ReasoningAdapter({ clock: fixedClock });
    const raw = "<thinking>observation: x</thinking>\n" + '{"tool": "search", "args": {"q": "1"}}';
    const { clean, tree } = adapter.parse(raw, "m", "b");
    expect(clean).toBe('\n{"tool": "search", "args": {"q": "1"}}');
    expect(tree.nodes.length).toBeGreaterThan(0);
  });
});

describe("contract", () => {
  it("exposes the closed vocabularies", () => {
    expect(Object.values(NodeKind)).toEqual([
      "goal",
      "observation",
      "decision",
      "tool-call",
      "tool-result",
      "conclusion",
      "blocker",
    ]);
    expect(Object.values(NodeStatus)).toEqual(["active", "complete", "blocked", "superseded"]);
    expect(Object.values(Format)).toEqual(["native", "thinking", "model_specific", "none"]);
  });

  it("serialized tree never contains whole raw reasoning", () => {
    const adapter = new ReasoningAdapter({ clock: fixedClock });
    const raw = "<thinking>observation: " + "z".repeat(1000) + "</thinking>";
    const tree = adapter.extract(raw, "m", "b");
    const serialized = JSON.stringify(tree);
    expect(serialized).not.toContain("z".repeat(1000));
    expect(Array.from(tree.nodes[0].summary).length).toBeLessThanOrEqual(512);
  });
});

describe("isContextTreeEmpty", () => {
  it("reports empty trees", () => {
    expect(isContextTreeEmpty(null)).toBe(true);
    expect(isContextTreeEmpty({ nodes: [] } as unknown as ContextTree)).toBe(true);
    const node: import("../src/reasoning/types.js").Node = {
      id: "a",
      parent_id: "",
      kind: NodeKind.GOAL,
      status: NodeStatus.COMPLETE,
      summary: "x",
      evidence_refs: [],
      tool_call_refs: [],
      created_at: FIXED,
      updated_at: FIXED,
    };
    const tree: ContextTree = {
      version: CONTRACT_VERSION,
      format: Format.NONE,
      model: "m",
      backend: "b",
      created_at: FIXED,
      root_id: "a",
      nodes: [node],
    };
    expect(isContextTreeEmpty(tree)).toBe(false);
  });
});
