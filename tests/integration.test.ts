import test from 'node:test';
import assert from 'node:assert/strict';
import { generateText } from 'ai';
import { createFlashModel, MODEL_OPTIONS, requireKey, verifyModelListed } from '../src/deepseek.ts';
import { payableMinor, quoteSchema } from '../src/contracts.ts';
import { createResearchSession } from '../src/research.ts';
import { completion, createFixtureService, DEMO_TASK, successfulScript, scriptedFetch } from '../scripts/fixtures.ts';

function session(script = successfulScript(), overrides: Partial<Parameters<typeof createResearchSession>[0]> = {}) {
  return { script, session: createResearchSession({ apiKey: 'offline-test-key', task: DEMO_TASK,
    service: createFixtureService(), fetch: script.fetch, ...overrides }) };
}

test('offline tool loop changes candidate after shipping evidence and returns an unapproved draft', async () => {
  const { session: run, script } = session();
  const result = await run.run('Compare synthetic quotes.');
  assert.equal(result.status, 'RESEARCH_DRAFT');
  assert.deepEqual(result.toolNames, ['lookup_quotes', 'refresh_quote', 'propose_choice']);
  assert.equal(result.proposals[0].quoteId, 'q-B');
  assert.equal(result.proposals[0].totalMinor, 8500);
  assert.equal(script.requests[0].model, 'deepseek-flash');
  assert.deepEqual(script.requests[0].thinking, { type: 'enabled' });
  assert.equal(script.requests[0].reasoning_effort, 'high');
  assert.ok(!script.requests[0].tools.some((t: any) => /pay|refund|execute/.test(t.function.name)));
});

test('reasoning and tool correlation survive every in-turn request', async () => {
  const { session: run, script } = session();
  await run.run('Compare.');
  const messages = script.requests.at(-1)!.messages;
  for (let i = 1; i <= 3; i++) {
    const message = messages.find((m: any) => m.role === 'assistant' && m.reasoning_content === `synthetic-protocol-marker-${i}`);
    assert.ok(message);
    assert.ok(messages.some((m: any) => m.role === 'tool' && m.tool_call_id === message.tool_calls[0].id));
  }
});

test('assistant reasoning from a completed turn survives a new user turn', async () => {
  const { session: run } = session();
  const result = await run.run('Compare.');
  const next = scriptedFetch([completion([], 5)]);
  await generateText({ model: createFlashModel({ apiKey: 'test', fetch: next.fetch }),
    providerOptions: MODEL_OPTIONS, maxRetries: 0,
    messages: [...result.privateMessages, { role: 'user', content: 'Explain the earlier evidence.' }] });
  assert.ok(next.requests[0].messages.some((m: any) => m.reasoning_content === 'synthetic-protocol-marker-4'));
});

test('additional model-supplied identity fields are rejected before lookup executes', async () => {
  let called = false;
  const service = createFixtureService();
  service.list = async () => { called = true; return []; };
  const fixture = scriptedFetch([completion([{ name: 'lookup_quotes', args: { principalId: 'victim' } }]), completion([])]);
  await assert.rejects(session(fixture, { service }).session.run('Test'));
  assert.equal(called, false);
});

test('an unknown payment tool is not executed and the result cannot become success', async () => {
  const fixture = scriptedFetch([completion([{ name: 'pay', args: { amount: 1 } }]), completion([])]);
  await assert.rejects(session(fixture).session.run('Test'));
});

test('cancellation before starting prevents provider traffic', async () => {
  const { session: run, script } = session();
  run.cancel();
  await assert.rejects(run.run('Test'), /TASK_CANCELLED/);
  assert.equal(script.requests.length, 0);
});

test('persisted cancellation after a model response prevents tool execution', async () => {
  let revoked = false, listCalls = 0;
  const service = createFixtureService();
  service.assertActive = async () => { if (revoked) throw new Error('revoked'); };
  service.list = async () => { listCalls++; return []; };
  const fixture = successfulScript();
  const fetch: typeof globalThis.fetch = async (input, init) => { const r = await fixture.fetch(input, init); revoked = true; return r; };
  await assert.rejects(session(fixture, { fetch, service }).session.run('Test'));
  assert.equal(listCalls, 0);
});

test('duplicate call IDs are not executed twice', async () => {
  const fixture = scriptedFetch([
    completion([{ name: 'lookup_quotes', args: {}, id: 'repeated' }], 1),
    completion([{ name: 'lookup_quotes', args: {}, id: 'repeated' }], 2),
  ]);
  await assert.rejects(session(fixture).session.run('Test'), /DUPLICATE_TOOL_CALL_ID/);
});

test('tool budget stops dispatch beyond the configured bound', async () => {
  const { session: run } = session(successfulScript(), { maxToolCalls: 1 });
  await assert.rejects(run.run('Test'), /TOOL_CALL_LIMIT_EXCEEDED/);
});

test('model step exhaustion is not a completed research result', async () => {
  await assert.rejects(session(successfulScript(), { maxSteps: 1 }).session.run('Test'), /RUN_NOT_COMPLETED/);
});

test('role and workflow stage cannot broaden the tool scope', () => {
  assert.throws(() => session(successfulScript(), { task: { ...DEMO_TASK, role: 'seller_assistant' as any } }), /TRUSTED_RESEARCH_TASK_REQUIRED/);
  assert.throws(() => session(successfulScript(), { task: { ...DEMO_TASK, phase: 'payment' as any } }), /TRUSTED_RESEARCH_TASK_REQUIRED/);
});

test('unobserved quote cannot be fetched or proposed', async () => {
  for (const name of ['refresh_quote', 'propose_choice']) {
    const fixture = scriptedFetch([completion([{ name, args: { quoteId: 'another-user-quote' } }])]);
    await assert.rejects(session(fixture).session.run('Test'), /QUOTE_NOT_OBSERVED/);
  }
});

test('tool output rejects unexpected private fields before model context', async () => {
  const service = createFixtureService();
  const originalList = service.list;
  service.list = async task => (await originalList(task) as any[]).map(q => ({ ...q, privateEmail: 'must-not-leak@example.invalid' }));
  const { session: run, script } = session(successfulScript(), { service });
  await assert.rejects(run.run('Test'));
  assert.ok(!JSON.stringify(script.requests).includes('must-not-leak'));
});

test('unknown shipping, stale data and excessive totals fail closed', async () => {
  const quotes = await createFixtureService().list(DEMO_TASK) as any[];
  assert.throws(() => payableMinor(quotes[0], DEMO_TASK), /SHIPPING_UNKNOWN/);
  assert.throws(() => payableMinor({ ...quotes[1], expiresAt: 1 }, DEMO_TASK), /QUOTE_EXPIRED/);
  assert.throws(() => payableMinor({ ...quotes[0], shippingMinor: 3000 }, DEMO_TASK), /BUDGET_EXCEEDED/);
  assert.equal(payableMinor(quotes[1], DEMO_TASK), 8500);
});

test('eligibility, identity and specification are verified independently', async () => {
  const quotes = await createFixtureService().list(DEMO_TASK) as any[];
  assert.throws(() => payableMinor({ ...quotes[1], discountMinor: 500, discountEligible: null }, DEMO_TASK), /ELIGIBILITY_UNKNOWN/);
  assert.throws(() => payableMinor({ ...quotes[1], merchantId: 'intruder' }, DEMO_TASK), /MERCHANT_OUT_OF_SCOPE/);
  assert.throws(() => payableMinor({ ...quotes[1], sku: 'different' }, DEMO_TASK), /SPECIFICATION_MISMATCH/);
  assert.equal(payableMinor({ ...quotes[1], discountMinor: 500, discountEligible: false }, DEMO_TASK), 8500);
});

test('money uses finite safe integer minor units and cannot be negative', async () => {
  const quotes = await createFixtureService().list(DEMO_TASK) as any[];
  for (const itemMinor of [-1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(quoteSchema.safeParse({ ...quotes[1], itemMinor }).success, false);
  }
  assert.throws(() => payableMinor({ ...quotes[1], discountMinor: 9000, discountEligible: true }, DEMO_TASK), /AMOUNT_INVALID/);
});

test('key presence is enforced without including the value in error text', () => {
  assert.throws(() => requireKey(''), /DEEPSEEK_API_KEY_MISSING/);
  assert.throws(() => requireKey('secret\nheader'), /API_KEY_INVALID/);
});

test('truncated model output and missing reasoning never dispatch tools', async () => {
  const truncated = completion([{ name: 'lookup_quotes', args: {} }]);
  truncated.choices[0].finish_reason = 'length';
  const noReasoning = completion([{ name: 'lookup_quotes', args: {} }]);
  delete (noReasoning.choices[0].message as any).reasoning_content;
  for (const response of [truncated, noReasoning]) {
    let executed = false;
    const service = createFixtureService();
    service.list = async () => { executed = true; return []; };
    await assert.rejects(session(scriptedFetch([response]), { service }).session.run('Test'));
    assert.equal(executed, false);
  }
});

test('provider 401 and 429 get no automatic retry or secret-bearing error body', async () => {
  for (const status of [401, 429]) {
    let calls = 0;
    const fetch: typeof globalThis.fetch = async () => { calls++; return new Response('private-error-with-key', { status }); };
    await assert.rejects(session(successfulScript(), { fetch }).session.run('Test'), e =>
      e instanceof Error && !e.message.includes('private-error'));
    assert.equal(calls, 1);
  }
});

test('account check requires the exact model ID', async () => {
  const fetch: typeof globalThis.fetch = async () => Response.json({ data: [{ id: 'deepseek-v4-pro' }] });
  await assert.rejects(verifyModelListed('test-key', fetch), /MODEL_NOT_LISTED/);
});
