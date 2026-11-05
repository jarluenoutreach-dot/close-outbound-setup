"""The eligibility check (team lead-ownership rulebook).

These are pure functions over data already fetched from Close, so they are easy to test.
The same functions are used by the webhook step, the backup sweep, the pre-send check and
the morning check (see Engine.full_check).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import phonenumbers

from .messages import clean_first_name
from .timeutil import parse_dt

BLOCKED_STATUSES = (
    "DNC",
    "Wrong Number",
    "Bad Data",
    "Not Interested",
    "Customer",
    "Student",
    "Deposit",
    "Wanting Refund",
    "No Show",
)
_BLOCKED = {s.casefold() for s in BLOCKED_STATUSES}

RULE_NAMES = {
    "1": "Lead replied by SMS (last 60 days) — rulebook 2 & 4",
    "2": "Another setter had a 2+ min call (last 60 days) — rulebook 1",
    "3": "Meeting booked or held (last 60 days) — rulebook 3",
    "4": "A scheduled/outbox SMS is already pending",
    "5": "Another setter texted recently (last 7 days)",
    "6": "Blocked lead status",
    "7": "Already sent this lead a first text",
    "8": "No contact has a phone number",
    "14": "Possible duplicate failed the check — rulebook 14",
    "view": "Left the smart view before sending",
    "gone": "Lead was deleted",
    "deleted": "Scheduled SMS was deleted in Close",
    "manual": "Canceled manually",
    "api": "Close API kept failing",
}

# Local DB statuses meaning "we already created a first text for this lead".
TEXTED_STATUSES = ("scheduled", "sent", "error")
_INBOUND = {"inbound", "incoming"}
_PENDING = {"scheduled", "outbox"}
_NOT_SENT = {"draft"}


@dataclass(frozen=True)
class Decision:
    eligible: bool
    rule: str | None = None
    reason: str = ""
    lead_id: str = ""
    lead_name: str = ""
    contact_id: str | None = None
    phone: str | None = None
    first_name: str | None = None
    duplicate_of: str | None = None
    duplicate_lead_id: str | None = None


def normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    try:
        parsed = phonenumbers.parse(phone, "US")
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        digits = "".join(ch for ch in phone if ch.isdigit())
        return f"+{digits}" if digits else None


def lead_name(lead: dict) -> str:
    return lead.get("display_name") or lead.get("name") or lead.get("id") or "(no name)"


def first_phone(lead: dict) -> tuple[dict | None, str | None]:
    """The first contact (in Close's order) that has a phone, and that contact's first phone."""
    for contact in lead.get("contacts") or []:
        for entry in contact.get("phones") or []:
            number = (entry.get("phone") or "").strip()
            if number:
                return contact, number
    return None, None


def lead_phones(lead: dict) -> set[str]:
    phones = set()
    for contact in lead.get("contacts") or []:
        for entry in contact.get("phones") or []:
            normalized = normalize_phone(entry.get("phone"))
            if normalized:
                phones.add(normalized)
    return phones


def is_inbound_sms(sms: dict) -> bool:
    return (sms.get("direction") or "").lower() in _INBOUND or (sms.get("status") or "").lower() == "inbox"


def activity_time(activity: dict) -> datetime | None:
    return parse_dt(activity.get("activity_at") or activity.get("date_sent") or activity.get("date_created"))


def _day(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "unknown date"


def base_decision(lead: dict) -> Decision:
    contact, phone = first_phone(lead)
    return Decision(
        eligible=True,
        reason="eligible",
        lead_id=lead.get("id", ""),
        lead_name=lead_name(lead),
        contact_id=(contact or {}).get("id"),
        phone=phone,
        first_name=clean_first_name((contact or {}).get("name")),
    )


def _fail(base: Decision, rule: str, reason: str) -> Decision:
    return replace(base, eligible=False, rule=rule, reason=reason)


def precheck(
    lead: dict,
    *,
    local_record: dict | None,
    ignore_sms_ids: frozenset[str] = frozenset(),
    require_phone: bool = True,
) -> Decision | None:
    """Cheap rules that need no activity lookups: 7 (local database), 6, 8."""
    base = base_decision(lead)
    if local_record and local_record.get("status") in TEXTED_STATUSES:
        own_sms = local_record.get("sms_activity_id")
        if not (own_sms and own_sms in ignore_sms_ids):
            return _fail(base, "7", f"already {local_record['status']} in local database")
    status = lead.get("status_label") or ""
    if status.casefold() in _BLOCKED:
        return _fail(base, "6", f"lead status is {status}")
    if require_phone and not base.phone:
        return _fail(base, "8", "no contact has a phone number")
    return None


def check_activities(
    lead: dict,
    sms: list[dict],
    calls: list[dict],
    meetings: list[dict],
    *,
    my_user_id: str,
    now: datetime,
    lookback_days: int = 60,
    other_setter_text_days: int = 7,
    long_call_seconds: int = 120,
    ignore_sms_ids: frozenset[str] = frozenset(),
) -> Decision | None:
    """Rules 1, 2, 3, 4, 5 and 7 (Close history). Missing dates are treated as recent."""
    base = base_decision(lead)
    cutoff = now - timedelta(days=lookback_days)
    text_cutoff = now - timedelta(days=other_setter_text_days)
    sms = [s for s in sms if s.get("id") not in ignore_sms_ids]

    # Rule 1: the lead replied by SMS in the lookback window.
    for s in sms:
        when = activity_time(s) or now
        if is_inbound_sms(s) and when >= cutoff:
            return _fail(base, "1", f"lead sent an SMS on {_day(when)}")

    # Rule 2: another setter had a call of 120s+ in the lookback window.
    for c in calls:
        user = c.get("user_id")
        duration = c.get("duration") or 0
        when = activity_time(c) or now
        if user and user != my_user_id and duration >= long_call_seconds and when >= cutoff:
            return _fail(base, "2", f"{duration}s call by another setter ({user}) on {_day(when)}")

    # Rule 3: a meeting booked or held in the lookback window (any status, any user).
    for m in meetings:
        starts = parse_dt(m.get("starts_at"))
        created = parse_dt(m.get("date_created"))
        if (starts and starts >= cutoff) or (created and created >= cutoff) or not (starts or created):
            return _fail(base, "3", f"meeting '{m.get('title') or 'untitled'}' on {_day(starts or created)}")

    outbound = [s for s in sms if not is_inbound_sms(s)]

    # Rule 4: anyone has a scheduled/outbox SMS to this lead right now.
    for s in outbound:
        status = (s.get("status") or "").lower()
        if status in _PENDING:
            return _fail(base, "4", f"{status} SMS by {s.get('user_id') or 'unknown user'} is pending")

    # Rule 5: another setter texted the lead recently, even with no reply.
    for s in outbound:
        status = (s.get("status") or "").lower()
        user = s.get("user_id")
        when = activity_time(s) or now
        if status not in _NOT_SENT and user and user != my_user_id and when >= text_cutoff:
            return _fail(base, "5", f"another setter ({user}) texted on {_day(when)}")

    # Rule 7: I already texted this lead (any time).
    for s in outbound:
        status = (s.get("status") or "").lower()
        if status not in _NOT_SENT and s.get("user_id") == my_user_id:
            return _fail(base, "7", f"I already texted this lead on {_day(activity_time(s))} ({status})")

    return None
