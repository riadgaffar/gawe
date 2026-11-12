from dataclasses import replace

import pytest
from tests.api_bundle import bundle
from tests.api_flow import ALICE, BOB
from tests.engine_flow import EMAIL_PLAN, GOOD_READ, INTERNAL_PLAN
from tests.helpers import signer
from tests.unit.test_llm_reader import PROFILE

from gawe.api.background import InlineBackground
from gawe.api.wiring import assemble, memory_stores
from gawe.llm.fake import FakeProvider


def test_assembled_service_runs_an_approval_flow() -> None:
    b, connector = bundle()
    service = assemble(
        b,
        stores=memory_stores(),
        signer=signer(),
        provider=FakeProvider([EMAIL_PLAN]),
        background=InlineBackground(),
    )
    run = service.start_run(ALICE, objective="email counsel", profile="contract_review")
    assert service.get_run(ALICE, run.run_id).status == "paused_for_approval"
    (item,) = service.inbox(BOB)
    service.decide(BOB, item.approval_id, decision="approved", action_hash=item.action_hash)
    assert service.get_run(ALICE, run.run_id).status == "completed"
    assert len(connector.effects) == 2


def test_reader_profile_enables_documents() -> None:
    b, _ = bundle()
    with_reader = replace(b, reader_profile=PROFILE)
    service = assemble(
        with_reader,
        stores=memory_stores(),
        signer=signer(),
        provider=FakeProvider([GOOD_READ, INTERNAL_PLAN]),
        background=InlineBackground(),
    )
    run = service.start_run(ALICE, objective="review", profile="contract_review", document="text")
    assert service.get_run(ALICE, run.run_id).status == "completed"


def test_missing_tool_implementation_is_refused() -> None:
    b, _ = bundle()
    broken = replace(b, tools={})
    with pytest.raises(ValueError, match="no implementation"):
        assemble(
            broken,
            stores=memory_stores(),
            signer=signer(),
            provider=FakeProvider([]),
            background=InlineBackground(),
        )
