import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";
import { RecordEvidenceCard } from "../components/RecordEvidenceCard";
import { recordCardModel } from "../lib/record-evidence";
import type { RecordEvidenceView } from "../lib/api-types";
import { evidence, HASH, PROSE, target } from "./render-fixtures";

function show(view: RecordEvidenceView | null = evidence()) {
  return render(<RecordEvidenceCard card={recordCardModel(view, HASH, true)} />);
}
function cells(name: string) {
  const row = screen.getByRole("rowheader", { name }).closest("tr")!;
  return within(row).getAllByRole("cell").map(n => n.textContent);
}

test("renders changed values with accessible headers, guard timing and no action controls", () => {
  const { container } = show();
  expect(screen.getByRole("region", { name: "Why the record check refused this action" })).toBeVisible();
  expect(screen.getAllByRole("columnheader").map(n => n.textContent)).toEqual(["Field", "Captured value", "Observed value", "Comparison"]);
  for (const header of screen.getAllByRole("columnheader")) expect(header).toHaveAttribute("scope", "col");
  expect(screen.getByRole("rowheader", { name: "deal_value" })).toHaveAttribute("scope", "row");
  expect(cells("deal_value")).toEqual(["45000", "150000", "Changed"]);
  expect(screen.getByText(/Observed by the atomic guard at 2026-11-25T13:00:00Z/)).toBeVisible();
  expect(screen.getByText(/will not be retried or sent for another approval/)).toBeVisible();
  expect(container.querySelector("button, form, input, a")).toBeNull();
});

for (const timing of ["at_guard", "after_refusal"] as const) {
  test(`matching fields at ${timing} still refuse, with honest timing`, () => {
    const record = target({ state: "unchanged" });
    record.observation!.timing = timing;
    record.observation!.fields[0]!.value = 45000;
    record.rows[0]!.observed.value = 45000;
    record.rows[0]!.changed = false;
    show(evidence(record));
    expect(cells("deal_value")).toEqual(["45000", "45000", "Unchanged"]);
    expect(screen.getByText(/The action remains refused/)).toBeVisible();
    if (timing === "after_refusal") {
      expect(screen.getByText(/Read after refusal.*not the historical guard state/)).toBeVisible();
      expect(screen.getByText(/Their values at refusal are not established/)).toBeVisible();
      expect(screen.queryByText(/Observed by the atomic guard/)).not.toBeInTheDocument();
    } else {
      expect(screen.getByText(/revision changed, but the selected fields did not/)).toBeVisible();
    }
  });
}

for (const [state, notice, values] of [
  ["no_fields_tracked", /No record fields are tracked/, null],
  ["snapshot_unavailable", /captured snapshot is unavailable/, ["Unavailable", "150000", "Not compared"]],
  ["observation_unavailable", /does not mean the record was unchanged/, ["45000", "Unavailable", "Not compared"]],
  ["record_missing", /missing or outside the permitted scope/, ["45000", "Record missing", "Not compared"]],
] as const) {
  test(`renders ${state} without inventing a comparison`, () => {
    const record = target({ state, rows: [] });
    if (state === "no_fields_tracked") { record.snapshot!.fields = []; record.observation!.fields = []; }
    if (state === "snapshot_unavailable") record.snapshot = null;
    if (state === "observation_unavailable") record.observation = null;
    if (state === "record_missing") { record.observation!.status = "missing"; record.observation!.revision = null; record.observation!.fields = []; }
    const { container } = show(evidence(record, { coverage: state === "observation_unavailable" ? "unavailable" : "complete" }));
    expect(screen.getByText(notice)).toBeVisible();
    if (values) expect(cells("deal_value")).toEqual(values);
    else expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(container.querySelector("button, form, input")).toBeNull();
  });
}

test("partial batches retain the unobserved target in order and show unavailable cells", () => {
  const second = target({ subject: "C-2", observation: null, state: "observation_unavailable", rows: [] });
  second.snapshot!.subject = "C-2";
  show(evidence(target(), { code: "event_changed", coverage: "partial", records: [target(), second] }));
  expect(screen.getByText(/Only part of the batch was observed/)).toBeVisible();
  expect(screen.getAllByRole("heading", { level: 4 }).map(n => n.textContent)).toEqual(["C-1 (clm.contract)", "C-2 (clm.contract)"]);
  const secondSection = screen.getByRole("heading", { name: "C-2 (clm.contract)" }).parentElement!;
  expect(within(secondSection).getByText("No saved observation is available for this target.")).toBeVisible();
  expect(within(secondSection).getAllByRole("cell").map(n => n.textContent)).toEqual(["45000", "Unavailable", "Not compared"]);
});

test("missing, present null, string null and unavailable have distinct rendered cells", () => {
  const record = target({ observation: null, state: "observation_unavailable", rows: [] });
  record.snapshot!.fields = [
    { path: "missing", present: false, value: null },
    { path: "null", present: true, value: null },
    { path: "string", present: true, value: "null" },
  ];
  show(evidence(record, { coverage: "unavailable" }));
  expect(cells("missing")).toEqual(["Missing", "Unavailable", "Not compared"]);
  expect(cells("null")).toEqual(["null", "Unavailable", "Not compared"]);
  expect(cells("string")).toEqual(['"null"', "Unavailable", "Not compared"]);
});

test("unreadable observations keep their later-read timing and do not become missing", () => {
  const record = target({ state: "observation_unavailable", rows: [] });
  Object.assign(record.observation!, { status: "unavailable", reason: "unreadable", timing: "after_refusal", revision: null, fields: [] });
  show(evidence(record));
  expect(cells("deal_value")).toEqual(["45000", "Unavailable", "Not compared"]);
  expect(screen.getByText(/Read after refusal/)).toBeVisible();
  expect(screen.queryByText("Record missing")).not.toBeInTheDocument();
});

for (const mode of ["absent", "mismatched"] as const) {
  test(`${mode} evidence shows an unavailable notice with no trusted-looking rows`, () => {
    show(mode === "absent" ? null : evidence(target(), { action_hash: "wrong" }));
    expect(screen.getByText("Saved record evidence is unavailable.")).toBeVisible();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText(/selected fields did not/)).not.toBeInTheDocument();
  });
}

test("untrusted subject, field and revision strings remain text even after expanding provenance", async () => {
  const record = target({ subject: PROSE, guard_revision: "<script>alert(2)</script>", rows: [] });
  record.snapshot!.subject = PROSE;
  record.observation!.subject = PROSE;
  record.snapshot!.fields = [{ path: "<iframe>field</iframe>", present: true, value: PROSE }];
  record.observation!.fields = [];
  const { container } = show(evidence(record));
  expect(screen.getByRole("heading", { level: 4 })).toHaveTextContent(PROSE);
  expect(screen.getByRole("rowheader", { name: "<iframe>field</iframe>" })).toBeVisible();
  expect(cells("<iframe>field</iframe>")[0]).toBe(JSON.stringify(PROSE));
  await userEvent.setup().click(screen.getByText("Record revisions and provenance"));
  expect(screen.getByText("<script>alert(2)</script>")).toBeVisible();
  expect(container.querySelector("img, script, iframe, button, form, input")).toBeNull();
});

test("no record card is rendered for an action without record evidence", () => {
  const { container } = render(<RecordEvidenceCard card={null} />);
  expect(container).toBeEmptyDOMElement();
});
