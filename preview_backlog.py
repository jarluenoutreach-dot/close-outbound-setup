"""One-off, read-only preview: what would happen to the 200 baseline-excluded
leads if the backlog were run through the normal 8 eligibility rules instead
of being blanket-excluded.

Safe by construction: works on an in-memory COPY of the real db (Db.snapshot),
runs the engine in dry_run=True (never writes to Close), and never touches the
real sqlite file on disk. Prints the same breakdown as `outbound dry-run`.
"""
import asyncio
import sys

from outbound.config import load_settings
from outbound.db import Db
from outbound.engine import Engine
from outbound.close_api import CloseClient


async def main():
    settings = load_settings()
    close = CloseClient(settings.close_api_key)
    db = Db.snapshot(settings.db_path)

    baseline_ids = [r["lead_id"] for r in db.rows("baseline")]
    print(f"{len(baseline_ids)} leads currently excluded by baseline\n")

    # Simulate "baseline never ran" for just these leads, in the SNAPSHOT only.
    for lead_id in baseline_ids:
        db.conn.execute("DELETE FROM leads WHERE lead_id = ?", (lead_id,))

    engine = Engine(settings, db, close, dry_run=True, ignore_stop=True)
    send, sched, skip, failed = [], [], [], []
    try:
        for n, lead_id in enumerate(baseline_ids, 1):
            await engine.process_lead(lead_id, "dry-run", check_view=False)
            rec = db.get(lead_id)
            if not rec:
                failed.append(lead_id)
            elif rec["status"] == "queued":
                send.append(rec)
            elif rec["status"] == "scheduled":
                sched.append(rec)
            elif rec["status"] == "skipped":
                skip.append(rec)
            if n % 25 == 0:
                print(f"  …checked {n}/{len(baseline_ids)}", file=sys.stderr, flush=True)
    finally:
        await close.aclose()

    def who(r):
        return f"{(r.get('lead_name') or '')[:28]:<28} {r.get('phone') or '':<13} {r.get('timezone') or '':<20}"

    print(f"\nWOULD TEXT NOW ({len(send)}):")
    for r in send:
        print(f"  {who(r)} v{r['message_version']}  {r['lead_id']}")
    print(f"\nWOULD SCHEDULE FOR THE MORNING ({len(sched)}):")
    for r in sched:
        print(f"  {who(r)} v{r['message_version']}  {r['scheduled_for']}  {r['lead_id']}")
    by_rule = {}
    for r in skip:
        by_rule.setdefault(r["skip_rule"], []).append(r)
    print(f"\nSKIPPED ({len(skip)}):")
    for rule, rows in sorted(by_rule.items()):
        print(f"  Rule {rule} ({len(rows)}): {rows[0]['skip_reason']}")
    if failed:
        print(f"\nCOULD NOT CHECK ({len(failed)}): {failed[:10]}")
    print("\nNothing was sent, scheduled, or written to Close. Pure preview.")


asyncio.run(main())
