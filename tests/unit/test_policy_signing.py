import pytest
from tests.helpers import KEY, signer

from gawe.core.models import PolicyDecision
from gawe.core.policy.signing import (
    ActionMismatchError,
    ForgedDecisionError,
    PolicySigner,
    PolicySigningError,
)

SEAL = {
    "step_id": "s1",
    "action_hash": "a" * 64,
    "outcome": "needs_approval",
    "rule_ids": ["r1", "r2"],
    "reason": "because",
    "approver_role": "legal",
}


def sealed() -> PolicyDecision:
    return signer().seal(**SEAL)  # type: ignore[arg-type]


def test_sealed_decision_verifies() -> None:
    assert signer().verify(sealed())


def test_signature_is_deterministic() -> None:
    assert sealed().signature == sealed().signature


@pytest.mark.parametrize(
    "field,value",
    [
        ("step_id", "s2"),
        ("action_hash", "b" * 64),
        ("outcome", "allow"),
        ("rule_ids", ("r1",)),
        ("reason", "different"),
        ("approver_role", "finance"),
        ("approver_role", None),
    ],
)
def test_changing_any_field_invalidates_signature(field: str, value: object) -> None:
    forged = sealed().model_copy(update={field: value})
    assert not signer().verify(forged)


def test_flipping_deny_to_allow_is_caught() -> None:
    denied = signer().seal(
        step_id="s1", action_hash="a" * 64, outcome="deny", rule_ids=["default-deny"], reason="no"
    )
    assert not signer().verify(denied.model_copy(update={"outcome": "allow"}))


def test_fabricated_decision_with_made_up_signature_is_rejected() -> None:
    fake = PolicyDecision(
        step_id="s1",
        action_hash="a" * 64,
        outcome="allow",
        rule_ids=("x",),
        reason="trust me",
        signature="0" * 64,
    )
    with pytest.raises(ForgedDecisionError):
        signer().require_valid(fake, expected_action_hash="a" * 64)


def test_decision_signed_with_another_key_is_rejected() -> None:
    other = PolicySigner(bytes(reversed(KEY)))
    decision = other.seal(**SEAL)  # type: ignore[arg-type]
    assert not signer().verify(decision)
    with pytest.raises(ForgedDecisionError):
        signer().require_valid(decision, expected_action_hash="a" * 64)


def test_valid_decision_for_a_different_action_is_rejected() -> None:
    with pytest.raises(ActionMismatchError):
        signer().require_valid(sealed(), expected_action_hash="c" * 64)


def test_valid_decision_for_the_right_action_passes() -> None:
    signer().require_valid(sealed(), expected_action_hash="a" * 64)


def test_short_keys_rejected() -> None:
    with pytest.raises(ValueError, match="at least"):
        PolicySigner(b"short")


def test_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAWE_POLICY_SIGNING_KEY", KEY.hex())
    assert PolicySigner.from_env().verify(sealed())


@pytest.mark.parametrize("value", [None, "", "   ", "not-hex!", "abcd"])
def test_from_env_rejects_missing_or_bad_keys(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("GAWE_POLICY_SIGNING_KEY", raising=False)
    else:
        monkeypatch.setenv("GAWE_POLICY_SIGNING_KEY", value)
    with pytest.raises(PolicySigningError):
        PolicySigner.from_env()
