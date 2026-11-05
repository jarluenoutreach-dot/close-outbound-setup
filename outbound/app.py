"""FastAPI app: the signed Close webhook endpoint plus the background loops."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .close_api import CloseClient
from .config import Settings
from .db import Db
from .engine import Engine
from .report import build_report
from .security import verify_signature
from .telegram import Telegram

log = logging.getLogger("outbound.app")

HELP = (
    "Commands:\n"
    "/report – first-text report for the past 7 days\n"
    "/status – queue, scheduled, sent and errors\n"
    "/stop – create the STOP file (nothing is sent or scheduled)\n"
    "/resume – delete the STOP file\n"
    "/chatid – show this chat's ID"
)


async def run_every(name: str, seconds: float, fn, initial_delay: float = 0) -> None:
    await asyncio.sleep(initial_delay)
    while True:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s loop failed; continuing", name)
        await asyncio.sleep(seconds)


async def handle_command(engine: Engine, text: str) -> str:
    command = text.split()[0].split("@")[0].lower() if text else ""
    stop_file = engine.s.stop_file
    if command == "/report":
        return await build_report(engine, engine.clock())
    if command == "/status":
        return engine.status_text()
    if command == "/stop":
        stop_file.write_text("Created from Telegram /stop. Delete this file or send /resume to resume.\n")
        log.warning("STOP file created from Telegram")
        return "🛑 Stopped. Nothing will be sent or scheduled. Send /resume to start again."
    if command == "/resume":
        if stop_file.exists():
            stop_file.unlink()
            log.warning("STOP file removed from Telegram")
            return "✅ Resumed. Queued leads are re-checked before anything is sent."
        return "Already running (no STOP file)."
    return HELP


async def telegram_loop(engine: Engine) -> None:
    tg = engine.telegram
    if not tg.token:
        log.info("Telegram bot token not set; bot commands disabled")
        return
    offset = int(engine.db.kv_get("telegram_offset") or 0)
    while True:
        try:
            for update in await tg.get_updates(offset):
                offset = update["update_id"] + 1
                engine.db.kv_set("telegram_offset", str(offset))
                message = update.get("message") or update.get("edited_message") or {}
                chat_id = str((message.get("chat") or {}).get("id", ""))
                text = (message.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                if text.split()[0].split("@")[0].lower() == "/chatid":
                    await tg.send(f"This chat's ID is {chat_id}", chat_id=chat_id)
                    continue
                if chat_id != tg.chat_id:
                    log.warning("Ignored Telegram command from unknown chat %s", chat_id)
                    continue
                await tg.send(await handle_command(engine, text))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Telegram loop error")
            await asyncio.sleep(10)


def create_app(settings: Settings, *, dry_run: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Db(settings.dry_run_db_path if dry_run else settings.db_path)
        close = CloseClient(settings.close_api_key)
        tg = Telegram(settings.telegram_bot_token, settings.telegram_chat_id)
        engine = Engine(settings, db, close, tg, dry_run=dry_run)
        app.state.engine = engine
        if not settings.webhook_signature_key:
            log.error("CLOSE_WEBHOOK_SIGNATURE_KEY is not set: every webhook will be rejected. Run `outbound setup-webhook`.")
        loops = [
            run_every("sender", 1, engine.sender_tick),
            run_every("verify", 15, engine.verify_tick),
            run_every("morning-check", 15, engine.morning_tick),
            run_every("sweep", settings.sweep_interval_seconds, engine.sweep, initial_delay=20),
            run_every("weekly-report", 60, engine.weekly_tick),
            telegram_loop(engine),
        ]
        tasks = [asyncio.create_task(loop) for loop in loops]
        log.info("Service started%s", " in DRY RUN mode" if dry_run else "")
        await tg.send(f"Close outbound service started{' (DRY RUN — nothing will be sent)' if dry_run else ''}.\n{engine.status_text()}")
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, *engine._tasks, return_exceptions=True)
            await close.aclose()
            await tg.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/webhooks/close")
    async def close_webhook(request: Request):
        body = await request.body()
        ok = verify_signature(
            settings.webhook_signature_key,
            body,
            request.headers.get("close-sig-hash"),
            request.headers.get("close-sig-timestamp"),
        )
        if not ok:
            log.warning("Rejected unsigned/invalid webhook from %s", request.client.host if request.client else "?")
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        try:
            payload = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        event = payload.get("event") or {}
        log.debug("Webhook %s.%s %s", event.get("object_type"), event.get("action"), event.get("object_id"))
        request.app.state.engine.dispatch_webhook(event)
        return {"ok": True}

    @app.get("/health")
    async def health(request: Request):
        engine: Engine = request.app.state.engine
        counts = engine.db.status_counts()
        return {"ok": True, "dry_run": engine.dry_run, "stopped": settings.stop_file.exists(),
                "queued": counts.get("queued", 0), "scheduled": counts.get("scheduled", 0)}

    return app
