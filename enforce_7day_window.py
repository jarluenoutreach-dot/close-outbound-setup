"""Restrict the backlog release to leads created in the last 7 days.

The user wants a tighter window than the smart view's own 60-day cutoff: no
lead older than 7 days should get a first text, even if it's otherwise
eligible and has a valid-looking number. This re-checks every currently
non-baseline, not-yet-sent/errored lead's real date_created from Close and
restores baseline for anything older than 7 days.
"""
import asyncio

from outbound.config import load_settings
from outbound.db import Db
from outbound.close_api import CloseClient
from outbound.timeutil import parse_dt, utcnow


async def main():
    settings = load_settings()
    close = CloseClient(settings.close_api_key)
    db = Db(settings.db_path)
    cutoff = utcnow() - __import__("datetime").timedelta(days=7)

    rows = db.conn.execute(
        "SELECT lead_id, lead_name, phone, status FROM leads WHERE status NOT IN ('baseline','sent','error')"
    ).fetchall()
    print(f"{len(rows)} leads currently queued/held — checking real signup date (cutoff: {cutoff.date()})\n")

    restored, kept = [], []
    try:
        for r in rows:
            lead = await close.get_lead(r["lead_id"])
            created = parse_dt(lead["date_created"]) if lead and lead.get("date_created") else None
            if not created or created < cutoff:
                db.upsert(r["lead_id"], status="baseline", source="baseline",
                          skip_reason=f"restored to baseline: created {created.date() if created else 'unknown'}, older than the 7-day window")
                restored.append((r["lead_name"], created.date() if created else "unknown"))
            else:
                kept.append((r["lead_name"], created.date()))
    finally:
        await close.aclose()

    print(f"Restored to baseline (older than 7 days): {len(restored)}")
    for name, d in restored:
        print(f"  {name:<28} created {d}")
    print(f"\nWithin the last 7 days, still held: {len(kept)}")
    for name, d in kept:
        print(f"  {name:<28} created {d}")


asyncio.run(main())
