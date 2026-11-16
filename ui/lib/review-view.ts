// Plain-language labels for next-day review items. Pure, so it runs under the Node test runner.
import type { ReviewItemView } from "./api-types.ts";

const REASONS: Record<string, string> = {
  audit_mismatch: "Stored decision and audit trail disagree",
  superseded: "Decision replaced by a recipient refresh",
  signature_invalid: "Decision signature does not verify",
  action_mismatch: "Stored arguments no longer match the decided action",
  rules_changed: "Today's rules decide differently (rule set changed)",
  rules_may_have_changed: "Today's rules decide differently (decision predates rule digests)",
  inputs_changed: "Today's rules decide differently (a value they read changed)",
  sampled_low: "Sampled: automatic low rating, nothing flagged",
};

export function reasonLabel(reason: string): string {
  return REASONS[reason] ?? reason.replaceAll("_", " ");
}

export function isSample(item: ReviewItemView): boolean {
  return item.reason === "sampled_low";
}
