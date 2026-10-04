import { createResearchSession } from '../src/research.ts';
import { createFixtureService, DEMO_PROMPT, DEMO_TASK, successfulScript } from './fixtures.ts';

const fixture = successfulScript();
const session = createResearchSession({ apiKey: 'offline-fixture-key',
  task: DEMO_TASK, service: createFixtureService(), fetch: fixture.fetch });
const { privateMessages: _privateMessages, ...report } = await session.run(DEMO_PROMPT);
console.log(JSON.stringify({ mode: 'OFFLINE_SCRIPTED_FIXTURE', liveModelVerified: false,
  liveMerchantVerified: false, ...report }, null, 2));
