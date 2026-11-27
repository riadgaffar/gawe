"""A pre-built run whose audit trail was altered (M29), for the demo's tamper scene.

Under memory stores there is no database to edit, so the tamper scene shows a run built for it:
an ordinary run made in scratch stores, with one event's actor changed on a copy before the
events are stored, each once, in order. The chain still links, so it is accepted, and
verification reports it broken at that event, as it would for an edit made later. It is labelled
as a fixture everywhere the demo names it.

The run assesses C-1004, which no scene uses, in scratch mock systems of its own, so it changes
neither the demo's contract system nor any rating history a scene reads.
"""

import tempfile
from dataclasses import dataclass
from pathlib import Path

from gawe.api.auth import Identity
from gawe.api.background import InlineBackground
from gawe.api.wiring import Stores, assemble, memory_stores
from gawe.apps import contract_review
from gawe.core.models import AuditEvent
from gawe.core.policy.signing import PolicySigner
from gawe.core.records import StepRecord
from gawe.core.runs import RunRecord
from gawe.demo.companies import BY_KEY, settings
from gawe.llm.fake import FakeProvider

TAMPERED_SEQ = 3
TAMPERED_ACTOR = "mallory"
OBJECTIVE = "Review contract C-1004 and record the risk assessment. (Pre-built tamper fixture.)"
DOCUMENT = "Analytics services agreement between Acme Corp and Umbrella Analytics Ltd."
FINDINGS = {
    "parties": ["Acme Corp", "Umbrella Analytics Ltd"],
    "risk_level": "high",
    "flagged_clauses": [{"kind": "one_sided_liability", "severity": "high"}],
    "summary": "One-sided analytics agreement.",
}
PLAN = {
    "steps": [
        {"id": "get", "tool": "clm.get_contract", "args": {"contract_id": "C-1004"}},
        {
            "id": "assess",
            "tool": "crm.write_assessment",
            "args": {"contract_id": "C-1004", "risk_level": "high", "summary": FINDINGS["summary"]},
            "depends_on": ["get"],
        },
    ]
}


@dataclass(frozen=True)
class Fixture:
    run: RunRecord
    steps: tuple[StepRecord, ...]
    events: tuple[AuditEvent, ...]  # as stored: event TAMPERED_SEQ altered

    @property
    def run_id(self) -> str:
        return self.run.run_id


def build(signer: PolicySigner) -> Fixture:
    """Make the run in scratch stores and return it with its altered events."""
    company = BY_KEY["careful"]
    alice = company.person("case_manager")
    who = Identity(company.tenant_id, alice.user_id, frozenset(alice.roles))
    scratch = memory_stores()
    with tempfile.TemporaryDirectory(prefix="gawe-demo-fixture-") as tmp:
        service = assemble(
            contract_review.bundle(Path(tmp), settings=settings()),
            stores=scratch,
            signer=signer,
            provider=FakeProvider([FINDINGS, PLAN]),
            background=InlineBackground(),
        )
        started = service.start_run(
            who, objective=OBJECTIVE, profile="contract_review", document=DOCUMENT
        )
    run = scratch.runs.get(started.run_id)
    if run.status.value != "completed":
        raise RuntimeError(f"the tamper fixture's run ended {run.status.value}")
    steps = tuple(scratch.steps.get(run.run_id, s.id) for s in (run.plan.steps if run.plan else ()))
    events = tuple(
        e.model_copy(update={"actor": TAMPERED_ACTOR}) if e.seq == TAMPERED_SEQ else e
        for e in scratch.audit.events(run.run_id)
    )
    return Fixture(run, steps, events)


def install(stores: Stores, fixture: Fixture) -> None:
    """Store the fixture's run, steps and events, each written once."""
    stores.runs.add(fixture.run)
    for step in fixture.steps:
        stores.steps.add(step)
    for event in fixture.events:
        stores.audit.append(event)
