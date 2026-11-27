"""M22: what each write relies on its target system for, required at startup."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from gawe.api.wiring import AppBundle, load_app_rules
from gawe.apps import contract_review, event_replay, it_access
from gawe.core.guarantees import EVERY_WRITE, GUARANTEES, required
from gawe.core.policy.provenance import PolicySource, PolicyVariable

GRANT = ("iam.grant_access", "2.0.0")
APPS = [contract_review, it_access, event_replay]


def current_writes(bundle: AppBundle) -> list[Any]:
    return [m for m in bundle.manifests if m.side_effect_class != "read" and not m.retired]


@pytest.mark.parametrize("app", APPS)
def test_every_shipped_write_declares_what_it_relies_on(tmp_path: Path, app: Any) -> None:
    bundle = app.bundle(tmp_path)
    rules = load_app_rules(bundle)
    writes = current_writes(bundle)
    assert writes
    for m in writes:
        needs = required(m, rules, bundle.policy_sources, bundle.policy_variables)
        assert needs >= EVERY_WRITE
        assert ("conditional_write" in needs) == (m.record_binding is not None)
        assert needs <= bundle.guarantees[(m.name, m.version)]


@pytest.mark.parametrize("app", APPS)
def test_removing_any_needed_guarantee_stops_startup(tmp_path: Path, app: Any) -> None:
    bundle = app.bundle(tmp_path)
    rules = load_app_rules(bundle)
    for m in current_writes(bundle):
        identity = (m.name, m.version)
        for guarantee in required(m, rules, bundle.policy_sources, bundle.policy_variables):
            without = {**bundle.guarantees, identity: bundle.guarantees[identity] - {guarantee}}
            with pytest.raises(ValueError, match=rf"{m.name}@{m.version} relies on .*{guarantee}"):
                load_app_rules(replace(bundle, guarantees=without))


def test_a_prepare_only_action_needs_no_guarantee(tmp_path: Path) -> None:
    bundle = it_access.bundle(tmp_path)
    without = {k: v for k, v in bundle.guarantees.items() if k != GRANT}
    with pytest.raises(ValueError, match="prepare-only"):
        load_app_rules(replace(bundle, guarantees=without))
    # GAWE never sends it, so nothing about its target is relied on.
    assert load_app_rules(
        replace(bundle, guarantees=without, prepare_only={GRANT: "security_admin"})
    )


def test_reads_and_retired_versions_need_nothing(tmp_path: Path) -> None:
    bundle = it_access.bundle(tmp_path)
    rules = load_app_rules(bundle)
    for m in bundle.manifests:
        if m.side_effect_class == "read" or m.retired:
            assert required(m, rules, bundle.policy_sources, bundle.policy_variables) == set()


@pytest.mark.parametrize(
    ("guarantees", "message"),
    [
        ({("iam.grant_access", "9.9.9"): frozenset()}, "doesn't register"),
        ({GRANT: frozenset({"idempotent_by_key", "exactly_once"})}, "unknown guarantees"),
    ],
)
def test_a_declaration_names_registered_versions_and_known_guarantees(
    tmp_path: Path, guarantees: dict[Any, Any], message: str
) -> None:
    bundle = it_access.bundle(tmp_path)
    with pytest.raises(ValueError, match=message):
        load_app_rules(replace(bundle, guarantees={**bundle.guarantees, **guarantees}))
    assert {"exactly_once"} - GUARANTEES


# --- running totals ------------------------------------------------------------------------------

CAP_RULE = """
  # Test only: a cap judged with a running total the IAM system keeps.
  - id: grants-today-over-cap-needs-security
    effect: needs_approval
    approver_role: security_admin
    reason: Too many grants today
    when:
      tool: iam.grant_access
      args:
        lifetime_hours: {gt: $request.grants_left_today}
"""


def capped(tmp_path: Path, *, through: bool = False) -> AppBundle:
    """The IT app with a rule that reads a running total (directly, or through a variable)."""
    bundle = it_access.bundle(tmp_path)
    rules = tmp_path / "capped-policies.yaml"
    rules.write_text(bundle.policy_rules.read_text(encoding="utf-8") + CAP_RULE, encoding="utf-8")
    sources = {
        **bundle.policy_sources,
        "grant_counter": PolicySource(kind="counter", resource="counters.grants_today"),
    }
    variables = dict(bundle.policy_variables)
    if through:
        variables["request.grants_today"] = PolicyVariable(dependencies=("grant_counter",))
        variables["request.grants_left_today"] = PolicyVariable(
            dependencies=("request.grants_today", "tenant_config")
        )
    else:
        variables["request.grants_left_today"] = PolicyVariable(dependencies=("grant_counter",))
    return replace(bundle, policy_rules=rules, policy_sources=sources, policy_variables=variables)


@pytest.mark.parametrize("through", [False, True], ids=["direct", "through_a_variable"])
def test_a_write_judged_by_a_running_total_needs_atomic_reservation(
    tmp_path: Path, through: bool
) -> None:
    bundle = capped(tmp_path, through=through)
    with pytest.raises(
        ValueError, match=r"iam.grant_access@2.0.0 relies on \['atomic_reservation'\]"
    ):
        load_app_rules(bundle)
    affected = {
        (m.name, m.version) for m in bundle.manifests if m.name == GRANT[0] and not m.retired
    }
    reserving = {
        **bundle.guarantees,
        **{key: bundle.guarantees[key] | {"atomic_reservation"} for key in affected},
    }
    assert load_app_rules(replace(bundle, guarantees=reserving))
    assert load_app_rules(replace(bundle, prepare_only={key: "security_admin" for key in affected}))
    # Every executable version needs the guarantee, including compatibility versions.
    for key in affected:
        missing_one = {**reserving, key: bundle.guarantees[key]}
        with pytest.raises(ValueError, match=rf"{key[0]}@{key[1]} relies on .*atomic_reservation"):
            load_app_rules(replace(bundle, guarantees=missing_one))


def test_a_running_total_no_rule_reads_needs_nothing(tmp_path: Path) -> None:
    bundle = it_access.bundle(tmp_path)
    unused = replace(
        bundle,
        policy_sources={
            **bundle.policy_sources,
            "grant_counter": PolicySource(kind="counter", resource="counters.grants_today"),
        },
        policy_variables={
            **bundle.policy_variables,
            "request.grants_today": PolicyVariable(dependencies=("grant_counter",)),
        },
    )
    assert load_app_rules(unused)


def test_one_prepare_only_action_exempts_only_itself(tmp_path: Path) -> None:
    bundle = contract_review.bundle(tmp_path)
    assessment = ("crm.write_assessment", "3.0.0")
    signature = ("esign.send_for_signature", "2.0.0")
    without = {
        **bundle.guarantees,
        assessment: bundle.guarantees[assessment] - {"conditional_write"},
    }
    with pytest.raises(ValueError, match="crm.write_assessment@3.0.0 relies on"):
        load_app_rules(
            replace(bundle, guarantees=without, prepare_only={signature: "legal_reviewer"})
        )
