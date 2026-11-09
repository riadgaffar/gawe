"""The API service wired to the in-memory engine, with identities in two tenants."""

from collections.abc import Sequence
from typing import Any

from gawe.api.auth import Identity
from gawe.api.background import InlineBackground
from gawe.api.service import ApiService, Limits
from gawe.core.anchor import Anchorer, AnchorSink
from tests.engine_flow import EngineFlow

T1, T2 = "tenant-1", "tenant-2"
ALICE = Identity(T1, "alice", frozenset({"case_manager"}))
BOB = Identity(T1, "bob", frozenset({"legal_reviewer"}))
DAVE = Identity(T1, "dave", frozenset({"case_manager", "legal_reviewer"}))  # can request AND hold
ADMIN = Identity(T1, "root", frozenset({"admin"}))
CAROL = Identity(T2, "carol", frozenset({"case_manager", "legal_reviewer", "admin"}))


class ApiFlow(EngineFlow):
    def __init__(
        self,
        plan_script: list[Any],
        read_script: list[Any] | None = None,
        *,
        limits: Limits | None = None,
        anchor_sinks: Sequence[AnchorSink] = (),
        **kw: Any,
    ) -> None:
        if anchor_sinks:
            kw["on_finished"] = lambda run: self.anchorer.anchor_run(run)
        super().__init__(plan_script, read_script, **kw)
        self.anchorer = Anchorer(self.audit, anchor_sinks)
        self.service = ApiService(
            orchestrator=self.orch,
            runs=self.runs,
            steps=self.steps,
            approval_service=self.approvals,
            approval_store=self.approval_store,
            registry=self.registry,
            audit=self.audit,
            background=InlineBackground(),
            profiles=frozenset({"contract_review"}),
            limits=limits,
            anchorer=self.anchorer,
        )

    def start_as(self, who: Identity = ALICE, **kw: Any) -> str:
        view = self.service.start_run(
            who,
            objective=kw.pop("objective", "Review the contract and email counsel"),
            profile=kw.pop("profile", "contract_review"),
            **kw,
        )
        return view.run_id
