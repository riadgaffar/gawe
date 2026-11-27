import { render, screen, within } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { ApprovalCard } from "../app/approvals/ApprovalCard";
import { ModelModeLabel } from "../components/ModelModeLabel";
import { demoGate } from "../lib/demo";
import type { DemoInfo } from "../lib/api-types";
import { approval } from "./render-fixtures";

// Only the server actions and the server-side gate are replaced; pages and components are real.
vi.mock("@/app/actions", () => ({ decide: vi.fn() }));
vi.mock("@/app/demo/actions", () => ({ demoSignIn: vi.fn(), demoStartScene: vi.fn() }));
vi.mock("../app/demo/actions", () => ({ demoSignIn: vi.fn(), demoStartScene: vi.fn() }));
vi.mock("@/lib/demo", () => ({ demoGate: vi.fn() }));
vi.mock("../lib/demo", () => ({ demoGate: vi.fn() }));

const INFO: DemoInfo = {
  model: "gawe-demo-scripted",
  companies: ["careful", "big-deals"].map((key) => ({
    key,
    name: key === "careful" ? "Careful Co." : "Big-Deals Co.",
    tenant_id: `t-${key}`,
    summary: key === "careful" ? "Every low rating needs legal." : "Legal only above 250,000.",
    people: [
      { key: `${key}-alice`, name: "Alice", title: "case manager", roles: ["case_manager"] },
      { key: `${key}-bob`, name: "Bob", title: "legal reviewer", roles: ["legal_reviewer"] },
      { key: `${key}-fiona`, name: "Fiona", title: "finance approver", roles: ["finance_approver"] },
    ],
  })),
  scenes: [
    {
      scene_id: "c1003-review",
      title: "A contract with hidden instructions, rated",
      objective: "Review contract C-1003 and record the risk assessment.",
      document: "C-1003_initech_sow_injection.md",
      modes: ["honest", "fooled"],
      fooled_how: "the Reader falls for the hidden text and rates the contract low",
      expect: "Fooled: it waits for legal at Careful and goes through at Big-Deals.",
    },
    {
      scene_id: "c1001-review",
      title: "A fair contract, rated low",
      objective: "Review contract C-1001 and record the risk assessment.",
      document: "C-1001_northwind_msa.md",
      modes: ["honest"],
      fooled_how: null,
      expect: "At Careful the low rating waits for legal.",
    },
  ],
  fixture: { company: "careful", run_id: "fixture/1" },
};

beforeEach(() => {
  vi.mocked(demoGate).mockReset();
});

test("the label says a run was scripted, and is absent for a real model's run", () => {
  const { rerender } = render(<ModelModeLabel mode="fooled" />);
  expect(screen.getByRole("note")).toHaveTextContent("Scripted demo model: fooled.");
  rerender(<ModelModeLabel mode={null} />);
  expect(screen.queryByRole("note")).not.toBeInTheDocument();
});

test("an approval card for a scripted run says so before the decision", () => {
  render(<ApprovalCard approval={approval({ model_mode: "honest" })} />);
  expect(screen.getByRole("note")).toHaveTextContent("Scripted demo model: honest.");
  render(<ApprovalCard approval={approval({ model_mode: null })} />);
  expect(screen.getAllByRole("note")).toHaveLength(1);
});

test("the sign-in page lists every person by company", async () => {
  vi.mocked(demoGate).mockResolvedValue(INFO);
  const { default: DemoPeoplePage } = await import("../app/demo/people/page");
  render(await DemoPeoplePage());
  for (const company of ["Careful Co.", "Big-Deals Co."]) {
    const section = screen.getByRole("heading", { name: company }).closest("section")!;
    const people = within(section).getAllByRole("button").map((b) => b.textContent);
    expect(people).toEqual(["Alice, case manager", "Bob, legal reviewer", "Fiona, finance approver"]);
  }
  expect(document.body.textContent).not.toMatch(/token/i);
});

test("the Demo page offers each scene at each company, fooled only where the scene has it", async () => {
  vi.mocked(demoGate).mockResolvedValue(INFO);
  const { default: DemoPage } = await import("../app/demo/page");
  render(await DemoPage());
  const fooledScene = screen.getByRole("heading", { name: INFO.scenes[0]!.title }).closest("section")!;
  expect(within(fooledScene).getAllByRole("button", { name: "Start (fooled model)" })).toHaveLength(2);
  expect(within(fooledScene).getByText(/Fooled: it waits for legal at Careful/)).toBeVisible();
  const honestOnly = screen.getByRole("heading", { name: INFO.scenes[1]!.title }).closest("section")!;
  expect(within(honestOnly).queryByRole("button", { name: "Start (fooled model)" })).not.toBeInTheDocument();
  expect(within(honestOnly).getAllByRole("button", { name: "Decide as Bob (legal)" })).toHaveLength(2);
  const tamper = screen.getByRole("heading", { name: "A tampered audit trail" }).closest("section")!;
  expect(within(tamper).getByText(/pre-built fixture/)).toBeVisible();
  expect(screen.getByText(/a decision\s+with a cost/)).toBeVisible();
});

test("without the gate, both demo pages are not found", async () => {
  vi.mocked(demoGate).mockResolvedValue(null);
  const { default: DemoPage } = await import("../app/demo/page");
  const { default: DemoPeoplePage } = await import("../app/demo/people/page");
  await expect(DemoPage()).rejects.toThrow();
  await expect(DemoPeoplePage()).rejects.toThrow();
});
