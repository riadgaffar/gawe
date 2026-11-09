from pathlib import Path

import pytest
import yaml
from tests.helpers import TOOLS, make_ctx, signer

from gawe.core.canonical import action_hash
from gawe.core.policy.engine import RuleEngine
from gawe.core.policy.rules import load_rules

FIXTURES = Path(__file__).parent.parent / "fixtures"
CASES = yaml.safe_load((FIXTURES / "example_policy_cases.yaml").read_text())["cases"]
ENGINE = RuleEngine(load_rules(FIXTURES / "example_policies.yaml"), signer())


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_golden_case(case: dict) -> None:  # type: ignore[type-arg]
    manifest = TOOLS[case["tool"]]
    kwargs = {"variables": case["variables"]} if "variables" in case else {}
    ctx = make_ctx(manifest, case["args"], roles=case.get("roles", ["case_manager"]), **kwargs)

    decision = ENGINE.evaluate(ctx)

    expect = case["expect"]
    assert decision.outcome == expect["outcome"]
    assert decision.approver_role == expect.get("approver_role")
    if "rule_ids" in expect:
        assert list(decision.rule_ids) == expect["rule_ids"]
    # Every decision is signed and bound to this exact action.
    signer().require_valid(
        decision,
        expected_action_hash=action_hash(
            tool=manifest.name,
            version=manifest.version,
            resolved_args=ctx.resolved_args,
            run_id=ctx.run_id,
            step_id=ctx.step_id,
        ),
    )
