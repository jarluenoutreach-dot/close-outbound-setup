"""Fast dry preview of the reply-notification system: pulls Close's inbox
directly (no per-lead looping) and shows who has a pending, never-notified
reply, plus exactly what the Telegram notification + buttons would look
like. Sends nothing, writes nothing.
"""
import asyncio

from outbound.config import load_settings
from outbound.db import Db
from outbound.close_api import CloseClient
from outbound.eligibility import is_inbound_sms
from outbound.followup import STEPS


async def main():
    settings = load_settings()
    close = CloseClient(settings.close_api_key)
    db = Db(settings.db_path)
    try:
        # Close doesn't actually honor a direction filter here — fetch recent
        # SMS account-wide and filter for real inbound ones ourselves.
        recent = (await close._request("GET", "/activity/sms/", params={"_limit": 100})).get("data", [])
    finally:
        await close.aclose()

    inbox = [s for s in recent if is_inbound_sms(s) and s.get("local_phone") == settings.local_phone]
    print(f"{len(recent)} most recent SMS account-wide, {len(inbox)} inbound on our number ({settings.local_phone})\n")
    pending = []
    for sms in inbox:
        lead_id = sms.get("lead_id")
        rec = db.get(lead_id)
        if not rec or rec.get("status") != "sent":
            continue  # not a lead we texted (or not via this system)
        if sms.get("id") == rec.get("followup_notified_sms_id"):
            continue  # already notified
        pending.append((rec, sms))

    print(f"Pending, never-notified replies from leads we texted: {len(pending)}\n")
    for rec, sms in pending:
        step = rec.get("followup_step") or "opener"
        options = STEPS.get(step)
        print("=" * 60)
        print(f"💬 {rec.get('lead_name')} ({rec.get('phone')}) replied:")
        print(f'   "{(sms.get("text") or "").strip()}"')
        print(f"   [current step: {step}]")
        if options:
            print("   Buttons that would show:")
            for label, next_step, text in options:
                print(f"     [{label}] -> would send: \"{text[:80]}{'...' if len(text) > 80 else ''}\"")
        else:
            print("   No more scripted buttons for this step — would notify as plain text only.")
    if not pending:
        print("(none right now)")


asyncio.run(main())
