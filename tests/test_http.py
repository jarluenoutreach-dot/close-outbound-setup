"""HTTP layer: webhook signature enforcement and the Close client's request shapes."""
import asyncio
import base64
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from outbound.app import create_app
from outbound.close_api import CloseClient, CloseError
from outbound.security import sign

from .fakes import make_settings


def test_webhook_rejects_unsigned_and_accepts_signed(tmp_path):
    key = "cd" * 32
    app = create_app(make_settings(tmp_path, webhook_signature_key=key))
    body = json.dumps({"event": {"object_type": "contact", "action": "updated"}}).encode()
    with TestClient(app) as client:
        assert client.post("/webhooks/close", content=body).status_code == 401
        bad = {"close-sig-hash": "0" * 64, "close-sig-timestamp": str(int(time.time()))}
        assert client.post("/webhooks/close", content=body, headers=bad).status_code == 401
        ts = str(int(time.time()))
        good = {"close-sig-hash": sign(key, ts, body), "close-sig-timestamp": ts}
        assert client.post("/webhooks/close", content=body, headers=good).status_code == 200
        assert client.get("/health").json()["ok"] is True


def test_webhook_rejects_everything_without_a_key(tmp_path):
    app = create_app(make_settings(tmp_path, webhook_signature_key=""))
    body = b"{}"
    ts = str(int(time.time()))
    with TestClient(app) as client:
        headers = {"close-sig-hash": sign("cd" * 32, ts, body), "close-sig-timestamp": ts}
        assert client.post("/webhooks/close", content=body, headers=headers).status_code == 401


def _client(handler):
    return CloseClient("api_key_123", transport=httpx.MockTransport(handler))


def test_basic_auth_and_activity_pagination():
    seen = []

    def handler(request):
        seen.append(request)
        skip = int(request.url.params["_skip"])
        return httpx.Response(200, json={"data": [{"id": f"a{skip + i}"} for i in range(100 if skip == 0 else 3)],
                                         "has_more": skip == 0})

    async def go():
        client = _client(handler)
        acts = await client.list_activities("sms", "lead_1")
        await client.aclose()
        return acts

    acts = asyncio.run(go())
    assert len(acts) == 103
    auth = seen[0].headers["authorization"]
    assert auth == "Basic " + base64.b64encode(b"api_key_123:").decode()
    assert seen[0].url.path == "/api/v1/activity/sms/"
    assert seen[0].url.params["lead_id"] == "lead_1"


def test_create_sms_is_not_retried_on_server_error_but_is_on_429():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, json={"error": "boom"})

    async def go():
        client = _client(handler)
        with pytest.raises(CloseError):
            await client.create_sms({"lead_id": "x"})
        await client.aclose()

    asyncio.run(go())
    assert calls["n"] == 1

    responses = [httpx.Response(429, json={"error": {"rate_reset": 0.01}}), httpx.Response(200, json={"id": "acti_1"})]

    async def go2():
        client = _client(lambda r: responses.pop(0))
        sms = await client.create_sms({"lead_id": "x"})
        await client.aclose()
        return sms

    assert asyncio.run(go2())["id"] == "acti_1"


def test_smart_view_membership_and_fallback():
    view = {"id": "save_1", "name": "Webinar Workflow", "s_query": {"query": {"type": "and", "queries": []}}}
    members = ["lead_a", "lead_b"]
    state = {"reject_filter": False}

    def handler(request):
        if request.url.path.endswith("/saved_search/save_1/"):
            return httpx.Response(200, json=view)
        if request.url.path.endswith("/data/search/"):
            body = json.loads(request.content)
            queries = body["query"]["queries"]
            id_filter = [q for q in queries if q.get("type") == "field_condition"]
            if id_filter:
                if state["reject_filter"]:
                    return httpx.Response(400, json={"error": "bad query"})
                wanted = id_filter[0]["condition"]["values"][0]
                return httpx.Response(200, json={"data": [{"id": wanted}] if wanted in members else [], "cursor": None})
            if not body.get("_cursor"):
                return httpx.Response(200, json={"data": [{"id": "lead_a"}], "cursor": "c2"})
            return httpx.Response(200, json={"data": [{"id": "lead_b"}], "cursor": None})
        return httpx.Response(404)

    async def go():
        client = _client(handler)
        assert await client.view_lead_ids("save_1") == ["lead_a", "lead_b"]
        assert await client.lead_in_view("save_1", "lead_b")
        assert not await client.lead_in_view("save_1", "lead_z")
        state["reject_filter"] = True
        assert await client.lead_in_view("save_1", "lead_a")
        assert client.membership_mode == "scan"
        await client.aclose()

    asyncio.run(go())


def test_phone_search_falls_back_to_lead_search():
    def handler(request):
        if request.url.path.endswith("/data/search/"):
            return httpx.Response(400, json={"error": "unsupported"})
        assert request.url.params["query"] == 'phone:"3125550100"'
        return httpx.Response(200, json={"data": [{"id": "lead_a"}]})

    async def go():
        client = _client(handler)
        ids = await client.find_lead_ids_by_phone("+13125550100")
        await client.aclose()
        return ids, client.phone_search_mode

    assert asyncio.run(go()) == (["lead_a"], "legacy")
