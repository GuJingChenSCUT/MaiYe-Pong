import { createResearchSession } from '../src/research.ts';
import { IntegrationError, verifyModelListed } from '../src/deepseek.ts';
import { createFixtureService, DEMO_PROMPT, DEMO_TASK } from './fixtures.ts';

if (!process.argv.includes('--live')) {
  console.log(JSON.stringify({ status: 'SKIPPED', reason: 'Use --live to opt into billable model requests. No network calls made.' }));
} else if (!process.env.DEEPSEEK_API_KEY) {
  console.log(JSON.stringify({ status: 'BLOCKED', reason: 'DEEPSEEK_API_KEY_MISSING' }));
  process.exitCode = 2;
} else {
  try {
    const apiKey = process.env.DEEPSEEK_API_KEY;
    const model = await verifyModelListed(apiKey);
    const session = createResearchSession({ apiKey, task: DEMO_TASK, service: createFixtureService() });
    const result = await session.run(DEMO_PROMPT);
    if (!['lookup_quotes', 'refresh_quote', 'propose_choice'].every(name => result.toolNames.includes(name)) ||
        !result.proposals.some(p => p.quoteId === 'q-B' && p.totalMinor === 8500) ||
        result.proposals.some(p => p.quoteId === 'q-A')) {
      throw new IntegrationError('SCENARIO_ACCEPTANCE_FAILED');
    }
    console.log(JSON.stringify({ status: 'LIVE_MODEL_SYNTHETIC_TOOLS_PASSED', model,
      toolNames: result.toolNames, proposals: result.proposals, usage: result.usage,
      liveMerchantVerified: false, paymentVerified: false, convexPersistenceVerified: false }, null, 2));
  } catch (error) {
    // No raw SDK error, prompt, authorization header or reasoning is logged.
    console.error(JSON.stringify({ status: 'FAILED', code: error instanceof IntegrationError ? error.message : 'SMOKE_FAILED' }));
    process.exitCode = 1;
  }
}
