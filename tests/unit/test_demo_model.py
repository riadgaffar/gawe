"""M29 S2: the scripted demo model, honest or fooled per run.

The demo model answers only the demo's scenes, from fixed scripts, and refuses anything else. A
run chooses honest or fooled only when the API runs the demo model; the mode is stored on the run,
shown in its views and its audit, and never sent to a real model.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from tests.helpers import signer

from gawe.api.auth import Identity
from gawe.api.background import InlineBackground
from gawe.api.errors import BadRequestError
from gawe.api.wiring import assemble, memory_stores
from gawe.apps import contract_review as cr
from gawe.core.canonical import canonical_json, sha256_hex
from gawe.demo.model import MODEL_NAME, DemoModel, DemoScriptMissing
from gawe.demo.scenes import BY_ID, SCENES
from gawe.llm.fake import FakeProvider
from gawe.llm.provider import LLMRequest, RequestContext, content_sha256, request_hash

ALICE = Identity("t1", "alice", frozenset({"case_manager"}))
BOB = Identity("t1", "bob", frozenset({"legal_reviewer"}))


class Demo:
    def __init__(self, tmp_path: Path, provider: Any = None) -> None:
        self.stores = memory_stores()
        self.svc = assemble(
            cr.bundle(tmp_path),
            stores=self.stores,
            signer=signer(),
            provider=provider or DemoModel(),
            background=InlineBackground(),
        )

    def run(self, scene_id: str, mode: str | None = "honest", document: str | None = None) -> Any:
        scene = BY_ID[scene_id]
        started = self.svc.start_run(
            ALICE,
            objective=scene.objective,
            profile="contract_review",
            document=document if document is not None else scene.text(),
            model_mode=mode,
        )
        return self.svc.get_run(ALICE, started.run_id)


def request(purpose: str, scene_id: str, mode: str | None, document: str | None = None) -> Any:
    scene = BY_ID[scene_id]
    context = RequestContext(
        model_mode=mode,
        objective_sha256=scene.objective_sha256(),
        document_sha256=content_sha256(document if document is not None else scene.text()),
    )
    return LLMRequest(purpose, "system", "user", {"type": "object"}, context=context)


# --- the model ----------------------------------------------------------------------------------


@pytest.mark.parametrize("scene", SCENES, ids=lambda s: s.scene_id)
def test_each_scene_answers_its_honest_script(scene: Any) -> None:
    model = DemoModel()
    read = model.complete(request("read", scene.scene_id, "honest"))
    plan = model.complete(request("plan", scene.scene_id, "honest"))
    assert json.loads(read.text) == dict(scene.honest.findings)
    assert json.loads(plan.text) == dict(scene.honest.plan)
    assert read.model == f"{MODEL_NAME} (honest)"


def test_a_fooled_script_is_answered_only_where_the_scene_has_one() -> None:
    model = DemoModel()
    fooled = model.complete(request("plan", "c1003-email-counsel", "fooled"))
    emails = [s for s in json.loads(fooled.text)["steps"] if s["tool"] == "email.send"]
    assert [s["args"]["to"] for s in emails] == ["attacker@evil.example"]
    with pytest.raises(DemoScriptMissing, match="no fooled script"):
        model.complete(request("plan", "c1001-review", "fooled"))


def test_no_mode_means_honest() -> None:
    model = DemoModel()
    assert json.loads(model.complete(request("read", "c1003-review", None)).text) == dict(
        BY_ID["c1003-review"].honest.findings
    )


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: LLMRequest("plan", "s", "u", {}), "needs the run's context"),
        (
            lambda: LLMRequest(
                "plan", "s", "u", {}, context=RequestContext("honest", content_sha256("other"))
            ),
            "no scene for this objective",
        ),
        (lambda: request("read", "c1001-review", "honest", "another document"), "own sample"),
        (lambda: request("summarise", "c1001-review", "honest"), "no answer for"),
        (lambda: request("plan", "c1003-review", "sideways"), "no sideways script"),
    ],
    ids=["no context", "unknown objective", "other document", "other purpose", "bad mode"],
)
def test_anything_else_is_refused_never_guessed(make: Any, message: str) -> None:
    with pytest.raises(DemoScriptMissing, match=message):
        DemoModel().complete(make())


def test_the_context_is_never_part_of_what_a_real_model_is_sent() -> None:
    plain = LLMRequest("plan", "system", "user", {"type": "object"})
    with_context = LLMRequest(
        "plan", "system", "user", {"type": "object"}, context=RequestContext("fooled", "a", "b")
    )
    assert request_hash(plain) == request_hash(with_context)


def test_content_keys_ignore_line_endings_and_surrounding_space() -> None:
    assert content_sha256("a\r\nb\n") == content_sha256("  a\nb")
    assert content_sha256("a b") != content_sha256("a  b")


# --- runs ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scene_id", "mode", "status", "last"),
    [
        ("c1001-review", "honest", "paused_for_approval", "awaiting_approval"),
        ("c1002-signature", "honest", "paused_for_approval", "awaiting_approval"),
        ("c1003-review", "honest", "completed", "succeeded"),
        ("c1003-review", "fooled", "paused_for_approval", "awaiting_approval"),
        ("c1003-email-counsel", "honest", "paused_for_approval", "awaiting_approval"),
        ("c1003-email-counsel", "fooled", "denied", "denied"),
    ],
)
def test_scenes_end_as_policy_decides_on_the_default_company(
    tmp_path: Path, scene_id: str, mode: str, status: str, last: str
) -> None:
    view = Demo(tmp_path).run(scene_id, mode)
    assert (view.status, view.steps[-1].status) == (status, last)


def test_honest_and_fooled_runs_of_one_scene_side_by_side(tmp_path: Path) -> None:
    d = Demo(tmp_path)
    honest, fooled = d.run("c1003-email-counsel", "honest"), d.run("c1003-email-counsel", "fooled")
    plan = {
        r: [s.args for s in d.stores.runs.get(r).plan.steps] for r in (honest.run_id, fooled.run_id)
    }  # type: ignore[union-attr]
    assert plan[honest.run_id][-1]["to"] == "counsel@initech.example"
    assert plan[fooled.run_id][-1]["to"] == "attacker@evil.example"
    assert "blocked-domain-email-denied" in fooled.steps[-1].policy_rule_ids
    assert d.stores.runs.get(fooled.run_id).planner_model == f"{MODEL_NAME} (fooled)"


def test_the_mode_shows_on_the_run_and_the_approval_card(tmp_path: Path) -> None:
    d = Demo(tmp_path)
    view = d.run("c1003-review", "fooled")
    assert view.model_mode == "fooled"
    (card,) = d.svc.inbox(BOB)
    assert card.model_mode == "fooled"
    plain = d.run("c1003-review", None)
    assert plain.model_mode is None


def test_only_scripted_runs_carry_the_mode_in_their_audit(tmp_path: Path) -> None:
    d = Demo(tmp_path)
    scripted = d.run("c1003-review", "fooled")
    unscripted = d.run("c1003-review", None)
    scene = BY_ID["c1003-review"]
    base = {
        "profile": "contract_review",
        "has_document": True,
        "objective_sha256": sha256_hex(scene.objective.encode("utf-8")),
    }
    for run_id, payload in [
        (scripted.run_id, {**base, "model_mode": "fooled"}),
        (unscripted.run_id, base),  # the shape every run had before M29
    ]:
        (created,) = [e for e in d.stores.audit.events(run_id) if e.type == "run.created"]
        assert created.payload_hash == sha256_hex(canonical_json(payload))


def test_a_mode_is_refused_unless_the_api_runs_the_demo_model(tmp_path: Path) -> None:
    d = Demo(tmp_path, provider=FakeProvider([]))
    with pytest.raises(BadRequestError, match="only accepted under the scripted demo model"):
        d.run("c1001-review", "fooled")
    assert d.stores.runs.list_for_tenant("t1") == []


def test_a_mode_other_than_honest_or_fooled_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BadRequestError, match="honest or fooled"):
        Demo(tmp_path).run("c1001-review", "sideways")


def test_a_document_the_scene_doesnt_know_fails_at_reading(tmp_path: Path) -> None:
    view = Demo(tmp_path).run("c1001-review", "honest", document="An entirely different contract.")
    assert view.status == "failed" and view.error == "reading_failed"


def test_an_objective_no_scene_has_fails_at_reading(tmp_path: Path) -> None:
    d = Demo(tmp_path)
    started = d.svc.start_run(
        ALICE,
        objective="Do something no scene covers.",
        profile="contract_review",
        document=BY_ID["c1001-review"].text(),
        model_mode="honest",
    )
    assert d.svc.get_run(ALICE, started.run_id).status == "failed"


# --- choosing the provider ----------------------------------------------------------------------


def test_the_provider_is_chosen_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    from gawe.api.main import ConfigError, _provider_from_env

    monkeypatch.setenv("GAWE_LLM_PROVIDER", "demo")
    assert isinstance(_provider_from_env(), DemoModel)
    monkeypatch.setenv("GAWE_LLM_PROVIDER", "something-else")
    with pytest.raises(ConfigError, match="anthropic' or 'demo"):
        _provider_from_env()
