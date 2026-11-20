"""Independent target observations and durable recovery, without a model or network."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.flow import Clock, Crash
from tests.helpers import signer

from gawe.api.auth import Identity
from gawe.api.background import InlineBackground
from gawe.api.errors import NotFoundError
from gawe.api.wiring import assemble, load_app_rules, memory_stores
from gawe.apps import contract_review, it_access
from gawe.connectors.base import Ledger
from gawe.connectors.mock_clm import MockCLM
from gawe.connectors.mock_email import MockEmail
from gawe.connectors.mock_esign import MockESign
from gawe.connectors.mock_iam import MockIAM
from gawe.core.approvals import Principal
from gawe.core.executor import ToolCall, ToolTransientError
from gawe.core.maintenance import JobConflictError
from gawe.core.statemachine import RunStatus, StepStatus
from gawe.llm.fake import FakeProvider

ALICE = Identity("t1", "alice", frozenset({"case_manager", "it_agent"}))
BOB = Identity("t1", "bob", frozenset({"legal_reviewer", "security_admin"}))
# Not "low": since M17 every "low" waits for legal, and these tests need an automatic write.
CRM = {"contract_id": "C-1001", "risk_level": "medium", "summary": "Private review text"}
IAM = {"email": "dana.ortiz@acme.example", "system": "wiki", "level": "read"}
MAIL = {"to": "legal@acme.example", "subject": "Private subject", "body": "Private body"}
SIGN = {"contract_id": "C-1001", "signer_email": "vendor@outside.example", "deal_value": 45000}
ARGS = {"crm": CRM, "iam": IAM, "mail": MAIL, "esign": SIGN}
TOOLS = {
    "crm": "crm.write_assessment",
    "iam": "iam.grant_access",
    "mail": "email.send",
    "esign": "esign.send_for_signature",
}


class Env:
    def __init__(
        self,
        path: Path,
        system: str = "crm",
        *,
        downstream: bool = False,
        args: Mapping[str, Any] | None = None,
        steps: list[dict[str, Any]] | None = None,
        plan_attempts: int = 1,
    ) -> None:
        self.clock = Clock()
        self.stores = memory_stores()
        ledger = Ledger(path / "ledger.jsonl")
        self.ledger_dir = path
        self.clm = MockCLM.from_seed(contract_review.HERE / "samples/clm_seed.json", ledger)
        self.esign = MockESign(ledger, self.clm)
        self.mail = MockEmail(ledger)
        self.iam = MockIAM.from_seed(it_access.HERE / "samples/iam_seed.json", ledger)
        if system == "iam":
            bundle = replace(
                it_access.bundle(path, iam=self.iam, clock=self.clock),
                variables=lambda run: {
                    "tenant": {
                        "internal_domains": ["acme.example"],
                        "max_grant_lifetime_hours": 720,
                    },
                    "request": {
                        "reported_requester": IAM["email"],
                        "submitters": [IAM["email"]],
                        "portal_submitters": [IAM["email"]],
                    },
                },
            )
        else:
            bundle = contract_review.bundle(path, email=self.mail, clm=self.clm)
            from gawe.apps.contract_review.tools import implementations

            bundle = replace(
                bundle,
                tools=implementations(self.clm, self.mail, self.esign),
                readbacks=contract_review.readbacks(self.clm, self.esign, self.mail),
            )
        self.system = system
        self.calls: list[ToolCall] = []
        write = bundle.tools[TOOLS[system]]

        def counted(args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
            self.calls.append(call)
            return write(args, call)

        bundle = replace(bundle, tools={**bundle.tools, TOOLS[system]: counted})
        planned = [
            {
                "id": "write",
                "tool": TOOLS[system],
                "args": dict(args or ARGS[system]),
                "depends_on": [],
            }
        ]
        if downstream:
            planned.append(
                {
                    "id": "next",
                    "tool": "clm.get_contract",
                    "args": {"contract_id": "C-1001"},
                    "depends_on": ["write"],
                }
            )
        self.steps = steps if steps is not None else planned
        self.bundle = bundle
        self.service = assemble(
            bundle,
            stores=self.stores,
            signer=signer(),
            provider=FakeProvider([{"steps": self.steps}] * plan_attempts),
            background=InlineBackground(),
            clock=self.clock,
        )
        self.orch = self.service._orch
        self.run_id = "run-1"
        profile = next(iter(bundle.profiles))
        source_id = None
        if profile in bundle.sources:  # IT access: the grantee filed the ticket via the portal
            source_id = "T-1"
            bundle.sources[profile].create(
                ALICE.tenant_id, source_id, submitted_by=IAM["email"], channel="portal", body="t"
            )
        self.orch.start_run(
            tenant_id=ALICE.tenant_id,
            acting_user_id=ALICE.user_id,
            roles=tuple(ALICE.roles),
            objective="Test the write",
            profile=profile,
            run_id=self.run_id,
            source_id=source_id,
        )

    @property
    def target(self) -> Any:
        return {
            "crm": self.clm.operations,
            "iam": self.iam.operations,
            "mail": self.mail.server,
            "esign": self.esign.operations,
        }[self.system]

    @property
    def job(self) -> Any:
        jobs = self.stores.maintenance.for_run(ALICE.tenant_id, self.run_id)
        return [j for j in jobs if j.kind == "write_readback"][-1]

    def advance(self) -> RunStatus:
        return self.orch.advance(self.run_id).status

    def approve(self) -> None:
        (item,) = self.service.inbox(BOB)
        self.service.decide(
            BOB, item.approval_id, decision="approved", action_hash=item.action_hash
        )

    def execute(self) -> RunStatus:
        self.advance()
        if self.service.inbox(BOB):
            self.approve()
        return self.stores.runs.get(self.run_id).status


@pytest.mark.parametrize("system", ["crm", "iam", "mail", "esign"])
def test_each_shipped_write_uses_independent_record(tmp_path: Path, system: str) -> None:
    e = Env(tmp_path, system)
    assert e.execute() is RunStatus.COMPLETED
    assert len(e.calls) == 1 and e.job.result["outcome"] == "agreement"
    assert e.job.operation_key == e.calls[0].idempotency_key
    assert e.job.deadline - e.job.created_at == timedelta(minutes=5)
    assert e.job.continued


@pytest.mark.parametrize(
    "system,change",
    [
        ("crm", {"contract_id": "C-1002"}),
        ("crm", {"risk_level": "high"}),
        ("crm", {"summary": "Different review"}),
        ("iam", {"email": "someone@acme.example"}),
        ("iam", {"system": "prod_db"}),
        ("iam", {"level": "admin"}),
        ("esign", {"contract_id": "C-1002"}),
        ("esign", {"signer_email": "wrong@outside.example"}),
        ("esign", {"deal_value": 1}),
        # M16b: the bound revision and the grant's end date are compared too.
        ("crm", {"record": {"contract_version": "another"}}),
        ("esign", {"record": {"contract_version": "another"}}),
        ("iam", {"record": {"user_version": "another"}}),
        ("iam", {"expires_at": "2099-01-01T00:00:00Z"}),
        ("iam", {"lifetime_hours": 720}),
    ],
)
def test_echoing_reply_cannot_hide_wrong_applied_fields(
    tmp_path: Path, system: str, change: dict[str, Any]
) -> None:
    e = Env(tmp_path, system)
    e.target.applied_override = change
    assert e.execute() is RunStatus.FAILED
    assert e.stores.steps.get(e.run_id, "write").status is StepStatus.VERIFICATION_BLOCKED
    assert e.job.result["outcome"] == "disagreement" and len(e.calls) == 1


def test_extra_mail_recipient_is_detected_after_acceptance(tmp_path: Path) -> None:
    e = Env(tmp_path, "mail", downstream=True)
    e.target.accepted_override = ("legal@acme.example", "leak@outside.example")
    assert e.execute() is RunStatus.FAILED
    assert len(e.target.records()) == 1  # mismatch detection is not prevention/undo
    assert e.stores.steps.get(e.run_id, "next").status is StepStatus.FAILED
    assert e.job.evidence["observed"]["recipients"] != e.job.evidence["expected"]["recipients"]


@pytest.mark.parametrize("system", ["crm", "iam", "mail", "esign"])
@pytest.mark.parametrize("outcome", ["agreement", "disagreement", "unknown"])
def test_delayed_reads_resolve_once_without_resend(
    tmp_path: Path, system: str, outcome: str
) -> None:
    e = Env(tmp_path, system)
    e.target.visible_at = e.clock.now + timedelta(seconds=60 if outcome != "unknown" else 600)
    if outcome == "disagreement":
        if system == "mail":
            e.target.accepted_override = ("wrong@outside.example",)
        else:
            e.target.applied_override = {next(iter(ARGS[system])): "wrong"}
    assert e.execute() is RunStatus.WAITING_VERIFICATION
    original_deadline = e.job.deadline
    assert e.advance() is RunStatus.WAITING_VERIFICATION
    assert len(e.calls) == 1
    e.clock.advance(seconds=60 if outcome != "unknown" else 300)
    expected = {
        "agreement": RunStatus.COMPLETED,
        "disagreement": RunStatus.FAILED,
        "unknown": RunStatus.UNKNOWN,
    }[outcome]
    assert e.advance() is expected
    assert e.job.result["outcome"] == outcome and e.job.deadline == original_deadline
    e.clock.advance(seconds=600)
    assert e.advance() is expected  # late visibility cannot rewrite a terminal outcome
    assert len(e.calls) == 1


@pytest.mark.parametrize("system", ["crm", "iam", "mail", "esign"])
def test_lost_reply_and_read_failure_never_repeat_write(tmp_path: Path, system: str) -> None:
    e = Env(tmp_path, system)
    if system == "mail":
        e.mail.drop_reply = True
    else:
        e.target.drop_reply = True
    e.target.read_failures = 1
    assert e.execute() is RunStatus.WAITING_VERIFICATION
    assert len(e.calls) == 1
    assert e.advance() is RunStatus.COMPLETED
    assert len(e.calls) == 1


def test_known_pre_dispatch_failures_retry_same_key(tmp_path: Path) -> None:
    e = Env(tmp_path)
    e.clm.faults.fail_next(2)
    e.orch._executor._sleep = lambda seconds: None
    assert e.execute() is RunStatus.COMPLETED
    assert len(e.calls) == 3
    assert len({c.idempotency_key for c in e.calls}) == 1
    assert len(e.target.records()) == 1


def test_uncertain_transient_error_is_reconciliation_only(tmp_path: Path) -> None:
    e = Env(tmp_path)

    def uncertain(args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
        e.calls.append(call)
        raise ToolTransientError("maybe accepted")

    e.orch._executor._tools[TOOLS["crm"]] = uncertain
    assert e.execute() is RunStatus.WAITING_VERIFICATION
    e.clock.advance(minutes=5)
    assert e.advance() is RunStatus.UNKNOWN and len(e.calls) == 1


@pytest.mark.parametrize(
    "reply", [{"assessment_id": None}, {"assessment_id": "someone-elses-record"}]
)
def test_malformed_or_wrong_record_reply_is_reconciled_but_not_released(
    tmp_path: Path, reply: dict[str, Any]
) -> None:
    e = Env(tmp_path, downstream=True)
    e.target.reply_override = reply
    assert e.execute() is RunStatus.FAILED
    assert e.job.result["outcome"] == "protocol_failure"
    assert e.job.evidence["code"] == "readback_agreed"
    assert e.stores.steps.get(e.run_id, "next").status is StepStatus.FAILED
    assert len(e.target.records()) == 1


def test_no_new_work_while_waiting_and_cancel_preserves_reconciliation(tmp_path: Path) -> None:
    e = Env(tmp_path, downstream=True)
    e.target.visible_at = e.clock.now + timedelta(seconds=60)
    assert e.execute() is RunStatus.WAITING_VERIFICATION
    assert e.stores.steps.get(e.run_id, "next").status is StepStatus.PENDING
    e.service.cancel_run(ALICE, e.run_id)
    assert e.stores.steps.get(e.run_id, "write").status is StepStatus.AWAITING_READBACK
    e.clock.advance(seconds=60)
    assert e.advance() is RunStatus.CANCELLED
    assert e.job.result["outcome"] == "agreement"
    assert e.stores.steps.get(e.run_id, "next").status is StepStatus.FAILED
    assert len(e.calls) == 1


def test_expired_approval_does_not_prevent_verification_or_allow_dispatch(tmp_path: Path) -> None:
    e = Env(tmp_path, "mail", args={**MAIL, "to": "vendor@outside.example"})
    e.target.visible_at = e.clock.now + timedelta(seconds=240)
    e.service._approvals._default_ttl = timedelta(seconds=30)
    assert e.advance() is RunStatus.PAUSED_FOR_APPROVAL
    e.approve()
    assert e.stores.runs.get(e.run_id).status is RunStatus.WAITING_VERIFICATION
    e.clock.advance(seconds=240)
    record = e.service._approval_store.for_step(e.run_id, "write")[0]
    assert e.clock.now >= record.expires_at
    assert e.advance() is RunStatus.COMPLETED and len(e.calls) == 1


def test_new_dispatch_after_expired_approval_is_refused(tmp_path: Path) -> None:
    e = Env(tmp_path, "mail", args={**MAIL, "to": "vendor@outside.example"})
    e.advance()
    (request,) = e.service.inbox(BOB)
    e.service._approvals.decide(
        request.approval_id,
        decider=Principal(BOB.user_id, BOB.roles),
        decision="approved",
        action_hash=request.action_hash,
    )
    e.clock.now = request.expires_at
    assert e.advance() is RunStatus.FAILED
    assert len(e.calls) == 0 and e.target.records() == []


@pytest.mark.parametrize(
    "boundary", ["intent", "attempt", "accept", "output", "result", "step", "audit", "continuation"]
)
def test_crash_boundaries_recover_without_dispatching_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    e = Env(tmp_path)
    resolver = e.orch._executor.verification
    assert resolver is not None
    if boundary == "intent":
        original = resolver.jobs.add

        def after_intent(*args: Any, **kwargs: Any) -> Any:
            original(*args, **kwargs)
            raise Crash()  # intent durable; cannot prove whether dispatch happened

        monkeypatch.setattr(resolver.jobs, "add", after_intent)
    elif boundary in {"attempt", "output"}:
        name = "start" if boundary == "attempt" else "save_output"
        original = getattr(e.stores.executions, name)

        def after_execution(*args: Any, **kwargs: Any) -> Any:
            original(*args, **kwargs)
            raise Crash()

        monkeypatch.setattr(e.stores.executions, name, after_execution)
    elif boundary == "accept":
        original = e.target.accept

        def after_accept(*args: Any, **kwargs: Any) -> Any:
            original(*args, **kwargs)
            raise Crash()

        monkeypatch.setattr(e.target, "accept", after_accept)
    elif boundary in {"result", "continuation"}:
        original = resolver.jobs.replace

        def after_job(job: Any, **kwargs: Any) -> Any:
            value = original(job, **kwargs)
            if (boundary == "result" and job.result) or (
                boundary == "continuation" and job.continued
            ):
                raise Crash()
            return value

        monkeypatch.setattr(resolver.jobs, "replace", after_job)
    elif boundary == "step":
        original = e.stores.steps.transition

        def after_step(*args: Any, **kwargs: Any) -> Any:
            value = original(*args, **kwargs)
            if kwargs.get("to") is StepStatus.SUCCEEDED:
                raise Crash()
            return value

        monkeypatch.setattr(e.stores.steps, "transition", after_step)
    else:
        original = resolver.audit.record

        def after_audit(*args: Any, **kwargs: Any) -> Any:
            value = original(*args, **kwargs)
            if args[1] == "verification.readback":
                raise Crash()
            return value

        monkeypatch.setattr(resolver.audit, "record", after_audit)
    with pytest.raises(Crash):
        e.advance()
    calls_at_crash = len(e.calls)
    monkeypatch.undo()
    e.clock.advance(seconds=31)
    if boundary in {"intent", "attempt"}:
        assert e.advance() is RunStatus.WAITING_VERIFICATION
        e.clock.advance(minutes=5)
        assert e.advance() is RunStatus.UNKNOWN
    else:
        assert e.advance() is RunStatus.COMPLETED
    assert len(e.calls) == calls_at_crash


def test_evidence_is_tenant_scoped_and_has_only_text_digests(tmp_path: Path) -> None:
    e = Env(tmp_path, "mail")
    assert e.execute() is RunStatus.COMPLETED
    evidence = e.service.verification_evidence(ALICE, e.run_id)
    assert len(evidence) == 1
    text = evidence[0].model_dump_json()
    assert MAIL["body"] not in text and MAIL["subject"] not in text
    with pytest.raises(NotFoundError):
        e.service.verification_evidence(Identity("other", "bob", frozenset()), e.run_id)
    assert e.stores.maintenance.get("other", e.job.job_id) is None


def test_missing_readback_adapter_blocks_assembly(tmp_path: Path) -> None:
    b = contract_review.bundle(tmp_path)
    with pytest.raises(ValueError, match="read-back adapters"):
        load_app_rules(replace(b, readbacks={}))


def test_operation_records_survive_recreation_and_ignore_ledger_edits(tmp_path: Path) -> None:
    e = Env(tmp_path)
    assert e.execute() is RunStatus.COMPLETED
    (tmp_path / "ledger.jsonl").write_text("")
    recreated = MockCLM.from_seed(
        contract_review.HERE / "samples/clm_seed.json", Ledger(tmp_path / "ledger.jsonl")
    )
    assert recreated.operations.read(e.job.operation_key, e.clock.now)["summary"] == CRM["summary"]
    assert recreated.operations.read("another-key", e.clock.now) is None


def test_recovery_rejects_modified_identity(tmp_path: Path) -> None:
    e = Env(tmp_path)
    e.target.visible_at = e.clock.now + timedelta(seconds=60)
    e.execute()
    step = e.stores.steps.get(e.run_id, "write")
    altered = step.model_copy(update={"resolved_args": {**CRM, "summary": "changed"}})
    resolver = e.orch._executor.verification
    assert resolver is not None
    with pytest.raises(JobConflictError, match="identity"):
        resolver._validate_identity(altered, e.job)


def test_pre_dispatch_retry_cannot_outlive_approval(tmp_path: Path) -> None:
    e = Env(tmp_path, "mail", args={**MAIL, "to": "vendor@outside.example"})
    e.mail.faults.fail_next(1)
    e.orch._executor._sleep = lambda seconds: e.clock.advance(hours=25)
    e.execute()
    assert e.stores.runs.get(e.run_id).status is RunStatus.FAILED
    assert len(e.calls) == 1 and e.target.records() == []
    assert e.job.result["outcome"] == "not_dispatched"


def test_sibling_failure_preserves_pending_reconciliation(tmp_path: Path) -> None:
    e = Env(tmp_path, downstream=True)
    e.target.visible_at = e.clock.now + timedelta(seconds=60)
    assert e.execute() is RunStatus.WAITING_VERIFICATION
    e.stores.steps.transition(
        e.run_id,
        "next",
        expected=StepStatus.PENDING,
        to=StepStatus.FAILED,
        error="independent_failure",
    )
    assert e.advance() is RunStatus.FAILED
    assert e.job.result is None
    e.clock.advance(seconds=60)
    assert e.advance() is RunStatus.FAILED
    assert e.job.result["outcome"] == "agreement"
    assert len(e.calls) == 1


def test_mail_header_and_application_ledger_cannot_forge_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e = Env(tmp_path, "mail")
    server = e.mail.server
    assert server is not None
    original = server.submit

    def wrong_envelope(*args: Any, **kwargs: Any) -> Any:
        record = original(*args, **kwargs)
        # The authorized header is unchanged while the actual accepted recipient differs.
        with server.directory.state.transaction() as conn:
            forged = record.model_copy(update={"accepted_recipients": ("wrong@outside.example",)})
            conn.execute(
                "UPDATE accepted SET record=? WHERE operation_key=?",
                (forged.model_dump_json(), record.operation_key),
            )
        return record

    monkeypatch.setattr(server, "submit", wrong_envelope)
    assert e.execute() is RunStatus.FAILED
    assert e.job.result["outcome"] == "disagreement"
    assert Ledger(tmp_path / "ledger.jsonl").records("email.sent")[0]["data"]["to"] == MAIL["to"]


def test_unavailable_read_at_exact_deadline_is_terminal_unknown(tmp_path: Path) -> None:
    e = Env(tmp_path)
    e.target.read_failures = 100
    assert e.execute() is RunStatus.WAITING_VERIFICATION
    e.clock.now = e.job.deadline
    assert e.advance() is RunStatus.UNKNOWN
    assert e.job.result["code"] == "verification_deadline"
    assert len(e.calls) == 1


def test_constructing_or_reading_absent_mock_store_has_no_filesystem_effect(tmp_path: Path) -> None:
    from gawe.connectors.mock_operations import MockOperations

    directory = tmp_path / "absent"
    target = MockOperations(Ledger(directory / "ledger.jsonl"), "iam")
    assert not directory.exists()
    assert target.read("missing", Clock().now) is None and target.records() == []
    assert not directory.exists()


def test_identical_action_in_another_plan_has_an_independent_operation_key(tmp_path: Path) -> None:
    from gawe.core.readback import operation_identity

    first = Env(tmp_path)
    first.execute()
    job = first.job
    assert operation_identity(job.action_hash, 2) != job.operation_key
    assert operation_identity(job.action_hash, 1) == job.operation_key


def test_legacy_verifying_write_without_intent_is_not_verified_from_reply(tmp_path: Path) -> None:
    e = Env(tmp_path)
    e.execute()
    step = e.stores.steps.get(e.run_id, "write")
    e.stores.steps._steps[(e.run_id, "write")] = step.model_copy(
        update={"status": StepStatus.VERIFYING}
    )
    e.stores.maintenance._jobs.clear()
    from gawe.core.executor import ExecutionRefusedError

    with pytest.raises(ExecutionRefusedError, match="legacy_write_reconciliation_required"):
        e.orch._executor.execute(e.run_id, "write", step.policy_decision)
    assert len(e.calls) == 1


def test_concurrent_processes_share_one_mock_idempotency_record(tmp_path: Path) -> None:
    import json
    import subprocess
    import sys

    from gawe.connectors.mock_operations import MockOperations

    code = """
import json, sys
from pathlib import Path
from gawe.connectors.base import Ledger
from gawe.connectors.mock_operations import MockOperations
system = MockOperations(Ledger(Path(sys.argv[1]) / 'ledger.jsonl'), 'crm')
print(json.dumps(system.accept('one-operation', {'value': sys.argv[2]})))
"""
    children = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path), str(i)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(3)
    ]
    try:
        records = []
        for child in children:
            out, err = child.communicate(timeout=20)
            assert child.returncode == 0, err
            records.append(json.loads(out))
        assert records[0] == records[1] == records[2]
        system = MockOperations(Ledger(tmp_path / "ledger.jsonl"), "crm")
        assert system.records() == [records[0]]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


@pytest.mark.parametrize("field", ["subject_override", "body_override"])
def test_mail_text_digest_disagreement_blocks_even_with_correct_reply(
    tmp_path: Path, field: str
) -> None:
    e = Env(tmp_path, "mail")
    setattr(e.target, field, "Different private text")
    assert e.execute() is RunStatus.FAILED
    assert e.job.result["outcome"] == "disagreement"
    assert "Different private text" not in str(e.job.evidence)


def test_explicit_resume_after_cancel_resolves_only_evidence(tmp_path: Path) -> None:
    e = Env(tmp_path, downstream=True)
    e.target.visible_at = e.clock.now + timedelta(seconds=60)
    e.execute()
    e.service.cancel_run(ALICE, e.run_id)
    e.clock.advance(seconds=60)
    e.service.resume_run(ALICE, e.run_id)
    assert e.job.result["outcome"] == "agreement" and e.job.continued
    assert e.stores.runs.get(e.run_id).status is RunStatus.CANCELLED
    assert e.stores.steps.get(e.run_id, "next").status is StepStatus.FAILED
    assert len(e.calls) == 1


def test_continuation_cannot_be_acknowledged_before_step_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e = Env(tmp_path)
    resolver = e.orch._executor.verification
    assert resolver is not None
    original = resolver.change

    def pause_after_result(job: Any, **kwargs: Any) -> Any:
        changed = original(job, **kwargs)
        if changed.result is not None and changed.evidence_seq is None:
            resolver.acknowledge(e.run_id)  # competing driver sees the result before continuation
            assert not e.job.continued
            raise Crash()
        return changed

    monkeypatch.setattr(resolver, "change", pause_after_result)
    with pytest.raises(Crash):
        e.advance()
    assert not e.job.continued
    monkeypatch.undo()
    assert e.advance() is RunStatus.COMPLETED
    assert e.job.continued and e.job.evidence_seq is not None


def test_post_terminal_evidence_advances_anchor_without_replacing_earlier_checkpoint(
    tmp_path: Path,
) -> None:
    from gawe.anchoring.file_sink import FileAnchorSink
    from gawe.core.anchor import Anchorer

    e = Env(tmp_path / "systems", downstream=True)
    path = tmp_path / "anchors.jsonl"
    sink = FileAnchorSink(path)
    anchorer = Anchorer(e.service._audit, [sink], clock=e.clock)
    e.service._anchorer = anchorer
    e.orch._on_finished = anchorer.anchor_run
    e.target.visible_at = e.clock.now + timedelta(seconds=60)
    e.execute()
    e.service.cancel_run(ALICE, e.run_id)
    original = sink.get(e.run_id)
    assert original is not None
    first_line = path.read_text().splitlines()[0]
    e.clock.advance(seconds=60)
    e.service.resume_run(ALICE, e.run_id)
    latest = FileAnchorSink(path).get(e.run_id)
    assert latest is not None and latest.point.seq > original.point.seq
    assert path.read_text().splitlines()[0] == first_line
    check = e.service.verify_audit(ALICE, e.run_id)
    assert check.ok and check.anchor.status == "matched" and check.anchor.events_after_anchor == 0
    e.advance()  # unchanged terminal runs do not write additional checkpoints
    assert len(path.read_text().splitlines()) == 2


@pytest.mark.parametrize("boundary", ["retry", "attempt"])
def test_stale_dispatcher_cannot_write_after_takeover_and_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    e = Env(tmp_path)
    other = assemble(
        e.bundle,
        stores=e.stores,
        signer=signer(),
        provider=FakeProvider([]),
        background=InlineBackground(),
        clock=e.clock,
    )
    executor = e.orch._executor

    def takeover() -> None:
        e.clock.advance(seconds=31)
        other._orch._executor.reconcile(e.stores.steps.get(e.run_id, "write"))
        other._orch.cancel(e.run_id, by=ALICE.user_id)

    if boundary == "retry":
        e.clm.faults.fail_next()
        monkeypatch.setattr(executor, "_sleep", lambda _: takeover())
    else:
        start = e.stores.executions.start

        def stalled_start(*args: Any, **kwargs: Any) -> Any:
            result = start(*args, **kwargs)
            takeover()
            return result

        monkeypatch.setattr(e.stores.executions, "start", stalled_start)
    assert e.advance() is RunStatus.CANCELLED
    assert e.target.records() == []
    assert len(e.calls) == (1 if boundary == "retry" else 0)


@pytest.mark.parametrize("boundary", ["result", "attempt"])
def test_recipient_rejection_recovers_fresh_approval_across_store_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from gawe.connectors.mock_directory import DirectoryEntry

    e = Env(tmp_path, "mail", args={**MAIL, "to": "vendor@outside.example"})
    assert e.advance() is RunStatus.PAUSED_FOR_APPROVAL
    e.mail.server.directory.put("legal@acme.example", DirectoryEntry(kind="mailbox"))
    resolver = e.orch._executor.verification
    assert resolver is not None
    target = resolver.jobs if boundary == "result" else e.stores.executions
    name = "replace" if boundary == "result" else "finish"
    original = getattr(target, name)

    def crash(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if (boundary == "result" and args[0].result is not None) or kwargs.get(
            "error"
        ) == "recipient_changed":
            raise Crash()
        return result

    monkeypatch.setattr(target, name, crash)
    with pytest.raises(Crash):
        e.approve()
    monkeypatch.undo()
    e.clock.advance(seconds=31)
    assert e.advance() is RunStatus.PAUSED_FOR_APPROVAL
    assert e.target.records() == []
    (fresh,) = e.service.inbox(BOB)
    assert fresh.resolved_args["delivery"]["reapproval_required"] is True
    e.approve()
    assert e.stores.runs.get(e.run_id).status is RunStatus.COMPLETED
    assert len(e.target.records()) == 1
