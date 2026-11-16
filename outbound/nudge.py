"""One automatic nudge for webinar leads who never replied to the first text.

24 hours after the first text went out (8am–8pm in the lead's time zone, like the first text),
the lead is re-checked and gets ONE nudge, unless the lead has ever replied to anyone:
any inbound text or inbound call from the lead, to any number on the team, at any time.

Only first texts sent after this feature went live are nudged (the start time is saved on the
first tick), so the backlog of older leads is left alone.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from .close_api import CloseError
from .eligibility import _BLOCKED, is_inbound_sms
from .timeutil import iso, parse_dt
from .timezones import in_send_window, zones_for_phone

log = logging.getLogger("outbound.nudge")

NUDGE_TEXT = (
    "Hey {name}, just following up on my text yesterday. Are you looking to start a courier business "
    "from scratch, or grow one you already run?"
)
NUDGE_TEXT_NO_NAME = (
    "Hey, just following up on my text yesterday. Are you looking to start a courier business from "
    "scratch, or grow one you already run?"
)
START_KEY = "nudge_started_at"
_PENDING = {"scheduled", "outbox"}


def no_reply_problem(settings, rec: dict, lead: dict, sms: list[dict], calls: list[dict], meetings: list[dict],
                     own_sms_ids: frozenset[str] = frozenset()) -> tuple[str | None, bool]:
    """Why this webinar lead must not get a no-reply text, or (None, False) if it may.

    The rule: never text a lead who has ever answered anyone on the team. The second value is
    True when another SMS is pending for the lead, so the caller should look again later."""
    if any(is_inbound_sms(s) for s in sms):
        return "lead has replied by text before", False
    if any((c.get("direction") or "").lower() in ("inbound", "incoming") for c in calls):
        return "lead has called in before", False
    if any((c.get("duration") or 0) >= settings.long_call_seconds for c in calls):
        return "someone already had a conversation with the lead by phone", False
    if meetings:
        return "lead has a meeting", False
    status = lead.get("status_label") or ""
    if status.casefold() in _BLOCKED:
        return f"lead status is {status}", False
    sent_at = parse_dt(rec.get("sent_at"))
    mine = own_sms_ids | {rec.get("sms_activity_id")}
    for s in sms:
        if is_inbound_sms(s) or s.get("id") in mine:
            continue
        if (s.get("status") or "").lower() in _PENDING:
            return None, True
        when = parse_dt(s.get("date_created") or s.get("activity_at"))
        if s.get("user_id") != settings.my_user_id and when and sent_at and when > sent_at:
            return "another setter texted the lead after the first text", False
    first = next((s for s in sms if s.get("id") == rec.get("sms_activity_id")), None)
    if first and (first.get("status") or "").lower() == "error":
        return "first text failed", False
    return None, False


def render_nudge(first_name: str | None) -> str:
    return NUDGE_TEXT.format(name=first_name) if first_name else NUDGE_TEXT_NO_NAME


class NudgeRunner:
    def __init__(self, engine):
        self.e = engine
        self.s = engine.s
        self.db = engine.db
        self.close = engine.close

    def _done(self, lead_id: str, status: str, reason: str) -> None:
        self.db.upsert(lead_id, nudge_status=status, nudge_reason=reason)
        self.e._event(f"nudge_{status}", lead_id, detail=reason)
        log.info("NUDGE %s %s: %s", status.upper(), lead_id, reason)

    def due(self, now) -> list[dict]:
        started = self.db.kv_get(START_KEY)
        if started is None:
            self.db.kv_set(START_KEY, iso(now))
            log.info("Webinar nudge turned on: only first texts sent from now on get a nudge")
            return []
        cutoff = iso(now - timedelta(hours=self.s.nudge_after_hours))
        cur = self.db.conn.execute(
            "SELECT * FROM leads WHERE status = 'sent' AND nudge_status IS NULL AND sent_at IS NOT NULL "
            "AND sent_at >= ? AND sent_at <= ? ORDER BY sent_at",
            (started, cutoff),
        )
        return [dict(r) for r in cur]

    async def tick(self) -> bool:
        """Send at most one nudge. Returns True if one went out."""
        if not self.s.nudge_enabled or self.e.stopped():
            return False
        now = self.e.clock()
        for rec in self.due(now):
            zones = zones_for_phone(rec.get("phone"), self.s.default_timezone)
            if not in_send_window(now, zones, self.s.send_window_start_hour, self.s.send_window_end_hour,
                                  self.s.send_window_end_minute):
                continue
            if self.e.send_wait_seconds() > 0:
                return False
            async with self.e._locks[rec["lead_id"]]:
                rec = self.db.get(rec["lead_id"])
                if rec and rec["status"] == "sent" and rec.get("nudge_status") is None:
                    return await self._nudge(rec, now)
        return False

    async def _nudge(self, rec: dict, now) -> bool:
        lead_id = rec["lead_id"]
        try:
            lead = await self.close.get_lead(lead_id)
            sms = await self.close.list_activities("sms", lead_id) if lead else []
            calls = await self.close.list_activities("call", lead_id) if lead else []
            meetings = await self.close.list_activities("meeting", lead_id) if lead else []
        except CloseError as exc:
            log.warning("Nudge check failed for %s (will retry): %s", lead_id, exc)
            return False
        if lead is None:
            self._done(lead_id, "skipped", "lead was deleted")
            return False

        reason, pending = no_reply_problem(self.s, rec, lead, sms, calls, meetings)
        if pending:
            return False  # something else is about to go out; look again next tick
        if reason:
            self._done(lead_id, "skipped", reason)
            return False

        text = render_nudge(rec.get("first_name") or _first_name(lead, rec.get("contact_id")))
        if self.e.dry_run:
            log.info("[DRY RUN] would NUDGE %s (%s) %s: %s", rec.get("lead_name"), lead_id, rec.get("phone"), text)
            self.db.upsert(lead_id, nudge_status="sent", nudge_sent_at=iso(now), nudge_reason="dry run")
            self.e._event("nudge_sent", lead_id, detail="[dry run]")
            return True
        try:
            sms_out = await self.close.create_sms({
                "lead_id": lead_id, "contact_id": rec.get("contact_id"), "local_phone": self.s.local_phone,
                "remote_phone": rec.get("phone"), "text": text, "status": "outbox",
            })
        except CloseError as exc:
            self.e._event("nudge_send_failed", lead_id, detail=str(exc))
            self._done(lead_id, "error", f"nudge failed: {exc.message}")
            await self.e._task(lead_id, f"Webinar nudge failed: {exc.message}")
            return False
        self.db.upsert(lead_id, nudge_status="sent", nudge_sent_at=iso(now), nudge_sms_id=sms_out.get("id"),
                       nudge_reason=None)
        self.e._event("nudge_sent", lead_id, detail=f"to {rec.get('phone')} sms={sms_out.get('id')}")
        log.info("NUDGE SENT %s (%s) %s sms=%s", rec.get("lead_name"), lead_id, rec.get("phone"), sms_out.get("id"))
        await self.e._note(lead_id, "Webinar nudge sent (no reply to the first text after 24h)")
        return True


def _first_name(lead: dict, contact_id: str | None) -> str | None:
    from .messages import clean_first_name

    for contact in lead.get("contacts") or []:
        if contact_id is None or contact.get("id") == contact_id:
            return clean_first_name(contact.get("name"))
    return None
