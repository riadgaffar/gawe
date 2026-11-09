import threading
from datetime import timedelta

import pytest
from tests.flow import INTERNAL_EMAIL, RUN, Crash, Flow
from tests.helpers import signer

from gawe.core.executor import (
    ConcurrentExecutionError,
    ExecutionRefusedError,
    RetryPolicy,
    ToolTransientError,
    idempotency_key,
)
from gawe.core.statemachine import StepStatus

SEND = "contracts.send_email"


def ready(flow: Flow, args=INTERNAL_EMAIL, tool: str = SEND):  # type: ignore[no-untyped-def]
    decision = flow.judge(tool, args)
    assert decision.outcome == "allow"
    flow.add_step(decision, tool, args)
    return decision


def test_allowed_step_runs_once_and_succeeds() -> None:
    flow = Flow()
    outcome = flow.executor.execute(RUN, "s1", ready(flow))
    assert outcome.status is StepStatus.SUCCEEDED and outcome.attempts == 1
    assert len(flow.connector.calls) == 1
    assert flow.steps.get(RUN, "s1").status is StepStatus.SUCCEEDED
    assert flow.audit_types() == ["execution.started", "execution.succeeded", "verification.passed"]


def test_audit_never_contains_arguments_or_output() -> None:
    flow = Flow()
    flow.executor.execute(RUN, "s1", ready(flow))
    dump = repr(flow.audit_store.events(RUN))
    assert "colleague@acme.com" not in dump and "m-0" not in dump


def test_succeeded_step_cannot_run_again() -> None:
    flow = Flow()
    decision = ready(flow)
    flow.executor.execute(RUN, "s1", decision)
    with pytest.raises(ExecutionRefusedError):
        flow.executor.execute(RUN, "s1", decision)
    assert len(flow.connector.calls) == 1


def test_transient_errors_are_retried_with_backoff() -> None:
    flow = Flow()
    flow.connector.script = [ToolTransientError(), ToolTransientError()]
    outcome = flow.executor.execute(RUN, "s1", ready(flow))
    assert outcome.status is StepStatus.SUCCEEDED and outcome.attempts == 3
    assert flow.sleeps == [1.0, 2.0]
    keys = {c.idempotency_key for c in flow.connector.calls}
    assert len(keys) == 1  # every attempt reuses one key
    assert len(flow.connector.effects) == 1


def test_retries_are_bounded() -> None:
    flow = Flow(retry=RetryPolicy(max_attempts=2, base_delay=1.0))
    flow.connector.script = [ToolTransientError()] * 5
    outcome = flow.executor.execute(RUN, "s1", ready(flow))
    assert outcome.status is StepStatus.FAILED and outcome.attempts == 2
    assert len(flow.connector.calls) == 2


def test_permanent_error_is_not_retried_and_message_is_not_logged() -> None:
    flow = Flow()
    flow.connector.script = [ValueError("password=hunter2")]
    outcome = flow.executor.execute(RUN, "s1", ready(flow))
    assert outcome.status is StepStatus.FAILED and outcome.error == "ValueError"
    assert len(flow.connector.calls) == 1
    assert "hunter2" not in repr(flow.audit_store.events(RUN))
    assert "hunter2" not in repr(flow.steps.get(RUN, "s1"))


def test_declared_error_code_is_recorded() -> None:
    class Rejected(Exception):
        code = "recipient_rejected"

    flow = Flow()
    flow.connector.script = [Rejected("details")]
    assert flow.executor.execute(RUN, "s1", ready(flow)).error == "recipient_rejected"


def test_output_failing_schema_fails_verification() -> None:
    flow = Flow(tool_overrides={SEND: lambda a, c: {"unexpected": True}})
    outcome = flow.executor.execute(RUN, "s1", ready(flow))
    assert outcome.status is StepStatus.FAILED
    assert outcome.error == "verification_failed:output_schema"
    assert "verification.failed" in flow.audit_types()


def test_args_that_violate_the_tool_schema_never_reach_the_tool() -> None:
    # Policy itself denies bad args; this covers the executor's own check if policy were wrong.
    flow = Flow()
    bad = {"to": "colleague@acme.com"}  # body missing
    denied = flow.judge(SEND, bad)
    assert denied.outcome == "deny"
    allowed = signer().seal(
        step_id="s1", action_hash=denied.action_hash, outcome="allow", rule_ids=["test"], reason="x"
    )
    flow.add_step(allowed, SEND, bad)
    outcome = flow.executor.execute(RUN, "s1", allowed)
    assert outcome.error == "args_schema_invalid" and not flow.connector.calls


def test_missing_implementation_fails_the_step() -> None:
    flow = Flow()
    del flow.executor._tools[SEND]  # noqa: SLF001
    outcome = flow.executor.execute(RUN, "s1", ready(flow))
    assert outcome.error == "tool_not_implemented"


def test_crash_then_resume_after_lease_runs_effect_once() -> None:
    flow = Flow(lease=timedelta(minutes=5))
    flow.connector.script = [None]
    decision = ready(flow)
    flow.connector.script = [Crash()]
    with pytest.raises(Crash):
        flow.executor.execute(RUN, "s1", decision)
    assert flow.steps.get(RUN, "s1").status is StepStatus.EXECUTING

    with pytest.raises(ConcurrentExecutionError):  # the first worker may still be alive
        flow.executor.execute(RUN, "s1", decision)

    flow.clock.advance(minutes=6)
    outcome = flow.executor.execute(RUN, "s1", decision)
    assert outcome.status is StepStatus.SUCCEEDED and outcome.attempts == 2
    assert len({c.idempotency_key for c in flow.connector.calls}) == 1
    assert len(flow.connector.effects) == 1
    assert "execution.resumed" in flow.audit_types()


def test_resume_after_output_was_saved_does_not_call_the_tool() -> None:
    flow2 = Flow()
    d2 = ready(flow2)
    step = flow2.steps.get(RUN, "s1")
    flow2.steps.transition(RUN, "s1", expected=StepStatus.POLICY_CHECKED, to=StepStatus.EXECUTING)
    flow2.executions.start(
        RUN,
        "s1",
        attempt=1,
        idempotency_key=idempotency_key(step.action_hash or ""),
        now=flow2.clock(),
    )
    flow2.executions.save_output(RUN, "s1", {"message_id": "m-saved"})
    flow2.clock.advance(minutes=10)
    outcome = flow2.executor.execute(RUN, "s1", d2)
    assert outcome.status is StepStatus.SUCCEEDED
    assert flow2.connector.calls == []


def test_concurrent_workers_run_the_tool_once() -> None:
    flow = Flow()
    decision = ready(flow)
    results: list[str] = []

    def work() -> None:
        try:
            flow.executor.execute(RUN, "s1", decision)
            results.append("ran")
        except ExecutionRefusedError:
            results.append("refused")

    threads = [threading.Thread(target=work) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ran") == 1
    assert len(flow.connector.calls) == 1


def test_idempotency_key_depends_on_the_action() -> None:
    assert idempotency_key("a" * 64) == idempotency_key("a" * 64)
    assert idempotency_key("a" * 64) != idempotency_key("b" * 64)


def test_retry_policy_validation_and_cap() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    p = RetryPolicy(max_attempts=9, base_delay=1.0, max_delay=5.0)
    assert [p.delay(n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 5.0]


def test_recent_activity_blocks_takeover_even_when_the_last_attempt_finished() -> None:
    flow = Flow(lease=timedelta(minutes=5))
    decision = ready(flow)
    step = flow.steps.get(RUN, "s1")
    flow.steps.transition(RUN, "s1", expected=StepStatus.POLICY_CHECKED, to=StepStatus.EXECUTING)
    flow.executions.start(
        RUN,
        "s1",
        attempt=1,
        idempotency_key=idempotency_key(step.action_hash or ""),
        now=flow.clock(),
    )
    flow.executions.finish(RUN, "s1", attempt=1, status="failed", now=flow.clock(), error="X")
    flow.clock.advance(minutes=1)  # a worker is probably sleeping between retries
    with pytest.raises(ConcurrentExecutionError):
        flow.executor.execute(RUN, "s1", decision)
    assert flow.connector.calls == []
    flow.clock.advance(minutes=5)
    assert flow.executor.execute(RUN, "s1", decision).status is StepStatus.SUCCEEDED
    assert len(flow.connector.calls) == 1


def test_verifying_step_resumes_from_saved_output_without_calling_the_tool() -> None:
    from tests.flow import Flow

    f = Flow()
    args = {"document_id": "d1"}
    decision = f.judge("contracts.extract_risks", args)
    f.add_step(decision, "contracts.extract_risks", args, status=StepStatus.VERIFYING)
    f.executions.save_output("run1", decision.step_id, {"ok": True})
    outcome = f.executor.execute("run1", decision.step_id, decision)
    assert outcome.status is StepStatus.SUCCEEDED
    assert f.connector.calls == []


def test_verifying_step_with_no_saved_output_fails_closed() -> None:
    from tests.flow import Flow

    f = Flow()
    args = {"document_id": "d1"}
    decision = f.judge("contracts.extract_risks", args)
    f.add_step(decision, "contracts.extract_risks", args, status=StepStatus.VERIFYING)
    outcome = f.executor.execute("run1", decision.step_id, decision)
    assert outcome.status is StepStatus.FAILED and outcome.error == "output_missing"
    assert f.connector.calls == []
