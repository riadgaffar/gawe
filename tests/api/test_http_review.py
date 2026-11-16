"""The review routes over HTTP: role, tenant, verdict once, and no argument values."""

from datetime import UTC, date, datetime

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from gawe.api.app import create_app  # noqa: E402
from gawe.api.auth import TokenAuthenticator, hash_token, new_token  # noqa: E402
from gawe.api.background import InlineBackground  # noqa: E402
from gawe.api.wiring import assemble, memory_stores  # noqa: E402
from gawe.core.review import ReviewItem, item_identity  # noqa: E402
from gawe.llm.fake import FakeProvider  # noqa: E402
from tests.api.test_http import PEOPLE, START  # noqa: E402
from tests.api_bundle import bundle  # noqa: E402
from tests.engine_flow import INTERNAL_PLAN  # noqa: E402
from tests.helpers import signer  # noqa: E402


class Reviewing:
    def __init__(self) -> None:
        b, _ = bundle()
        self.stores = memory_stores()
        self.tokens = {name: new_token() for name in PEOPLE}
        auth = TokenAuthenticator({hash_token(self.tokens[n]): i for n, i in PEOPLE.items()})
        service = assemble(
            b,
            stores=self.stores,
            signer=signer(),
            provider=FakeProvider([INTERNAL_PLAN]),
            background=InlineBackground(),
        )
        self.http = TestClient(create_app(service=service, authenticator=auth))

    def h(self, who: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who]}"}

    def flagged_run(self) -> ReviewItem:
        run_id = self.http.post("/v1/runs", json=START, headers=self.h("alice")).json()["run_id"]
        day = date(2026, 1, 1)
        return self.stores.reviews.add(
            ReviewItem(
                item_id=item_identity("t1", day, run_id, "s1", "a" * 64, "rules_changed"),
                tenant_id="t1",
                day=day,
                run_id=run_id,
                step_id="s1",
                action_hash="a" * 64,
                reason="rules_changed",
                created_at=datetime(2026, 1, 2, 1, tzinfo=UTC),
            )
        )


def test_review_routes() -> None:
    c = Reviewing()
    item = c.flagged_run()
    assert c.http.get("/v1/review-items", headers=c.h("alice")).status_code == 403
    listed = c.http.get("/v1/review-items", headers=c.h("bob"))
    assert listed.status_code == 200
    (view,) = listed.json()
    assert view["item_id"] == item.item_id and view["reason"] == "rules_changed"
    assert set(view) == {
        "item_id",
        "day",
        "run_id",
        "step_id",
        "action_hash",
        "reason",
        "key_id",
        "status",
        "created_at",
        "decided_by",
        "decided_at",
    }
    assert c.http.get("/v1/review-items", headers=c.h("carol")).json() == []  # other tenant
    other = c.http.post(
        f"/v1/review-items/{item.item_id}/verdict",
        json={"verdict": "confirmed"},
        headers=c.h("carol"),
    )
    assert other.status_code == 404
    bad = c.http.post(
        f"/v1/review-items/{item.item_id}/verdict", json={"verdict": "fine"}, headers=c.h("bob")
    )
    assert bad.status_code == 422
    url = f"/v1/review-items/{item.item_id}/verdict"
    ok = c.http.post(url, json={"verdict": "missed_risk"}, headers=c.h("bob"))
    assert ok.status_code == 200 and ok.json()["status"] == "missed_risk"
    again = c.http.post(url, json={"verdict": "confirmed"}, headers=c.h("bob"))
    assert again.status_code == 409
    everything = c.http.get("/v1/review-items?status=all", headers=c.h("bob")).json()
    assert [i["status"] for i in everything] == ["missed_risk"]
