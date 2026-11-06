"""Un-baseline (remove the "leave alone" mark) in the REAL db, but only for
backlog leads that are BOTH eligible per the normal 8 rules AND have a
plausible real phone number (not a 555-xxxx/test pattern or an impossibly
short number).

Eligibility is computed the same safe way as preview_backlog.py (in-memory
snapshot, dry_run=True engine, never writes to Close). Only the final step —
deleting the baseline row for qualifying leads — touches the real db. The
already-running live service picks those up on its next sweep and sends them
for real using its own current rules.
"""
import asyncio
import re
import sys

from outbound.config import load_settings
from outbound.db import Db
from outbound.engine import Engine
from outbound.close_api import CloseClient


def plausible_number(phone: str) -> bool:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        full = re.sub(r"\D", "", phone or "")
        return 8 <= len(full) <= 15 and len(set(full)) > 1
    area, exch = digits[:3], digits[3:6]
    if area[0] in "01" or exch[0] in "01":
        return False
    if exch == "555":
        return False
    if len(set(digits)) == 1:
        return False
    if digits == "1234567890":
        return False
    return True


async def main():
    settings = load_settings()
    close = CloseClient(settings.close_api_key)
    snap = Db.snapshot(settings.db_path)

    baseline_ids = [r["lead_id"] for r in snap.rows("baseline")]
    for lead_id in baseline_ids:
        snap.conn.execute("DELETE FROM leads WHERE lead_id = ?", (lead_id,))

    engine = Engine(settings, snap, close, dry_run=True, ignore_stop=True)
    to_release, bad_number, ineligible = [], [], []
    try:
        for n, lead_id in enumerate(baseline_ids, 1):
            await engine.process_lead(lead_id, "dry-run", check_view=False)
            rec = snap.get(lead_id)
            if not rec:
                continue
            if rec["status"] not in ("queued", "scheduled"):
                ineligible.append(rec)
                continue
            if plausible_number(rec.get("phone")):
                to_release.append(rec)
            else:
                bad_number.append(rec)
            if n % 25 == 0:
                print(f"  …checked {n}/{len(baseline_ids)}", file=sys.stderr, flush=True)
    finally:
        await close.aclose()

    print(f"\nEligible + plausible number -> releasing from baseline: {len(to_release)}")
    print(f"Eligible but bad/fake-looking number -> staying baselined: {len(bad_number)}")
    print(f"Not eligible per the 8 rules -> staying baselined: {len(ineligible)}")

    # The ONLY write to the real db: remove the baseline mark for qualifying leads.
    real = Db(settings.db_path)
    for rec in to_release:
        real.conn.execute("DELETE FROM leads WHERE lead_id = ? AND status = 'baseline'", (rec["lead_id"],))
        real.log("baseline_released_for_backlog_send", lead_id=rec["lead_id"])
    print(f"\nReleased {len(to_release)} leads — the live service will pick them up on its next sweep.")

    print("\nReleased leads:")
    for r in to_release:
        print(f"  {r.get('lead_name',''):<28} {r.get('phone',''):<16} {r['lead_id']}")
    print("\nSkipped for bad/fake number:")
    for r in bad_number:
        print(f"  {r.get('lead_name',''):<28} {r.get('phone',''):<16} {r['lead_id']}")


asyncio.run(main())
