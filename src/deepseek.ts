import { createDeepSeek, type DeepSeekLanguageModelChatOptions } from '@ai-sdk/deepseek';

export const MODEL_ID = 'deepseek-flash';
export const API_ORIGIN = 'https://api.deepseek.com';
export const MODEL_OPTIONS = {
  deepseek: { thinking: { type: 'enabled' }, reasoningEffort: 'high' } satisfies DeepSeekLanguageModelChatOptions,
};

export class IntegrationError extends Error {
  constructor(code: string) { super(code); this.name = 'IntegrationError'; }
}

export function requireKey(key: string | undefined): string {
  if (!key?.trim()) throw new IntegrationError('DEEPSEEK_API_KEY_MISSING');
  if (key !== key.trim() || /[\r\n]/.test(key)) throw new IntegrationError('API_KEY_INVALID');
  return key;
}

/** A credential-aware server adapter, never import into a browser bundle. */
export function createFlashModel(options: {
  apiKey: string;
  fetch?: typeof globalThis.fetch;
  signal?: AbortSignal;
  requestTimeoutMs?: number;
}) {
  const apiKey = requireKey(options.apiKey);
  const timeoutMs = options.requestTimeoutMs ?? 30_000;
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 60_000) {
    throw new IntegrationError('REQUEST_TIMEOUT_INVALID');
  }
  const requestFetch = options.fetch ?? globalThis.fetch;
  const guardedFetch: typeof globalThis.fetch = async (input, init) => {
    const url = input instanceof Request ? input.url : String(input);
    if (url !== `${API_ORIGIN}/chat/completions`) throw new IntegrationError('ENDPOINT_REJECTED');
    const signals = [AbortSignal.timeout(timeoutMs)];
    if (options.signal) signals.push(options.signal);
    if (init?.signal) signals.push(init.signal);
    const signal = AbortSignal.any(signals);
    signal.throwIfAborted();
    let response: Response;
    try {
      response = await requestFetch(input, { ...init, redirect: 'error', signal });
    } catch {
      throw new IntegrationError(signal.aborted ? 'MODEL_REQUEST_ABORTED' : 'MODEL_NETWORK_ERROR');
    }
    if (!response.ok) {
      await response.body?.cancel();
      throw new IntegrationError(`MODEL_HTTP_${response.status}`);
    }
    // Read a bounded non-stream response, including the body under the same timeout.
    const reader = response.body?.getReader();
    if (!reader) throw new IntegrationError('MODEL_RESPONSE_EMPTY');
    const chunks: Uint8Array[] = [];
    let size = 0;
    try {
      while (true) {
        signal.throwIfAborted();
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > 2_000_000) throw new IntegrationError('MODEL_RESPONSE_TOO_LARGE');
        chunks.push(value);
      }
    } catch (error) {
      await reader.cancel().catch(() => {});
      if (error instanceof IntegrationError) throw error;
      throw new IntegrationError('MODEL_RESPONSE_INTERRUPTED');
    } finally { reader.releaseLock(); }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    let body;
    try { body = JSON.parse(new TextDecoder().decode(bytes)); }
    catch { throw new IntegrationError('MODEL_RESPONSE_INVALID_JSON'); }
    const choice = body?.choices?.[0];
    if (body?.choices?.length !== 1 || !['stop', 'tool_calls'].includes(choice?.finish_reason)) {
      throw new IntegrationError('MODEL_OUTPUT_INCOMPLETE');
    }
    const message = choice.message;
    if (message?.role !== 'assistant' || typeof message?.reasoning_content !== 'string') {
      throw new IntegrationError('MODEL_REASONING_PROTOCOL_INVALID');
    }
    const calls = message.tool_calls ?? [];
    if (!Array.isArray(calls) || (choice.finish_reason === 'tool_calls') !== (calls.length > 0)) {
      throw new IntegrationError('MODEL_TOOL_PROTOCOL_INVALID');
    }
    // Response is recreated without upstream headers that could misstate length/encoding.
    return new Response(bytes, { status: 200, headers: { 'Content-Type': 'application/json' } });
  };
  return createDeepSeek({ apiKey, baseURL: API_ORIGIN, fetch: guardedFetch })(MODEL_ID);
}

/** Account model listing is not proof that generation or tool calling works. */
export async function verifyModelListed(apiKey: string, requestFetch = globalThis.fetch) {
  const response = await requestFetch(`${API_ORIGIN}/models`, {
    headers: { Authorization: `Bearer ${requireKey(apiKey)}` },
    redirect: 'error', signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) { await response.body?.cancel(); throw new IntegrationError(`MODEL_LIST_HTTP_${response.status}`); }
  const body = await response.json();
  const match = Array.isArray(body.data)
    ? body.data.find((row: { id?: string }) => row.id === MODEL_ID) : undefined;
  if (!match) throw new IntegrationError('MODEL_NOT_LISTED');
  return { id: MODEL_ID, name: typeof match.name === 'string' ? match.name : null };
}
