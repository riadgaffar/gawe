// The hand-off card's model (M20): the same typed-first layout as the approval card, with
// model-written text collapsed behind a header that carries only its path, a label and its length.
import { display, freeTextHeader, type FreeTextSection, type TypedRow } from "./approval-view.ts";
import type { HandoffView } from "./api-types.ts";

export type HandoffCardModel = {
  policy: string | null;
  typed: TypedRow[];
  actionHash: string;
  freeText: FreeTextSection[];
};

export function handoffCardModel(handoff: HandoffView): HandoffCardModel {
  return {
    policy: policyLine(handoff.policy_outcome, handoff.reason),
    typed: handoff.typed_fields.map((field) => ({ path: field.path, display: display(field.value) })),
    actionHash: handoff.action_hash,
    freeText: handoff.free_text_fields.map((field) => ({
      path: field.path,
      header: freeTextHeader(field.label, field.length),
      content: display(field.content),
    })),
  };
}

// What policy decided, said so that it can't be read as a request to approve: nobody is asked.
export function policyLine(outcome: string | null, reason: string): string | null {
  if (!outcome) return null;
  const detail = reason ? ` (${reason})` : "";
  if (outcome === "needs_approval") {
    return `Policy would need an approval for this${detail}. No approval is asked: you decide whether to do it.`;
  }
  return `Policy: ${reason || outcome.replaceAll("_", " ")}.`;
}

// What the run view says about a hand-off step, without any argument values.
export function handoffSummary(mark: { role: string; marked_by: string | null; reference: string | null }, status: string): string {
  if (status === "done_by_person") {
    return `Done by ${mark.marked_by ?? "?"}${mark.reference ? ` (reference ${mark.reference})` : ""}`;
  }
  if (status === "not_done_by_person") return `Not done, said ${mark.marked_by ?? "?"}`;
  return `Waiting for someone with ${mark.role} to do it by hand`;
}
