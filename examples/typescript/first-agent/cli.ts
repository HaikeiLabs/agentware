import { OpenAIBackend } from '@haikeilabs/agentware';
import { runCli } from './agent.ts';

if (!process.env.OPENAI_API_KEY) throw new Error('Set OPENAI_API_KEY before running the agent');
const backend = new OpenAIBackend({
  apiKey: process.env.OPENAI_API_KEY,
  baseUrl: process.env.OPENAI_BASE_URL,
  model: process.env.MODEL ?? 'gpt-4o-mini',
});
// Summarize the top stories, then call hn_post_comment, which the local policy denies.
await runCli(backend);
