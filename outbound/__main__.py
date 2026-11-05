"""Command line: `python -m outbound <command>` (or `outbound <command>` on the server)."""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import logging.handlers
import sys
from datetime import timedelta

from .close_api import CloseClient, CloseError
from .config import load_settings, update_env_file
from .db import SETTLED_STATUSES, Db
from .eligibility import RULE_NAMES, first_phone, normalize_phone
from .engine import Engine
from .report import build_report
from .telegram import Telegram
from .timeutil import iso, parse_dt, utcnow
from .timezones import CENTRAL, zones_for_phone

WEBHOOK_EVENTS = [
    {"object_type": "lead", "action": "created"},
    {"object_type": "lead", "action": "updated"},
    {"object_type": "activity.sms", "action": "updated"},
]


def setup_logging(settings, *, verbose: bool = False, to_file: bool = True) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    if to_file:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            settings.log_dir / "outbound.log", maxBytes=10_000_000, backupCount=10
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _need_close(settings) -> CloseClient:
    if not settings.close_api_key:
        sys.exit("CLOSE_API_KEY is missing from .env")
    return CloseClient(settings.close_api_key)


# --------------------------------------------------------------------- serve
def cmd_serve(args, settings) -> None:
    import uvicorn

    from .app import create_app

    if not settings.close_api_key:
        sys.exit("CLOSE_API_KEY is missing from .env")
    setup_logging(settings, verbose=args.verbose)
    uvicorn.run(
        create_app(settings, dry_run=args.dry_run),
        host=args.host,
        port=args.port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        workers=1,  # exactly one process: one send worker
    )


# ------------------------------------------------------------------- dry run
async def _dry_run(args, settings) -> None:
    close = _need_close(settings)
    db = Db.snapshot(settings.db_path)
    engine = Engine(settings, db, close, dry_run=True, ignore_stop=True)
    logging.getLogger("outbound").setLevel(logging.WARNING)
    now = engine.clock()
    try:
        ids = await close.view_lead_ids(settings.smart_view_id)
        if args.limit:
            ids = ids[: args.limit]
        print(f"DRY RUN — {len(ids)} leads in smart view {settings.smart_view_name} "
              f"({now.astimezone(CENTRAL).strftime('%a %b %d %I:%M %p %Z')})\n", flush=True)
        handled, failed = [], []
        for n, lead_id in enumerate(ids, 1):
            rec = db.get(lead_id)
            if rec and rec["status"] in SETTLED_STATUSES:
                handled.append(rec)
                continue
            decision = await engine.process_lead(lead_id, "dry-run", check_view=False)
            if decision is None and not (db.get(lead_id) or {}).get("status"):
                failed.append(lead_id)
            if n % 25 == 0:
                print(f"  …checked {n}/{len(ids)}", file=sys.stderr, flush=True)
    finally:
        await close.aclose()

    handled_ids = {r["lead_id"] for r in handled}
    rows = [r for r in (db.get(i) for i in ids if i not in handled_ids) if r]
    send = [r for r in rows if r["status"] == "queued"]
    sched = [r for r in rows if r["status"] == "scheduled"]
    skip = [r for r in rows if r["status"] == "skipped"]

    def who(r):
        return f"{(r.get('lead_name') or '')[:28]:<28} {r.get('phone') or '':<13} {r.get('timezone') or '':<20}"

    print(f"WOULD TEXT NOW ({len(send)}):")
    for r in send:
        print(f"  {who(r)} v{r['message_version']}  {r['lead_id']}")
    if send:
        gap_minutes = (len(send) - 1) * settings.min_seconds_between_sends / 60
        cap_minutes = ((len(send) - 1) // settings.max_sends_per_hour) * 60
        print(f"  → {settings.min_seconds_between_sends}s apart, max {settings.max_sends_per_hour}/hour: "
              f"about {max(gap_minutes, cap_minutes):.0f} min to send them all")
        print(f"  Example: {send[0]['message_text']}")
    print(f"\nWOULD SCHEDULE FOR THE MORNING ({len(sched)}):")
    for r in sched:
        when = parse_dt(r["scheduled_for"])
        zone = zones_for_phone(r.get("phone"))[0]
        print(f"  {who(r)} v{r['message_version']}  {when.astimezone(zone).strftime('%a %I:%M:%S %p %Z')}  {r['lead_id']}")
    print(f"\nSKIPPED ({len(skip)}):")
    by_rule: dict[str, list] = {}
    for r in skip:
        by_rule.setdefault(r["skip_rule"], []).append(r)
    for rule in sorted(by_rule, key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else 0, x)):
        print(f"  Rule {rule} — {RULE_NAMES.get(rule, '')} ({len(by_rule[rule])})")
        for r in by_rule[rule]:
            print(f"    {who(r)} {r['skip_reason']}")
    if handled:
        kinds = sorted({r["status"] for r in handled})
        print(f"\nLEFT ALONE ({len(handled)}): already {'/'.join(kinds)} in the local database")
    if failed:
        print(f"\nCOULD NOT CHECK ({len(failed)}) — Close API errors, see log: {', '.join(failed[:10])}")

    out = settings.data_dir / f"dry_run_{now.astimezone(CENTRAL).strftime('%Y%m%d_%H%M')}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["lead_id", "lead_name", "phone", "timezone", "decision", "version", "scheduled_for_utc",
                         "rule", "reason", "message"])
        for r in rows:
            decision = {"queued": "send_now", "scheduled": "schedule", "skipped": "skip"}.get(r["status"], r["status"])
            writer.writerow([r["lead_id"], r.get("lead_name"), r.get("phone"), r.get("timezone"), decision,
                             r.get("message_version") if decision != "skip" else "", r.get("scheduled_for") or "",
                             r.get("skip_rule") or "", r.get("skip_reason") or "",
                             r.get("message_text") if decision != "skip" else ""])
    print(f"\nFull results saved to {out}")
    print("Nothing was sent, scheduled, or written to Close.")


# -------------------------------------------------------------------- doctor
async def _doctor(args, settings) -> None:
    close = _need_close(settings)
    problems = 0

    def ok(msg):
        print(f"  PASS  {msg}")

    def bad(msg):
        nonlocal problems
        problems += 1
        print(f"  FAIL  {msg}")

    try:
        print("Close API")
        me = await close.me()
        if me.get("id") == settings.my_user_id:
            ok(f"API key belongs to {me.get('first_name')} {me.get('last_name')} ({me['id']})")
        else:
            bad(f"API key belongs to {me.get('id')}, not MY_USER_ID {settings.my_user_id} — texts would be sent as that user")

        try:
            numbers = [p.get("number") for p in await close.phone_numbers()]
            (ok if settings.local_phone in numbers else bad)(f"sending number {settings.local_phone} {'found' if settings.local_phone in numbers else 'NOT found'} in Close")
        except CloseError as exc:
            print(f"  WARN  could not list phone numbers: {exc}")

        print(f"\nSmart view {settings.smart_view_id}")
        name = await close.view_name(settings.smart_view_id)
        (ok if name else bad)(f"name: {name!r}")
        ids = await close.view_lead_ids(settings.smart_view_id)
        ok(f"{len(ids)} leads in the view")
        if ids:
            for lead_id in {ids[0], ids[-1]}:
                inside = await close.lead_in_view(settings.smart_view_id, lead_id)
                (ok if inside else bad)(f"membership check says {lead_id} is in the view (mode: {close.membership_mode})")
            others = await close._request("GET", "/lead/", params={"_limit": 50, "_fields": "id"})
            outside = next((d["id"] for d in others.get("data", []) if d["id"] not in set(ids)), None)
            if outside:
                inside = await close.lead_in_view(settings.smart_view_id, outside)
                (bad if inside else ok)(f"membership check says {outside} is NOT in the view")

            print("\nLead data (first lead in the view)")
            lead = await close.get_lead(ids[0])
            ok(f"{lead.get('display_name')!r}, status {lead.get('status_label')!r}, {len(lead.get('contacts') or [])} contacts")
            for kind in ("sms", "call", "meeting", "status_change/lead"):
                acts = await close.list_activities(kind, ids[0])
                ok(f"{kind}: {len(acts)} activities")
            _, phone = first_phone(lead)
            phone_lead_id = ids[0]
            if not phone:
                for other_id in ids[1:20]:
                    other = await close.get_lead(other_id)
                    _, phone = first_phone(other or {})
                    if phone:
                        phone_lead_id = other_id
                        break
            if phone:
                found = await close.find_lead_ids_by_phone(normalize_phone(phone))
                (ok if phone_lead_id in found else bad)(
                    f"duplicate search for {normalize_phone(phone)} finds its own lead ({len(found)} results, mode: {close.phone_search_mode})")
            else:
                print("  WARN  no lead with a phone found to test the duplicate search")

        print("\nWebhook")
        hooks = await close.list_webhooks()
        mine = [h for h in hooks if h.get("id") == settings.webhook_id] if settings.webhook_id else []
        (ok if settings.webhook_signature_key else bad)("CLOSE_WEBHOOK_SIGNATURE_KEY is set" if settings.webhook_signature_key else "CLOSE_WEBHOOK_SIGNATURE_KEY missing — run setup-webhook")
        if mine:
            ok(f"subscription {mine[0]['id']} → {mine[0].get('url')} ({mine[0].get('status', 'active')})")
        else:
            bad("no webhook subscription saved in .env (CLOSE_WEBHOOK_ID) — run setup-webhook")
    except CloseError as exc:
        bad(str(exc))
    finally:
        await close.aclose()

    print("\nTelegram")
    tg = Telegram(settings.telegram_bot_token, settings.telegram_chat_id)
    if not settings.telegram_bot_token:
        bad("TELEGRAM_BOT_TOKEN missing")
    else:
        info = await tg.get_me()
        (ok if info.get("ok") else bad)(f"bot @{info.get('result', {}).get('username')}" if info.get("ok") else f"bot token rejected: {info}")
        if settings.telegram_chat_id and info.get("ok"):
            sent = await tg.send("✅ Test message from the Close outbound service (doctor).")
            (ok if sent else bad)(f"test message to chat {settings.telegram_chat_id}")
        elif not settings.telegram_chat_id:
            bad("TELEGRAM_CHAT_ID missing — run `outbound telegram-chat-id`")
    await tg.aclose()

    print(f"\nKill switch: {'STOP file PRESENT (nothing will send)' if settings.stop_file.exists() else 'no STOP file'}")
    print(f"\n{'All checks passed.' if not problems else f'{problems} problem(s) found.'}")
    if problems:
        sys.exit(1)


# ------------------------------------------------------------------ webhooks
async def _setup_webhook(args, settings) -> None:
    close = _need_close(settings)
    try:
        existing = [h for h in await close.list_webhooks() if h.get("url") == args.url]
        if existing and not args.replace:
            sys.exit(f"A webhook for {args.url} already exists ({existing[0]['id']}). Re-run with --replace to recreate it.")
        for hook in existing:
            await close.delete_webhook(hook["id"])
            print(f"Deleted old subscription {hook['id']}")
        sub = await close.create_webhook(args.url, WEBHOOK_EVENTS)
    finally:
        await close.aclose()
    key = sub.get("signature_key")
    if not key:
        sys.exit(f"Close did not return a signature key: {sub}")
    update_env_file(settings.env_file, {"CLOSE_WEBHOOK_SIGNATURE_KEY": key, "CLOSE_WEBHOOK_ID": sub["id"]})
    print(f"Created webhook {sub['id']} → {args.url}")
    print("Events: lead created, lead updated, SMS activity updated")
    print(f"Signature key saved to {settings.env_file}")
    print("Restart the service so it picks up the key:  sudo systemctl restart close-outbound")


async def _list_webhooks(args, settings) -> None:
    close = _need_close(settings)
    try:
        for hook in await close.list_webhooks():
            events = ", ".join(f"{e.get('object_type')}.{e.get('action')}" for e in hook.get("events", []))
            print(f"{hook['id']}  {hook.get('status', '')}  {hook.get('url')}  [{events}]")
    finally:
        await close.aclose()


async def _delete_webhook(args, settings) -> None:
    close = _need_close(settings)
    try:
        await close.delete_webhook(args.webhook_id)
        print(f"Deleted {args.webhook_id}")
    finally:
        await close.aclose()


# -------------------------------------------------------------------- report
async def _report(args, settings) -> None:
    close = _need_close(settings)
    db = Db(settings.dry_run_db_path if args.dry_run_data else settings.db_path)
    engine = Engine(settings, db, close, Telegram(settings.telegram_bot_token, settings.telegram_chat_id),
                    dry_run=args.dry_run_data)
    try:
        text = await build_report(engine, utcnow(), days=args.days)
    finally:
        await close.aclose()
    print(text)
    if args.send:
        print("\nSent to Telegram." if await engine.telegram.send(text) else "\nTelegram send failed (see above).")
    await engine.telegram.aclose()


async def _telegram_chat_id(args, settings) -> None:
    if not settings.telegram_bot_token:
        sys.exit("Put TELEGRAM_BOT_TOKEN in .env first.")
    tg = Telegram(settings.telegram_bot_token, "")
    updates = await tg.get_updates(0, timeout=0)
    await tg.aclose()
    chats = {}
    for update in updates:
        chat = (update.get("message") or update.get("edited_message") or {}).get("chat") or {}
        if chat:
            chats[chat["id"]] = chat
    if not chats:
        print("No messages found. Open your bot in Telegram, send it any message (e.g. 'hi'), then run this again.")
        print("(If the service is already running it consumes messages — send /chatid to the bot instead.)")
        return
    for chat_id, chat in chats.items():
        name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
        print(f"TELEGRAM_CHAT_ID={chat_id}    ({chat.get('type')}: {name} @{chat.get('username', '')})")


async def _baseline(args, settings) -> None:
    db = Db(settings.db_path)
    if args.undo:
        rows = db.rows("baseline")
        for r in rows:
            db.conn.execute("DELETE FROM leads WHERE lead_id = ? AND status = 'baseline'", (r["lead_id"],))
        print(f"Removed the baseline mark from {len(rows)} leads; the sweep will check them again.")
        return
    close = _need_close(settings)
    try:
        ids = await close.view_lead_ids(settings.smart_view_id)
    finally:
        await close.aclose()
    targets = [i for i in ids if (db.get(i) or {}).get("status", "skipped") == "skipped"]
    if not args.yes:
        sys.exit(f"{len(targets)} of {len(ids)} leads currently in {settings.smart_view_name} would be marked as "
                 "baseline and never auto-texted (only leads that enter the view later get texted). "
                 "Re-run with --yes to do it.")
    for lead_id in targets:
        db.upsert(lead_id, status="baseline", source="baseline",
                  skip_reason="already in the smart view before go-live (outbound baseline)")
        db.log("baseline", lead_id=lead_id)
    print(f"Marked {len(targets)} existing leads as baseline. Undo with `outbound baseline --undo`.")


async def _cancel_scheduled(args, settings) -> None:
    db = Db(settings.db_path)
    rows = db.rows("scheduled")
    if not rows:
        print("No scheduled texts.")
        return
    if not args.yes:
        for r in rows:
            print(f"  {r.get('lead_name')}  {r.get('phone')}  {r.get('scheduled_for')}  sms={r.get('sms_activity_id')}")
        sys.exit(f"{len(rows)} scheduled texts would be deleted from Close. Re-run with --yes to do it.")
    close = _need_close(settings)
    try:
        for r in rows:
            if r.get("sms_activity_id"):
                await close.delete_sms(r["sms_activity_id"])
            db.upsert(r["lead_id"], status="skipped", skip_rule="manual", skip_reason="canceled with cancel-scheduled")
            db.log("unscheduled", lead_id=r["lead_id"], rule="manual", detail="cancel-scheduled")
            print(f"Deleted scheduled text for {r.get('lead_name')} ({r['lead_id']})")
    finally:
        await close.aclose()


def cmd_status(args, settings) -> None:
    db = Db(settings.db_path)
    engine = Engine(settings, db, close=None)  # type: ignore[arg-type]
    print(engine.status_text())
    queued = db.rows("queued")
    if queued:
        print(f"\nQueued: {', '.join((r.get('lead_name') or r['lead_id']) for r in queued[:20])}")
    upcoming = sorted(db.rows("scheduled"), key=lambda r: r.get("scheduled_for") or "")
    for r in upcoming[:20]:
        when = parse_dt(r["scheduled_for"]).astimezone(CENTRAL).strftime("%a %I:%M:%S %p %Z")
        print(f"  scheduled {when}: {r.get('lead_name')} {r.get('phone')} v{r.get('message_version')}")
    errors = [r for r in db.rows("error") if (r.get("error_at") or "") >= iso(utcnow() - timedelta(days=7))]
    for r in errors:
        print(f"  error {r.get('error_at')}: {r.get('lead_name')} {r.get('phone')}: {r.get('error_message')}")


def cmd_stop(args, settings) -> None:
    settings.stop_file.write_text("Created with `outbound stop`. Delete this file or run `outbound resume` to resume.\n")
    print(f"STOP file created at {settings.stop_file}. Nothing will be sent or scheduled.")


def cmd_resume(args, settings) -> None:
    if settings.stop_file.exists():
        settings.stop_file.unlink()
        print("STOP file removed. Sending resumes (every queued lead is re-checked first).")
    else:
        print("No STOP file; already running.")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="outbound", description="Close CRM first-text service")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the web server + workers (systemd runs this)")
    p.add_argument("--dry-run", action="store_true", help="do everything except send/schedule or write to Close")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("dry-run", help="check every lead in the smart view now and print who would be texted")
    p.add_argument("--limit", type=int, default=0, help="only check the first N leads")
    p.set_defaults(func=lambda a, s: asyncio.run(_dry_run(a, s)))

    p = sub.add_parser("doctor", help="test the Close API key, smart view, searches, webhook and Telegram")
    p.set_defaults(func=lambda a, s: asyncio.run(_doctor(a, s)))

    p = sub.add_parser("setup-webhook", help="create the Close webhook subscription and save its signature key")
    p.add_argument("--url", required=True, help="e.g. https://texts.example.com/webhooks/close")
    p.add_argument("--replace", action="store_true", help="delete and recreate an existing subscription for this URL")
    p.set_defaults(func=lambda a, s: asyncio.run(_setup_webhook(a, s)))

    p = sub.add_parser("list-webhooks", help="list Close webhook subscriptions")
    p.set_defaults(func=lambda a, s: asyncio.run(_list_webhooks(a, s)))

    p = sub.add_parser("delete-webhook", help="delete a Close webhook subscription")
    p.add_argument("webhook_id")
    p.set_defaults(func=lambda a, s: asyncio.run(_delete_webhook(a, s)))

    p = sub.add_parser("report", help="print the first-text report (past 7 days)")
    p.add_argument("--send", action="store_true", help="also send it to Telegram")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--dry-run-data", action="store_true", help="report on the `serve --dry-run` database")
    p.set_defaults(func=lambda a, s: asyncio.run(_report(a, s)))

    p = sub.add_parser("telegram-chat-id", help="print your Telegram chat ID (message the bot first)")
    p.set_defaults(func=lambda a, s: asyncio.run(_telegram_chat_id(a, s)))

    p = sub.add_parser("baseline", help="mark every lead already in the smart view so only NEW leads get texted")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--undo", action="store_true", help="remove the baseline marks")
    p.set_defaults(func=lambda a, s: asyncio.run(_baseline(a, s)))

    p = sub.add_parser("cancel-scheduled", help="delete every scheduled first text from Close")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=lambda a, s: asyncio.run(_cancel_scheduled(a, s)))

    sub.add_parser("status", help="queue, scheduled texts and recent errors").set_defaults(func=cmd_status)
    sub.add_parser("stop", help="create the STOP file (kill switch)").set_defaults(func=cmd_stop)
    sub.add_parser("resume", help="remove the STOP file").set_defaults(func=cmd_resume)

    args = parser.parse_args(argv)
    settings = load_settings()
    if args.command not in ("serve",):
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args.func(args, settings)


if __name__ == "__main__":
    main()
