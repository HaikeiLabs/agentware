import { Action, type AsyncBackend, type Message, Role } from '../src/index.js';

function fakeBackend(calls: Message[][]): AsyncBackend {
  return {
    complete: async (messages) => {
      calls.push([...messages]);
      return { content: calls.length === 1 ? '' : 'Five stories summarized', reasoning: '',
        tool_calls: calls.length === 1 ? [{ id: 'call-1', name: 'hn_top_stories', arguments: {} }] : [],
        finish_reason: 'stop', usage_tokens: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 } };
    },
    supportsNativeToolCalling: () => true, modelName: () => 'fake', contextWindowSize: () => 8192,
  };
}

function fakeFetch(urls: string[]) {
  return async (url: string) => {
    urls.push(url);
    return { ok: true, status: 200, json: async () => url.endsWith('/topstories.json') ? [1, 2, 3, 4, 5, 6] : { title: `Story ${url.match(/item\/(\d+)\.json/)![1]}`, url: `https://example.com/${url.match(/item\/(\d+)\.json/)![1]}` } };
  };
}

test('first agent fetches five stories, audits the tool call, and summarizes them', async () => {
  const modulePath = '../../examples/typescript/first-agent/agent.js';
  const { runAgent } = await import(modulePath);
  const calls: Message[][] = [];
  const urls: string[] = [];
  const audits: any[] = [];
  const { result, client } = await runAgent(fakeBackend(calls), fakeFetch(urls), (record: unknown) => audits.push(record));
  expect(result.final_response).toBe('Five stories summarized');
  expect(urls).toHaveLength(6);
  expect(calls[1].find(m => m.role === Role.TOOL)?.content).toContain('Story 1');
  expect(audits).toHaveLength(1);
  expect(audits[0]).toMatchObject({ tool_name: 'hn_top_stories', decision: { action: Action.ALLOW } });
  const [, allowed] = await client.execute('hn_post_comment', {}, { trusted: false });
  expect(allowed).toBe(false);
  expect(audits[1]).toMatchObject({ tool_name: 'hn_post_comment', decision: { action: Action.DENY } });
});

test('first agent CLI run prints the summary, then the denied tool call and its audit record', async () => {
  const modulePath = '../../examples/typescript/first-agent/agent.js';
  const { runCli } = await import(modulePath);
  const lines: string[] = [];
  await runCli(fakeBackend([]), fakeFetch([]), (line: string) => lines.push(line));

  const audits = lines.filter(line => line.startsWith('AUDIT ')).map(line => JSON.parse(line.slice('AUDIT '.length)));
  expect(audits).toHaveLength(2);
  expect(audits[0]).toMatchObject({ tool_name: 'hn_top_stories', decision: { action: Action.ALLOW, rule: 'read-stories' } });
  expect(audits[1]).toMatchObject({ tool_name: 'hn_post_comment', decision: { action: Action.DENY, rule: 'deny-comments' } });
  expect(lines).toContain('Five stories summarized');
  expect(lines.at(-1)).toBe('DENIED hn_post_comment: denied by policy: Only reading stories is allowed');
  expect(lines.indexOf('Five stories summarized')).toBeLessThan(lines.findIndex(line => line.includes('"hn_post_comment"')));
});
