"""Weekly report: results per message version for leads texted in the past 7 days."""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta

from .close_api import CloseError
from .eligibility import RULE_NAMES, activity_time, is_inbound_sms
from .messages import VERSIONS
from .timeutil import iso, parse_dt
from .timezones import CENTRAL

log = logging.getLogger("outbound.report")

PROGRESS_STATUSES = {"qualified", "customer", "deposit"}


def _pct(part: int, whole: int) -> str:
    return f"{(100 * part / whole):.0f}%" if whole else "–"


async def lead_outcome(close, lead_id: str, sent_at: datetime) -> dict:
    sms = await close.list_activities("sms", lead_id)
    meetings = await close.list_activities("meeting", lead_id)
    changes = await close.list_activities("status_change/lead", lead_id)
    replied = any(is_inbound_sms(s) and (activity_time(s) or sent_at) > sent_at for s in sms)
    booked = any((parse_dt(m.get("date_created")) or sent_at) > sent_at for m in meetings)
    progressed = any(
        (c.get("new_status_label") or "").casefold() in PROGRESS_STATUSES
        and (parse_dt(c.get("date_created")) or sent_at) > sent_at
        for c in changes
    )
    return {"replied": replied, "booked": booked, "progressed": progressed}


async def build_report(engine, now: datetime, days: int = 7) -> str:
    db, close = engine.db, engine.close
    since = now - timedelta(days=days)
    per = {v: Counter() for v in VERSIONS}
    unreadable = 0
    for row in db.texted_since(iso(since)):
        version = row.get("message_version")
        sent_at = parse_dt(row.get("sent_at"))
        if version not in per or not sent_at:
            continue
        per[version]["sent"] += 1
        try:
            outcome = await lead_outcome(close, row["lead_id"], sent_at)
        except CloseError as exc:
            log.warning("Report: could not read %s: %s", row["lead_id"], exc)
            unreadable += 1
            continue
        for key, hit in outcome.items():
            per[version][key] += int(hit)

    total = sum(per.values(), Counter())
    start = since.astimezone(CENTRAL).strftime("%b %d")
    end = now.astimezone(CENTRAL).strftime("%b %d, %Y")
    lines = [
        f"📊 First-text report{' (DRY RUN data)' if engine.dry_run else ''}",
        f"{start} – {end} · leads texted in the past {days} days",
        "",
    ]
    for label, c in [*((f"Version {v}", per[v]) for v in VERSIONS), ("All versions", total)]:
        lines.append(
            f"{label}: {c['sent']} sent · {c['replied']} replies ({_pct(c['replied'], c['sent'])}) · "
            f"{c['booked']} booked calls · {c['progressed']} → Qualified/Customer/Deposit"
        )
    lines += ["", f"Errors: {db.errors_since(iso(since))}"]
    skips = db.skip_counts_since(iso(since))
    if skips:
        lines.append("Skips by rule (distinct leads):")
        for rule, count in sorted(skips.items(), key=lambda kv: (not str(kv[0]).isdigit(), int(kv[0]) if str(kv[0]).isdigit() else 0, str(kv[0]))):
            lines.append(f"  Rule {rule}: {count} — {RULE_NAMES.get(str(rule), '')}")
    else:
        lines.append("Skips by rule: none")
    if unreadable:
        lines.append(f"(Could not read {unreadable} leads from Close; their replies/bookings aren't counted.)")
    return "\n".join(lines)
