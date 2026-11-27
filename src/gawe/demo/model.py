"""A scripted model for the demo (M29): `GAWE_LLM_PROVIDER=demo`.

It answers the Reader and the Planner from the scene whose objective the run has, honestly or
fooled as the run chose, and checks the run's document is that scene's. Anything else (another
objective, another document, a mode the scene has no script for) is refused, so the run fails
instead of the model guessing. It is labelled as scripted everywhere a run's model is shown.
"""

import json
from collections.abc import Mapping

from gawe.demo.scenes import SCENES, Scene
from gawe.llm.provider import LLMOutputError, LLMRequest, LLMResponse

MODEL_NAME = "gawe-demo-scripted"


class DemoScriptMissing(LLMOutputError):
    """The demo model has no script for this request; the run fails, nothing is guessed."""


class DemoModel:
    # The API lets runs choose a model mode only under a provider that says it takes one.
    ACCEPTS_MODEL_MODE = True

    def __init__(self, scenes: tuple[Scene, ...] = SCENES) -> None:
        self._by_objective: Mapping[str, Scene] = {s.objective_sha256(): s for s in scenes}
        self._documents: Mapping[str, str] = {s.scene_id: s.document_sha256() for s in scenes}

    def complete(self, request: LLMRequest) -> LLMResponse:
        context = request.context
        if context is None or context.objective_sha256 is None:
            raise DemoScriptMissing("the demo model needs the run's context")
        scene = self._by_objective.get(context.objective_sha256)
        if scene is None:
            raise DemoScriptMissing("the demo model has no scene for this objective")
        mode = context.model_mode or "honest"
        script = scene.honest if mode == "honest" else scene.fooled
        if mode not in ("honest", "fooled") or script is None:
            raise DemoScriptMissing(f"scene {scene.scene_id} has no {mode} script")
        if request.purpose == "read":
            if context.document_sha256 != self._documents[scene.scene_id]:
                raise DemoScriptMissing(f"scene {scene.scene_id} reads its own sample document")
            text = json.dumps(dict(script.findings))
        elif request.purpose == "plan":
            text = json.dumps(dict(script.plan))
        else:
            raise DemoScriptMissing(f"the demo model has no answer for {request.purpose!r}")
        return LLMResponse(
            text=text, model=f"{MODEL_NAME} ({mode})", input_tokens=0, output_tokens=0
        )
