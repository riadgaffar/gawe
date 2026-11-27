"""What `GET /v1/demo` tells the UI about the demo (M29): its companies, people and scenes, and
the tamper fixture's run. Names, roles and scene text only; never a token or a setting value a
person could not read on the Demo page anyway."""

from typing import Any

from gawe.demo.companies import COMPANIES
from gawe.demo.model import MODEL_NAME
from gawe.demo.scenes import SCENES


def demo_info(fixture_run_id: str | None) -> dict[str, Any]:
    return {
        "model": MODEL_NAME,
        "companies": [
            {
                "key": c.key,
                "name": c.name,
                "tenant_id": c.tenant_id,
                "summary": c.summary,
                "people": [
                    {"key": p.key, "name": p.name, "title": p.title, "roles": list(p.roles)}
                    for p in c.people
                ],
            }
            for c in COMPANIES
        ],
        "scenes": [
            {
                "scene_id": s.scene_id,
                "title": s.title,
                "objective": s.objective,
                "document": s.document,
                "modes": ["honest", "fooled"] if s.fooled else ["honest"],
                "fooled_how": s.fooled_how,
                "expect": s.expect,
            }
            for s in SCENES
        ],
        "fixture": None
        if fixture_run_id is None
        else {"company": "careful", "run_id": fixture_run_id},
    }
