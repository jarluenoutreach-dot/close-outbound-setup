import asyncio
import datetime

from outbound.config import load_settings
from outbound.db import Db
from outbound.close_api import CloseClient
from outbound.timeutil import parse_dt, utcnow


async def main():
    settings = load_settings()
    close = CloseClient(settings.close_api_key)
    db = Db(settings.db_path)
    cutoff = utcnow() - datetime.timedelta(days=7)

    ids = await close.view_lead_ids(settings.smart_view_id)
    print(f"{len(ids)} leads currently in the Webinar Workflow view\n")

    untouched_recent, untouched_old, touched = [], [], 0
    try:
        for lead_id in ids:
            rec = db.get(lead_id)
            if rec:
                touched += 1
                continue
            lead = await close.get_lead(lead_id)
            created = parse_dt(lead["date_created"]) if lead and lead.get("date_created") else None
            if created and created >= cutoff:
                untouched_recent.append((lead.get("display_name"), created))
            else:
                untouched_old.append((lead.get("display_name"), created))
    finally:
        await close.aclose()

    print(f"Already touched (baseline/sent/error) — handled: {touched}")
    print(f"Not yet in local db, created >= 7 days ago cutoff ({cutoff.date()}), WOULD be checked by the live engine: {len(untouched_recent)}")
    for name, d in untouched_recent:
        print(f"  {name:<28} created {d.date() if d else 'unknown'}")
    print(f"Not yet in local db but older than 7 days: {len(untouched_old)}")
    for name, d in untouched_old:
        print(f"  {name:<28} created {d.date() if d else 'unknown'}")


asyncio.run(main())
