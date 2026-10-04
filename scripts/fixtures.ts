import type { Quote, QuoteService, TrustedTask } from '../src/contracts.ts';
import { IntegrationError } from '../src/deepseek.ts';

export const DEMO_TASK: TrustedTask = {
  principalId: 'synthetic-user', taskId: 'synthetic-shopping-task',
  role: 'offer_analyst', phase: 'research', sku: 'demo-tissue-3ply-10pack',
  quantity: 1, budgetMinor: 10_000, allowedMerchantIds: ['demo-A', 'demo-B'],
};
export function createFixtureService(): QuoteService {
  const common = { sku: DEMO_TASK.sku, quantity: 1, currency: 'HKD' as const,
    destination: 'HK' as const, discountMinor: 0, discountEligible: false,
    expiresAt: Date.now() + 300_000, dataMode: 'synthetic' as const };
  const quotes: Quote[] = [
    { ...common, quoteId: 'q-A', merchantId: 'demo-A', itemMinor: 8000,
      shippingMinor: null, evidenceRefs: ['fixture:A:v1'] },
    { ...common, quoteId: 'q-B', merchantId: 'demo-B', itemMinor: 8500,
      shippingMinor: 0, evidenceRefs: ['fixture:B:v1'] },
  ];
  function check(task: TrustedTask) {
    if (task.principalId !== DEMO_TASK.principalId || task.taskId !== DEMO_TASK.taskId) {
      throw new IntegrationError('FIXTURE_TASK_OWNER_MISMATCH');
    }
  }
  return {
    async assertActive(task) { check(task); },
    async list(task) { check(task); return structuredClone(quotes); },
    async refresh(task, quoteId) {
      check(task);
      const quote = quotes.find(q => q.quoteId === quoteId);
      if (!quote) throw new IntegrationError('FIXTURE_QUOTE_NOT_FOUND');
      return { ...quote, shippingMinor: quoteId === 'q-A' ? 3000 : 0,
        evidenceRefs: [`fixture:${quoteId}:v2`] };
    },
  };
}

export function completion(calls: Array<{ name: string; args: unknown; id?: string }>, index = 1) {
  return {
    id: `fixture-response-${index}`, object: 'chat.completion', created: 1,
    model: 'deepseek-flash',
    choices: [{ index: 0, finish_reason: calls.length ? 'tool_calls' : 'stop',
      message: { role: 'assistant', reasoning_content: `synthetic-protocol-marker-${index}`,
        content: calls.length ? null : '模拟数据：A 补查后为 HKD 110，超过预算；B 为 HKD 85，草案待用户确认。',
        ...(calls.length ? { tool_calls: calls.map((c, i) => ({
          id: c.id ?? `call-${index}-${i}`, type: 'function',
          function: { name: c.name, arguments: JSON.stringify(c.args) },
        })) } : {}),
      } }],
    usage: { prompt_tokens: 20, completion_tokens: 10, total_tokens: 30 },
  };
}

export function scriptedFetch(responses: unknown[]) {
  const requests: Array<Record<string, any>> = [];
  const fetch: typeof globalThis.fetch = async (_input, init) => {
    requests.push(JSON.parse(String(init?.body)));
    if (!responses.length) throw new Error('NO_MORE_FIXTURE_RESPONSES');
    return new Response(JSON.stringify(responses.shift()), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  };
  return { requests, fetch };
}

export function successfulScript() {
  return scriptedFetch([
    completion([{ name: 'lookup_quotes', args: {} }], 1),
    completion([{ name: 'refresh_quote', args: { quoteId: 'q-A' } }], 2),
    completion([{ name: 'propose_choice', args: { quoteId: 'q-B' } }], 3),
    completion([], 4),
  ]);
}

export const DEMO_PROMPT = '请比较当前任务中两家商户的同规格纸巾。A 运费未知，请补查；预算 HKD 100。核实后形成一个待确认草案，并列出证据与未知。数据均为模拟，不能下单或付款。';
