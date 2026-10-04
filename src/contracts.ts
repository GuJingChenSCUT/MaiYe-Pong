import { z } from 'zod';
import { IntegrationError } from './deepseek.ts';

const minor = z.number().int().min(0).max(Number.MAX_SAFE_INTEGER);
export const quoteSchema = z.strictObject({
  quoteId: z.string().min(1).max(120),
  merchantId: z.string().min(1).max(120),
  sku: z.string().min(1).max(200),
  quantity: z.number().int().positive().max(100),
  currency: z.literal('HKD'),
  destination: z.literal('HK'),
  itemMinor: minor,
  shippingMinor: minor.nullable(),
  discountMinor: minor,
  discountEligible: z.boolean().nullable(),
  expiresAt: z.number().int().positive(),
  evidenceRefs: z.array(z.string().min(1).max(200)).min(1).max(20),
  dataMode: z.enum(['synthetic', 'merchant_verified']),
});
export type Quote = z.infer<typeof quoteSchema>;
export type Role = 'buyer_planner' | 'offer_analyst';
export type TrustedTask = Readonly<{
  principalId: string;
  taskId: string;
  role: Role;
  phase: 'research';
  sku: string;
  quantity: number;
  budgetMinor: number;
  allowedMerchantIds: readonly string[];
}>;

/** Calculation is authoritative for these inputs; their authenticity is the service's duty. */
export function payableMinor(input: unknown, task: TrustedTask, now = Date.now()): number {
  const quote = quoteSchema.parse(input);
  if (!task.allowedMerchantIds.includes(quote.merchantId)) throw new IntegrationError('MERCHANT_OUT_OF_SCOPE');
  if (quote.sku !== task.sku || quote.quantity !== task.quantity) throw new IntegrationError('SPECIFICATION_MISMATCH');
  if (quote.expiresAt <= now) throw new IntegrationError('QUOTE_EXPIRED');
  if (quote.shippingMinor === null) throw new IntegrationError('SHIPPING_UNKNOWN');
  if (quote.discountMinor > 0 && quote.discountEligible === null) throw new IntegrationError('ELIGIBILITY_UNKNOWN');
  const total = quote.itemMinor + quote.shippingMinor - (quote.discountEligible ? quote.discountMinor : 0);
  if (!Number.isSafeInteger(total) || total < 0) throw new IntegrationError('AMOUNT_INVALID');
  if (!Number.isSafeInteger(task.budgetMinor) || task.budgetMinor < 0) throw new IntegrationError('BUDGET_INVALID');
  if (total > task.budgetMinor) throw new IntegrationError('BUDGET_EXCEEDED');
  return total;
}

export interface QuoteService {
  // Implementations must verify principal/task/resource ownership, not only IDs.
  list(task: TrustedTask): Promise<unknown>;
  refresh(task: TrustedTask, quoteId: string): Promise<unknown>;
  assertActive(task: TrustedTask): Promise<void>;
}
