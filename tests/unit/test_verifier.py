from tests.flow import manifests

from gawe.core.verifier import Verifier

SEND = manifests()["contracts.send_email"]


def test_valid_output_passes() -> None:
    assert Verifier().verify(SEND, {}, {"message_id": "m1"}).ok


def test_schema_violation_fails() -> None:
    result = Verifier().verify(SEND, {}, {"message_id": 5})
    assert not result.ok and result.failures == ("output_schema",)


def test_postcondition_pass_fail_and_crash() -> None:
    manifest = SEND.model_copy(update={"postcondition": "sent_to_requested"})
    out = {"message_id": "m1", "to": "a@b.com"}
    ok = Verifier({"sent_to_requested": lambda a, o: a["to"] == o["to"]})
    assert ok.verify(manifest, {"to": "a@b.com"}, out).ok
    assert ok.verify(manifest, {"to": "x@y.com"}, out).failures == ("postcondition_failed",)

    def boom(a: object, o: object) -> bool:
        raise RuntimeError("secret detail")

    crashed = Verifier({"sent_to_requested": boom}).verify(manifest, {}, out)
    assert crashed.failures == ("postcondition_failed",)
    assert "secret" not in repr(crashed)


def test_unregistered_postcondition_fails_closed() -> None:
    manifest = SEND.model_copy(update={"postcondition": "nobody_registered_this"})
    result = Verifier().verify(manifest, {}, {"message_id": "m1"})
    assert result.failures == ("postcondition_unregistered",)
