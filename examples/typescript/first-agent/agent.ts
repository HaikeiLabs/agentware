import {
  Action, AgentLoop, AgentTerminationReason, AuditedToolClient, InMemoryAuditor,
  ResponseValidator, Result, ToolRegistry,
  type AsyncBackend, type AuditRecord,
} from '@haikeilabs/agentware';

const BASE = 'https://hacker-news.firebaseio.com/v0';
type FetchStories = (url: string) => Promise<{ ok: boolean; status: number; json(): Promise<unknown> }>;

export async function runAgent(
  backend: AsyncBackend,
  fetchStories: FetchStories = fetch,
  printAudit: (record: AuditRecord) => void = (record) => console.log('AUDIT', JSON.stringify(record)),
) {
  const auditor = new InMemoryAuditor();
  const client = new AuditedToolClient('first-agent', async (name) => {
    if (name !== 'hn_top_stories') return [null, false, 'unknown tool'];
    const read = async (url: string) => {
      const response = await fetchStories(url);
      if (!response.ok) throw new Error(`Hacker News returned HTTP ${response.status}`);
      return response.json();
    };
    const ids = await read(`${BASE}/topstories.json`);
    if (!Array.isArray(ids)) throw new Error('Invalid Hacker News story list');
    const stories = await Promise.all(ids.slice(0, 5).map((id) => read(`${BASE}/item/${id}.json`)));
    return [stories.map((story: any) => ({ title: story.title, url: story.url ?? `https://news.ycombinator.com/item?id=${story.id}` })), true, ''];
  }, {
    record(record) { auditor.record(record); printAudit(record); },
    query(filter) { return auditor.query(filter); },
  }).withPolicy({ evaluate(name) {
    const allowed = name === 'hn_top_stories';
    const rule = name === 'hn_post_comment' ? 'deny-comments' : allowed ? 'read-stories' : 'deny-other-tools';
    return { action: allowed ? Action.ALLOW : Action.DENY, rule,
      reason: allowed ? 'Reading stories is allowed' : 'Only reading stories is allowed', timestamp: new Date() };
  } });
  const registry = new ToolRegistry();
  const storyTool = {
    name: 'hn_top_stories', description: 'Read the titles and URLs of the top five Hacker News stories.',
    inputSchema: () => ({ type: 'object', properties: {}, additionalProperties: false }),
    async execute(args: Record<string, unknown>) {
      const [data, success, error] = await client.execute('hn_top_stories', args, { trusted: false });
      return new Result(success, data, error);
    },
  };
  registry.register(storyTool);
  const loop = new AgentLoop({ backend, registry, validator: new ResponseValidator(registry.names()), max_iterations: 6 });
  const result = await loop.run('Use the available tool to answer the user.', "Summarize today's top five Hacker News stories.");
  if (result.termination_reason !== AgentTerminationReason.COMPLETE) throw new Error(`Agent stopped: ${result.termination_reason}`);
  return { result, client };
}
