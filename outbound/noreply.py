"""Day 3 and Day 7 follow-ups for webinar leads who never answered the first text.

They come after the Day 1 nudge (outbound/nudge.py) and use the same rule: never text a lead who
has ever answered anyone on the team. Each follow-up is timed from when the first text went out,
sent Mon–Sat 9am–7pm in the lead's own time zone, and re-checked in Close right before it goes.

Only leads whose first text went out after these follow-ups went live get them (the start time is
saved on the first tick), so the existing backlog is left alone.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from .close_api import CloseError
from .nudge import _first_name, no_reply_problem
from .timeutil import iso, parse_dt
from .timezones import zones_for_phone

log = logging.getLogger("outbound.noreply")

START_KEY = "noreply_started_at"

# (days after the first text, label, text with name, text without name)
STEPS = (
    (3, "Day 3",
     "Hey {name}, Jarluen again from Daeron's team. Still thinking about the courier business? Happy to "
     "answer any questions.",
     "Hey, Jarluen again from Daeron's team. Still thinking about the courier business? Happy to answer "
     "any questions."),
    (7, "Day 7",
     "{name}, is starting a courier business still something you want this year, or should I check back "
     "later?",
     "Is starting a courier business still something you want this year, or should I check back later?"),
)


def render_step(index: int, first_name: str | None) -> str:
    _, _, with_name, without = STEPS[index]
    return with_name.format(name=first_name) if first_name else without


class NoReplyRunner:
    def __init__(self, engine):
        self.e = engine
        self.s = engine.s
        self.db = engine.db
        self.close = engine.close

    def in_window(self, now, phone: str | None) -> bool:
        """Mon–Sat, 9am–7pm in every time zone the lead's number could be in."""
        for zone in zones_for_phone(phone, self.s.default_timezone):
            local = now.astimezone(zone)
            if local.weekday() not in self.s.noreply_days:
                return False
            if not self.s.noreply_start_hour <= local.hour < self.s.noreply_end_hour:
                return False
        return True

    def _stop(self, lead_id: str, status: str, reason: str) -> None:
        self.db.upsert(lead_id, noreply_status=status, noreply_reason=reason)
        self.e._event(f"noreply_{status}", lead_id, detail=reason)
        log.info("FOLLOW-UP %s %s: %s", status.upper(), lead_id, reason)

    def due(self, now) -> list[dict]:
        started = self.db.kv_get(START_KEY)
        if started is None:
            self.db.kv_set(START_KEY, iso(now))
            log.info("Webinar Day 3/Day 7 follow-ups turned on: only first texts sent from now on get them")
            return []
        cur = self.db.conn.execute(
            "SELECT * FROM leads WHERE status = 'sent' AND nudge_status = 'sent' AND noreply_status IS NULL "
            "AND sent_at IS NOT NULL AND sent_at >= ? AND COALESCE(noreply_step, 0) < ? ORDER BY sent_at",
            (started, len(STEPS)),
        )
        out = []
        for rec in (dict(r) for r in cur):
            days = STEPS[rec.get("noreply_step") or 0][0]
            if parse_dt(rec["sent_at"]) + timedelta(days=days) <= now:
                out.append(rec)
        return out

    async def tick(self) -> bool:
        """Send at most one follow-up. Returns True if one went out."""
        if not self.s.noreply_enabled or self.e.stopped():
            return False
        now = self.e.clock()
        for rec in self.due(now):
            if not self.in_window(now, rec.get("phone")):
                continue
            if self.e.send_wait_seconds() > 0:
                return False
            async with self.e._locks[rec["lead_id"]]:
                rec = self.db.get(rec["lead_id"])
                if rec and rec["status"] == "sent" and rec.get("noreply_status") is None:
                    return await self._send(rec, now)
        return False

    async def _send(self, rec: dict, now) -> bool:
        lead_id = rec["lead_id"]
        index = rec.get("noreply_step") or 0
        try:
            lead = await self.close.get_lead(lead_id)
            sms = await self.close.list_activities("sms", lead_id) if lead else []
            calls = await self.close.list_activities("call", lead_id) if lead else []
            meetings = await self.close.list_activities("meeting", lead_id) if lead else []
        except CloseError as exc:
            log.warning("Follow-up check failed for %s (will retry): %s", lead_id, exc)
            return False
        if lead is None:
            self._stop(lead_id, "stopped", "lead was deleted")
            return False
        own = frozenset(i for i in [rec.get("nudge_sms_id"), *(rec.get("noreply_sms_ids") or "").split(",")] if i)
        reason, pending = no_reply_problem(self.s, rec, lead, sms, calls, meetings, own)
        if pending:
            return False
        if reason:
            self._stop(lead_id, "stopped", reason)
            return False

        label = STEPS[index][1]
        text = render_step(index, _first_name(lead, rec.get("contact_id")))
        last = index + 1 >= len(STEPS)
        if self.e.dry_run:
            log.info("[DRY RUN] would send webinar %s follow-up to %s (%s) %s: %s",
                     label, rec.get("lead_name"), lead_id, rec.get("phone"), text)
            self.db.upsert(lead_id, noreply_step=index + 1, noreply_last_at=iso(now),
                           noreply_status="done" if last else None)
            self.e._event("noreply_sent", lead_id, detail=f"[dry run] {label}")
            return True
        try:
            out = await self.close.create_sms({
                "lead_id": lead_id, "contact_id": rec.get("contact_id"), "local_phone": self.s.local_phone,
                "remote_phone": rec.get("phone"), "text": text, "status": "outbox",
            })
        except CloseError as exc:
            self.e._event("noreply_send_failed", lead_id, detail=str(exc))
            self._stop(lead_id, "error", f"{label} follow-up failed: {exc.message}")
            await self.e._task(lead_id, f"Webinar {label} follow-up failed: {exc.message}")
            return False
        ids = ",".join(i for i in [*(rec.get("noreply_sms_ids") or "").split(","), out.get("id") or ""] if i)
        self.db.upsert(lead_id, noreply_step=index + 1, noreply_last_at=iso(now), noreply_sms_ids=ids,
                       noreply_status="done" if last else None, noreply_reason="all follow-ups sent" if last else None)
        self.e._event("noreply_sent", lead_id, detail=f"{label} to {rec.get('phone')} sms={out.get('id')}")
        log.info("FOLLOW-UP SENT %s %s (%s) %s sms=%s", label, rec.get("lead_name"), lead_id, rec.get("phone"),
                 out.get("id"))
        await self.e._note(lead_id, f"Webinar {label} follow-up sent (no reply yet)")
        return True
