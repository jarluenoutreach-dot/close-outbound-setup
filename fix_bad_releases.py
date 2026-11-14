"""Cleanup pass after a too-loose phone filter let some bogus numbers through.

For every currently-non-baseline lead that was released by apply_backlog.py and
has NOT already sent/errored, re-check its phone with a strict US/CA E.164
filter (this sending number only supports a specific set of countries, per the
real "outside of the countries we allow" errors we just saw). Anything that
fails is restored to baseline (excluded again, permanently). Never touches
sent/errored rows — those are historical fact already.
"""
import re

from outbound.config import load_settings
from outbound.db import Db


def strict_us_ca_number(phone: str) -> bool:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return False
    area, exch = digits[:3], digits[3:6]
    if area[0] in "01" or exch[0] in "01":
        return False
    if exch == "555" or len(set(digits)) == 1 or digits == "1234567890":
        return False
    return True


settings = load_settings()
db = Db(settings.db_path)

rows = db.conn.execute(
    "SELECT lead_id, lead_name, phone, status FROM leads WHERE status NOT IN ('baseline','sent','error')"
).fetchall()
print(f"{len(rows)} non-baseline, not-yet-sent/errored leads to re-check\n")

restored, kept = [], []
for r in rows:
    if strict_us_ca_number(r["phone"]):
        kept.append(r)
    else:
        db.upsert(r["lead_id"], status="baseline", source="baseline",
                  skip_reason="restored to baseline: phone failed the stricter US/CA check after the first filter was too loose")
        restored.append(r)

print(f"Restored to baseline (bad number, excluded again): {len(restored)}")
for r in restored:
    print(f"  {r['lead_name']:<28} {r['phone']}")
print(f"\nStill held, phone looks genuinely valid: {len(kept)}")
for r in kept:
    print(f"  {r['lead_name']:<28} {r['phone']}")
