import { AgentLoop } from '../src/executor/index.js';
import { OpenAIBackend, Role } from '../src/llm/index.js';
import { ResponseValidator } from '../src/middleware/index.js';
import { Result, ToolRegistry } from '../src/tools/index.js';

test('native tool calls retain their IDs in the conversation and OpenAI requests', async () => {
  const requests: Array<{ messages: Array<Record<string, unknown>> }> = [];
  const backend = new OpenAIBackend({ model: 'test', fetchFn: async (_url, init) => {
    requests.push(JSON.parse(init!.body!));
    const message = requests.length === 1
      ? { content: null, tool_calls: [
          { id: 'call-one', function: { name: 'ok', arguments: '{"x":1}' } },
          { id: 'call-two', function: { name: 'fail', arguments: '{}' } },
        ] }
      : { content: 'done' };
    return { ok: true, status: 200, text: async () => JSON.stringify({ choices: [{ message }] }) };
  }});
  const registry = new ToolRegistry();
  registry.register({ name: 'ok', description: 'ok', execute: () => new Result(true, 'ok') });
  registry.register({ name: 'fail', description: 'fail', execute: () => new Result(false, null, 'failed') });
  const result = await new AgentLoop({ backend, registry, validator: new ResponseValidator(registry.names()) }).run('sys', 'go');
  expect(result.conversation[2]).toMatchObject({ role: Role.ASSISTANT, tool_calls: [
    { id: 'call-one', name: 'ok', arguments: { x: 1 } },
    { id: 'call-two', name: 'fail', arguments: {} },
  ] });
  expect(result.conversation.filter(m => m.role === Role.TOOL).map(m => m.tool_call_id)).toEqual(['call-one', 'call-two']);
  expect(requests[1].messages.slice(2)).toMatchObject([
    { role: 'assistant', tool_calls: [
      { id: 'call-one', function: { name: 'ok', arguments: '{"x":1}' } },
      { id: 'call-two', function: { name: 'fail', arguments: '{}' } },
    ] },
    { role: 'tool', tool_call_id: 'call-one' },
    { role: 'tool', tool_call_id: 'call-two' },
  ]);
});

test('native calls retain IDs for unknown and blocked tools', async () => {
  const requests: Array<{ messages: Array<Record<string, unknown>> }> = [];
  const backend = new OpenAIBackend({ model: 'test', fetchFn: async (_url, init) => {
    requests.push(JSON.parse(init!.body!));
    const message = requests.length === 1 ? { tool_calls: [
      { id: 'missing-id', function: { name: 'missing', arguments: '{}' } },
      { id: 'blocked-id', function: { name: 'blocked', arguments: '{}' } },
    ] } : { content: 'done' };
    return { ok: true, status: 200, text: async () => JSON.stringify({ choices: [{ message }] }) };
  }});
  const registry = new ToolRegistry();
  registry.register({ name: 'blocked', description: 'blocked', execute: () => new Result(true) });
  const { ErrorTracker, ErrorCategory } = await import('../src/middleware/index.js');
  const tracker = new ErrorTracker(1);
  tracker.recordError('', 'blocked', {}, new Error('failed'), ErrorCategory.UNKNOWN);
  const loop = new AgentLoop({ backend, registry, error_tracker: tracker, validator: new ResponseValidator(['missing', 'blocked']) });
  const result = await loop.run('sys', 'go');
  expect(result.conversation.filter(m => m.role === Role.TOOL).map(m => m.tool_call_id)).toEqual(['missing-id', 'blocked-id']);
  expect(requests[1].messages.slice(-2)).toMatchObject([
    { role: 'tool', tool_call_id: 'missing-id', content: 'Tool missing error: unknown tool' },
    { role: 'tool', tool_call_id: 'blocked-id', content: 'Tool blocked error: blocked after repeated errors' },
  ]);
});
