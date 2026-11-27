import { act, render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { ApprovalCard } from "../app/approvals/ApprovalCard";
import { decide, type ActionState } from "../app/actions";
import { approval, HASH, LABEL, PROSE, RAW } from "./render-fixtures";

// Only the Next server action is replaced. React, form hooks, child components and Link are real.
vi.mock("@/app/actions", () => ({ decide: vi.fn() }));
beforeEach(() => {
  vi.mocked(decide).mockReset();
});

function precedes(a: Node, b: Node) {
  expect(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
}

test("renders typed facts, saved fields, history, hash and differences before collapsed prose", async () => {
  const user = userEvent.setup();
  const { container } = render(<ApprovalCard approval={approval()} />);
  const typed = screen.getByText("Exactly what will run").parentElement!;
  expect([...typed.querySelectorAll("dt")].map(n => n.textContent)).toEqual(["contract_id", "risk_level"]);
  expect(within(typed).getByText("C-1")).toBeVisible();
  expect(within(typed).getByText("low")).toBeVisible();
  const capture = screen.getByRole("region", { name: "Captured record fields" });
  expect(within(capture).getByRole("cell", { name: "45000" })).toBeVisible();
  expect(screen.queryByRole("columnheader", { name: "Observed value" })).not.toBeInTheDocument();
  const history = screen.getByText("Earlier actions on the same subject");
  expect(screen.getByText(/risk_level high · run started 2026-11-24/)).toBeVisible();
  expect(screen.getAllByRole("link", { name: "run" }).map(n => n.getAttribute("href"))).toEqual(["/runs/run%2F1", "/runs/previous%2F1"]);
  const hash = screen.getByText(HASH, { selector: "pre" });
  expect(hash).toBeVisible();
  const difference = screen.getByText("Compared with the last approved call");
  expect(screen.getByText("risk_level: high → low")).toBeVisible();
  expect(screen.getByText("summary: changed (model-written text, not shown)")).toBeVisible();
  expect(screen.getByText("snapshots: changed (captured record evidence; see captured fields)")).toBeVisible();
  const prose = screen.getByText(PROSE, { selector: "pre" });
  const details = prose.closest("details")!;
  const summary = details.querySelector("summary")!;
  expect(details.open).toBe(false);
  expect(prose).not.toBeVisible();
  expect(summary.textContent).toBe(`summary ${LABEL} · ${[...PROSE].length} characters`);
  expect(summary).not.toHaveTextContent("Approve this vendor");
  [typed, capture, history, hash, difference, details].slice(1).forEach((node, i) =>
    precedes([typed, capture, history, hash, difference][i]!, node));
  expect(container).not.toHaveTextContent(RAW);
  expect(container.querySelector("img, script, iframe")).toBeNull();
  await user.click(summary);
  expect(details.open).toBe(true);
  expect(prose).toBeVisible();
  expect(container.querySelector("img, script, iframe")).toBeNull();
  await user.click(summary);
  expect(prose).not.toBeVisible();
});

test("renders replay events as a table in the supplied order", () => {
  render(<ApprovalCard approval={approval({ tool: "billing.replay_events", record_evidence: null,
    typed_fields: [{ path: "batch", value: [{ event_id: "ev-2", amount: 340 }, { event_id: "ev-1", amount: 120 }] }],
  })} />);
  const table = screen.getByRole("table");
  expect(within(table).getAllByRole("columnheader").map(n => n.textContent)).toEqual(["event_id", "amount"]);
  expect(within(table).getAllByRole("row").slice(1).map(n => within(n).getAllByRole("cell").map(c => c.textContent))).toEqual([["ev-2", "340"], ["ev-1", "120"]]);
});

test("restore card explains lasting effects and preserves original capture provenance", async () => {
  const value = approval({ tool: "crm.restore_assessment" });
  const snapshot = value.record_evidence!.records[0]!.snapshot!;
  snapshot.basis = "confirmed_original";
  snapshot.revision = "original-v1";
  snapshot.source_action_hash = "b".repeat(64);
  render(<ApprovalCard approval={value} />);
  expect(screen.getByText(/Later actions and their effects stay as they are/)).toBeVisible();
  expect(screen.getByText(/Captured for the original confirmed action at/)).toBeVisible();
  await userEvent.setup().click(screen.getByText("Record revisions and provenance"));
  for (const text of ["Required revision for this action", "guard-v2", "Captured revision", "original-v1", "Original action hash", "b".repeat(64)]) {
    expect(screen.getByText(text)).toBeVisible();
  }
});

test("old responses with expected but absent evidence show an unavailable notice", () => {
  render(<ApprovalCard approval={approval({ record_evidence: undefined })} />);
  expect(screen.getByText("Saved record evidence is unavailable.")).toBeVisible();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  expect(document.body).not.toHaveTextContent(RAW);
});

for (const [label, decision, message] of [["Approve", "approved", "Approved."], ["Reject", "rejected", "Rejected."]] as const) {
  test(`${label} sends the displayed binding and comment, disables pending buttons and finishes once`, async () => {
    const user = userEvent.setup();
    let finish!: (state: ActionState) => void;
    vi.mocked(decide).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    render(<ApprovalCard approval={approval()} />);
    await user.type(screen.getByRole("textbox", { name: "Comment (optional)" }), "Reviewed evidence");
    await user.click(screen.getByRole("button", { name: label }));
    await waitFor(() => expect(decide).toHaveBeenCalledTimes(1));
    const [previous, form] = vi.mocked(decide).mock.calls[0]!;
    expect(previous).toBeNull();
    expect(Object.fromEntries(form.entries())).toEqual({ approval_id: "approval-1", action_hash: screen.getByText(HASH, { selector: "pre" }).textContent, decision, comment: "Reviewed evidence" });
    const pending = screen.getAllByRole("button", { name: "Working…" });
    expect(pending).toHaveLength(2);
    for (const button of pending) expect(button).toBeDisabled();
    await user.click(pending[0]!);
    expect(decide).toHaveBeenCalledTimes(1);
    await act(async () => finish({ ok: message }));
    expect(await screen.findByRole("status")).toHaveTextContent(message);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Comment (optional)" })).toBeDisabled();
  });
}

test("action errors are shown as text and leave the decision controls available", async () => {
  vi.mocked(decide).mockResolvedValue({ error: PROSE });
  const { container } = render(<ApprovalCard approval={approval()} />);
  await userEvent.setup().click(screen.getByRole("button", { name: "Approve" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(PROSE);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
  expect(container.querySelector("img, script, iframe")).toBeNull();
});
