"""Small async client for the Close REST API (https://api.close.com/api/v1).

Auth is HTTP Basic with the API key as the username and an empty password.
Reads are retried on rate limits, 5xx and network errors. Creating an SMS is never retried
after a network error or 5xx (it might already have gone out); 429 is safe to retry.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import httpx

log = logging.getLogger("outbound.close")

BASE_URL = "https://api.close.com/api/v1"
LEAD_FIELDS = "id,display_name,name,status_id,status_label,contacts,date_created"
_LEAD_ONLY = {"type": "object_type", "object_type": "lead"}


class CloseError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Close API {status}: {message}" if status else f"Close API: {message}")
        self.status = status
        self.message = message


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or resp.reason_phrase or "")[:300]
    if isinstance(body, dict):
        parts = []
        err = body.get("error")
        if err:
            parts.append(err.get("message", json.dumps(err)) if isinstance(err, dict) else str(err))
        if body.get("errors"):
            parts.append("; ".join(map(str, body["errors"])))
        if body.get("field-errors"):
            parts.append(json.dumps(body["field-errors"]))
        if parts:
            return " | ".join(parts)[:500]
    return json.dumps(body)[:500]


def _retry_after(resp: httpx.Response) -> float:
    header = resp.headers.get("retry-after")
    if header:
        try:
            return min(float(header), 60.0)
        except ValueError:
            pass
    try:
        err = resp.json().get("error")
        if isinstance(err, dict) and err.get("rate_reset") is not None:
            return min(float(err["rate_reset"]) + 0.25, 60.0)
    except (ValueError, AttributeError):
        pass
    return 2.0


def phone_query(phone: str) -> dict:
    """Advanced Filtering query: leads with a contact phone matching `phone`."""
    return {
        "type": "and",
        "queries": [
            _LEAD_ONLY,
            {
                "type": "has_related",
                "this_object_type": "lead",
                "related_object_type": "contact",
                "related_query": {
                    "type": "has_related",
                    "this_object_type": "contact",
                    "related_object_type": "contact_phone",
                    "related_query": {
                        "type": "field_condition",
                        "field": {"type": "regular_field", "object_type": "contact_phone", "field_name": "phone"},
                        "condition": {"type": "text", "mode": "phrase", "value": phone},
                    },
                },
            },
        ],
    }


def id_query(lead_id: str) -> dict:
    return {
        "type": "field_condition",
        "field": {"type": "regular_field", "object_type": "lead", "field_name": "id"},
        "condition": {"type": "term", "values": [lead_id]},
    }


class CloseClient:
    def __init__(self, api_key: str, *, base_url: str = BASE_URL, timeout: float = 30.0, transport=None):
        if not api_key:
            raise ValueError("CLOSE_API_KEY is empty — set it in .env")
        self._http = httpx.AsyncClient(
            base_url=base_url,
            auth=(api_key, ""),
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "close-outbound-setup/1.0"},
        )
        self._view_query_cache: dict[str, tuple[float, dict]] = {}
        self._view_ids_cache: dict[str, tuple[float, list[str]]] = {}
        # Fallback modes, switched automatically if Close rejects the preferred query shape.
        self.membership_mode = "filter"
        self.phone_search_mode = "advanced"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method, path, *, params=None, json_body=None, retry=True, allow_404=False):
        attempts = 5
        for attempt in range(1, attempts + 1):
            try:
                resp = await self._http.request(method, path, params=params, json=json_body)
            except httpx.TransportError as exc:
                if not retry or attempt == attempts:
                    raise CloseError(0, f"network error: {exc!r}") from exc
                await asyncio.sleep(min(2**attempt, 30))
                continue
            if resp.status_code == 429:
                if attempt == attempts:
                    raise CloseError(429, "rate limited")
                wait = _retry_after(resp)
                log.info("Close rate limit hit, waiting %.1fs", wait)
                await asyncio.sleep(wait)
                continue
            if resp.status_code >= 500 and retry and attempt < attempts:
                await asyncio.sleep(min(2**attempt, 30))
                continue
            if resp.status_code == 404 and allow_404:
                return None
            if resp.status_code >= 400:
                raise CloseError(resp.status_code, _error_message(resp))
            if not resp.content:
                return {}
            return resp.json()
        raise CloseError(0, "request failed")

    # ---- basics ------------------------------------------------------------
    async def me(self) -> dict:
        return await self._request("GET", "/me/")

    async def phone_numbers(self) -> list[dict]:
        data = await self._request("GET", "/phone_number/", params={"_limit": 100})
        return data.get("data", [])

    async def get_lead(self, lead_id: str) -> dict | None:
        return await self._request("GET", f"/lead/{lead_id}/", params={"_fields": LEAD_FIELDS}, allow_404=True)

    async def list_activities(self, kind: str, lead_id: str) -> list[dict]:
        """All activities of one kind for a lead: 'sms', 'call', 'meeting', 'status_change/lead'."""
        out: list[dict] = []
        skip = 0
        while True:
            data = await self._request(
                "GET", f"/activity/{kind}/", params={"lead_id": lead_id, "_limit": 100, "_skip": skip}
            )
            batch = data.get("data", [])
            out.extend(batch)
            if not data.get("has_more") or not batch or skip > 5000:
                return out
            skip += len(batch)

    # ---- SMS / notes / tasks ----------------------------------------------
    async def get_sms(self, sms_id: str) -> dict | None:
        return await self._request("GET", f"/activity/sms/{sms_id}/", allow_404=True)

    async def create_sms(self, payload: dict) -> dict:
        return await self._request("POST", "/activity/sms/", json_body=payload, retry=False)

    async def delete_sms(self, sms_id: str) -> None:
        await self._request("DELETE", f"/activity/sms/{sms_id}/", allow_404=True)

    async def create_note(self, lead_id: str, text: str) -> dict:
        return await self._request("POST", "/activity/note/", json_body={"lead_id": lead_id, "note": text}, retry=False)

    async def create_task(self, lead_id: str, text: str, *, assigned_to: str, date: str) -> dict:
        body = {"_type": "lead", "lead_id": lead_id, "assigned_to": assigned_to, "text": text, "date": date}
        return await self._request("POST", "/task/", json_body=body, retry=False)

    # ---- smart view --------------------------------------------------------
    async def _search(self, body: dict) -> dict:
        return await self._request("POST", "/data/search/", json_body=body)

    async def view_query(self, view_id: str) -> dict:
        cached = self._view_query_cache.get(view_id)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        view = await self._request("GET", f"/saved_search/{view_id}/")
        s_query = (view or {}).get("s_query") or {}
        if not s_query.get("query"):
            raise CloseError(0, f"smart view {view_id} has no Advanced Filtering query (s_query)")
        query = {"type": "and", "queries": [_LEAD_ONLY, s_query["query"]]}
        self._view_query_cache[view_id] = (time.monotonic() + 300, query)
        return query

    async def view_name(self, view_id: str) -> str:
        view = await self._request("GET", f"/saved_search/{view_id}/")
        return (view or {}).get("name", "")

    async def view_lead_ids(self, view_id: str) -> list[str]:
        query = await self.view_query(view_id)
        ids: list[str] = []
        cursor = None
        while True:
            body = {"query": query, "_fields": {"lead": ["id"]}, "_limit": 200}
            if cursor:
                body["_cursor"] = cursor
            data = await self._search(body)
            batch = data.get("data", [])
            ids.extend(d["id"] for d in batch if d.get("id"))
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
        self._view_ids_cache[view_id] = (time.monotonic(), ids)
        return ids

    async def lead_in_view(self, view_id: str, lead_id: str) -> bool:
        if self.membership_mode == "filter":
            query = await self.view_query(view_id)
            body = {
                "query": {"type": "and", "queries": [query, id_query(lead_id)]},
                "_fields": {"lead": ["id"]},
                "_limit": 5,
            }
            try:
                data = await self._search(body)
                return any(d.get("id") == lead_id for d in data.get("data", []))
            except CloseError as exc:
                if exc.status != 400:
                    raise
                log.warning("Close rejected the lead-id filter (%s); checking membership by listing the view", exc)
                self.membership_mode = "scan"
        cached = self._view_ids_cache.get(view_id)
        if cached and time.monotonic() - cached[0] < 15:
            ids = cached[1]
        else:
            ids = await self.view_lead_ids(view_id)
        return lead_id in ids

    # ---- duplicates --------------------------------------------------------
    async def find_lead_ids_by_phone(self, phone: str) -> list[str]:
        if self.phone_search_mode == "advanced":
            try:
                data = await self._search({"query": phone_query(phone), "_fields": {"lead": ["id"]}, "_limit": 50})
                return [d["id"] for d in data.get("data", []) if d.get("id")]
            except CloseError as exc:
                if exc.status != 400:
                    raise
                log.warning("Close rejected the phone query (%s); using lead search instead", exc)
                self.phone_search_mode = "legacy"
        digits = re.sub(r"\D", "", phone)[-10:]
        data = await self._request("GET", "/lead/", params={"query": f'phone:"{digits}"', "_fields": "id", "_limit": 50})
        return [d["id"] for d in data.get("data", []) if d.get("id")]

    # ---- webhooks ----------------------------------------------------------
    async def list_webhooks(self) -> list[dict]:
        data = await self._request("GET", "/webhook/")
        return data.get("data", [])

    async def create_webhook(self, url: str, events: list[dict]) -> dict:
        return await self._request("POST", "/webhook/", json_body={"url": url, "events": events}, retry=False)

    async def delete_webhook(self, webhook_id: str) -> None:
        await self._request("DELETE", f"/webhook/{webhook_id}/", allow_404=True)
