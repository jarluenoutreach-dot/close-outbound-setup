"""Minimal Telegram Bot API client (sendMessage + getUpdates)."""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger("outbound.telegram")


class Telegram:
    def __init__(self, token: str, chat_id: str, *, transport=None):
        self.token = token or ""
        self.chat_id = str(chat_id or "")
        self._http = httpx.AsyncClient(timeout=70, transport=transport)

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send(self, text: str, chat_id: str | None = None) -> bool:
        target = chat_id or self.chat_id
        if not (self.token and target):
            log.info("Telegram not configured; message not sent:\n%s", text)
            return False
        ok = True
        for start in range(0, len(text), 3900):
            chunk = text[start : start + 3900]
            try:
                resp = await self._http.post(
                    self._url("sendMessage"),
                    json={"chat_id": target, "text": chunk, "disable_web_page_preview": True},
                )
                if resp.status_code != 200:
                    log.error("Telegram sendMessage failed: %s %s", resp.status_code, resp.text[:200])
                    ok = False
            except httpx.HTTPError as exc:
                log.error("Telegram sendMessage failed: %r", exc)
                ok = False
        return ok

    async def get_updates(self, offset: int = 0, timeout: int = 50) -> list[dict]:
        if not self.token:
            return []
        try:
            resp = await self._http.get(self._url("getUpdates"), params={"offset": offset, "timeout": timeout})
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Telegram getUpdates failed: %r", exc)
            await asyncio.sleep(10)
            return []
        if not data.get("ok"):
            log.warning("Telegram getUpdates error: %s", data.get("description"))
            await asyncio.sleep(10)
            return []
        return data.get("result", [])

    async def get_me(self) -> dict:
        resp = await self._http.get(self._url("getMe"))
        return resp.json()
